"""Annotate wall structure with offset dimension lines following architectural drafting standards.

Reads wall mask PNG and raw elements JSON to produce annotated PDF/PNG with
ISO 128 / ANSI Y14.5 style dimension lines for each room.
"""

import argparse
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import fitz
import numpy as np

logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Constants ---
SCALE_FACTOR = 3
PAGE_WIDTH_PT = 792.0
PAGE_HEIGHT_PT = 612.0
SCAN_BAND = 60  # ±px band for wall scanning
WALL_DILATE_ITER = 2
ARROW_LEN = 10
ARROW_HW = 3
DIM_OFFSET_1 = 28  # first dimension line offset from wall
DIM_OFFSET_2 = 50  # second level offset (for shared walls)
EXT_GAP = 3  # gap from wall to extension line start
EXT_OVERSHOOT = 5  # extension line past dimension line
DIM_FONT = cv2.FONT_HERSHEY_SIMPLEX
DIM_FONT_SCALE = 0.42
DIM_THICK = 1
LABEL_FONT_SCALE = 0.50
LABEL_THICK = 1
AREA_FONT_SCALE = 0.36
# Colors (BGR)
COL_DIM = (180, 60, 20)  # blue dimension lines
COL_WALL = (100, 30, 20)  # dark blue wall overlay
COL_LABEL = (30, 30, 30)  # dark gray room labels
COL_WHITE = (255, 255, 255)
WALL_SCAN_MAX = 900
VALIDATION_TOLERANCE = 0.20  # 20% tolerance for scan vs expected


@dataclass
class Room:
    """Room annotation data."""
    name: str
    dim_str: str
    w_ft: float
    h_ft: float
    area_sqft: float
    cx_pt: float  # label center in PDF points
    cy_pt: float
    # Wall boundaries in pixels
    wl: int = 0
    wr: int = 0
    wt: int = 0
    wb: int = 0
    scan_valid: dict = None  # which boundaries came from scan vs computed


def parse_dim(s: str) -> tuple[float, float]:
    """Parse '13\\'6\" X 15\\'6\"' → (13.5, 15.5)."""
    m = re.findall(r"(\d+)'(\d+)\"", s)
    if len(m) >= 2:
        return int(m[0][0]) + int(m[0][1]) / 12.0, int(m[1][0]) + int(m[1][1]) / 12.0
    return 0.0, 0.0


def extract_rooms(raw_json_path: Path) -> list[Room]:
    """Extract room names, dimensions, and positions from raw elements JSON.

    Args:
        raw_json_path: Path to the raw elements JSON file.

    Returns:
        List of Room objects for each identified room.
    """
    with open(raw_json_path) as f:
        data = json.load(f)

    texts = []
    for g in data["groups"]:
        if g["group"] == "text":
            texts = g["elements"]
            break

    names = [e for e in texts if 6.5 <= e["font_size"] <= 7.5]
    dims = [e for e in texts if 4.5 <= e["font_size"] <= 5.5]

    def center(el: dict) -> tuple[float, float]:
        p = el["points"]
        return (p[0]["x"] + p[1]["x"]) / 2, (p[0]["y"] + p[1]["y"]) / 2

    # Merge multi-word room names
    MULTI = {
        "BREAKFAST NOOK": ("BREAKFAST", "NOOK"),
        "LIVING ROOM": ("LIVING", "ROOM"),
        "DINING ROOM": ("DINING", "ROOM"),
    }
    used = set()
    merged: list[dict] = []

    # Multi-word pairs
    for full, (p1, p2) in MULTI.items():
        for i, e1 in enumerate(names):
            if i in used or e1["label"] != p1:
                continue
            x1, y1 = center(e1)
            for j, e2 in enumerate(names):
                if j in used or j == i or e2["label"] != p2:
                    continue
                x2, y2 = center(e2)
                if math.hypot(x2 - x1, y2 - y1) < 40:
                    merged.append({"name": full, "x": (x1 + x2) / 2, "y": min(y1, y2)})
                    used.update((i, j))
                    break

    # Single-element multi-word (MASTER BDRM, MASTER BATH)
    for i, e in enumerate(names):
        if i in used:
            continue
        if e["label"] in ("MASTER BDRM", "MASTER BATH"):
            x, y = center(e)
            merged.append({"name": e["label"], "x": x, "y": y})
            used.add(i)

    # Single-word rooms
    SKIP = {"SHOWER"}
    for i, e in enumerate(names):
        if i in used or e["label"] in SKIP:
            continue
        x, y = center(e)
        merged.append({"name": e["label"], "x": x, "y": y})
        used.add(i)

    # Match names to nearest dimension text
    rooms: list[Room] = []
    used_d: set[int] = set()

    for entry in merged:
        nx, ny = entry["x"], entry["y"]
        best_i, best_dist = -1, 999.0
        for di, d in enumerate(dims):
            if di in used_d:
                continue
            dx, dy = center(d)
            dist = math.hypot(dx - nx, dy - ny)
            if dist < best_dist:
                best_dist = dist
                best_i = di

        if best_i < 0 or best_dist > 30:
            logger.warning("No dimension for %s (dist=%.1f)", entry["name"], best_dist)
            continue

        d = dims[best_i]
        ds = d["label"]
        w, h = parse_dim(ds)

        # Disambiguate duplicate BEDROOMs by dimension
        display = entry["name"]
        if display == "BEDROOM" and w == 12.0 and h == 11.5:
            display = "BEDROOM 2"

        rooms.append(Room(
            name=display, dim_str=ds, w_ft=w, h_ft=h, area_sqft=w * h,
            cx_pt=nx, cy_pt=ny,
        ))
        used_d.add(best_i)
        logger.info("  %-16s %s", display, ds)

    return rooms


def band_scan(mask: np.ndarray, cx: int, cy: int, direction: str,
              band: int = SCAN_BAND, max_dist: int = WALL_SCAN_MAX) -> int | None:
    """Scan from center in a direction using a perpendicular band to find walls.

    Starts scanning from cx±1 / cy±1 (not from the center itself) to avoid
    detecting walls that pass through the center point.

    Args:
        mask: Dilated grayscale wall mask.
        cx: Center x pixel.
        cy: Center y pixel.
        direction: One of 'left', 'right', 'up', 'down'.
        band: Half-width of perpendicular band.
        max_dist: Maximum scan distance.

    Returns:
        Pixel coordinate of nearest wall, or None if not found.
    """
    h, w = mask.shape

    if direction == "left":
        for x in range(cx - 1, max(cx - max_dist, -1), -1):
            strip = mask[max(cy - band, 0):min(cy + band, h), x]
            if np.any(strip > 128):
                return x
    elif direction == "right":
        for x in range(cx + 1, min(cx + max_dist, w)):
            strip = mask[max(cy - band, 0):min(cy + band, h), x]
            if np.any(strip > 128):
                return x
    elif direction == "up":
        for y in range(cy - 1, max(cy - max_dist, -1), -1):
            strip = mask[y, max(cx - band, 0):min(cx + band, w)]
            if np.any(strip > 128):
                return y
    elif direction == "down":
        for y in range(cy + 1, min(cy + max_dist, h)):
            strip = mask[y, max(cx - band, 0):min(cx + band, w)]
            if np.any(strip > 128):
                return y
    return None


def adaptive_scan(mask: np.ndarray, cx: int, cy: int, direction: str,
                  exp_dist: float) -> int | None:
    """Try all band widths and pick the result closest to expected distance.

    Narrow bands avoid cross-wall false positives but miss walls with gaps.
    Wide bands find walls through gaps but may pick up wrong walls.
    By trying all widths and comparing to expected distance, we get the
    most accurate result.

    Args:
        mask: Dilated grayscale wall mask.
        cx: Center x pixel.
        cy: Center y pixel.
        direction: Scan direction.
        exp_dist: Expected distance from center to wall (half of room dimension in px).

    Returns:
        Pixel coordinate of nearest wall, or None if no valid wall found.
    """
    candidates: list[tuple[float, int]] = []  # (error_vs_expected, position)
    for band in [5, 10, 20, 40, 80]:
        result = band_scan(mask, cx, cy, direction, band=band)
        if result is not None:
            if direction in ("left", "right"):
                dist = abs(result - cx)
            else:
                dist = abs(result - cy)
            if dist > 10 and dist < exp_dist * 3:
                error = abs(dist - exp_dist)
                candidates.append((error, result))
    if not candidates:
        return None
    # Pick the result closest to expected distance
    candidates.sort()
    return candidates[0][1]


def find_walls(mask: np.ndarray, rooms: list[Room], ppf: float) -> None:
    """Find wall boundaries for each room using adaptive band scanning.

    Uses progressively wider scan bands, validates against expected dimensions,
    and falls back to computed positions when scanning fails.

    Args:
        mask: Dilated grayscale wall mask.
        rooms: List of rooms to update with wall positions.
        ppf: Pixels per foot for validation/fallback.
    """
    h, w = mask.shape

    for room in rooms:
        cx = int(round(room.cx_pt * SCALE_FACTOR))
        cy = int(round(room.cy_pt * SCALE_FACTOR))
        cx = max(0, min(cx, w - 1))
        cy = max(0, min(cy, h - 1))

        exp_w = room.w_ft * ppf
        exp_h = room.h_ft * ppf

        sv = {"left": False, "right": False, "top": False, "bottom": False}

        # Adaptive scan in each direction
        sl = adaptive_scan(mask, cx, cy, "left", exp_w / 2)
        sr = adaptive_scan(mask, cx, cy, "right", exp_w / 2)
        st = adaptive_scan(mask, cx, cy, "up", exp_h / 2)
        sb = adaptive_scan(mask, cx, cy, "down", exp_h / 2)

        # Validate width
        if sl is not None and sr is not None:
            scanned_w = sr - sl
            if abs(scanned_w - exp_w) / exp_w < VALIDATION_TOLERANCE:
                room.wl, room.wr = sl, sr
                sv["left"] = sv["right"] = True

        # Validate height
        if st is not None and sb is not None:
            scanned_h = sb - st
            if abs(scanned_h - exp_h) / exp_h < VALIDATION_TOLERANCE:
                room.wt, room.wb = st, sb
                sv["top"] = sv["bottom"] = True

        # Fallback for width: try anchoring from individual walls, then center
        if not sv["left"]:
            placed = False
            # Try each scanned wall as anchor — pick one that contains the label
            if sl is not None:
                computed_r = int(sl + exp_w)
                if sl <= cx <= computed_r:
                    room.wl, room.wr = sl, computed_r
                    placed = True
            if not placed and sr is not None:
                computed_l = int(sr - exp_w)
                if computed_l <= cx <= sr:
                    room.wl, room.wr = computed_l, sr
                    placed = True
            if not placed:
                room.wl = int(cx - exp_w / 2)
                room.wr = int(cx + exp_w / 2)

        # Fallback for height: try anchoring from individual walls, then center
        if not sv["top"]:
            placed = False
            if st is not None:
                computed_b = int(st + exp_h)
                if st <= cy <= computed_b:
                    room.wt, room.wb = st, computed_b
                    placed = True
            if not placed and sb is not None:
                computed_t = int(sb - exp_h)
                if computed_t <= cy <= sb:
                    room.wt, room.wb = computed_t, sb
                    placed = True
            if not placed:
                room.wt = int(cy - exp_h / 2)
                room.wb = int(cy + exp_h / 2)

        # Clamp to image bounds
        room.wl = max(0, room.wl)
        room.wr = min(w - 1, room.wr)
        room.wt = max(0, room.wt)
        room.wb = min(h - 1, room.wb)
        room.scan_valid = sv

        actual_w = room.wr - room.wl
        actual_h = room.wb - room.wt
        logger.info("  %-16s L=%-4d R=%-4d T=%-4d B=%-4d  %dx%d px  (scan: %s)",
                     room.name, room.wl, room.wr, room.wt, room.wb,
                     actual_w, actual_h,
                     " ".join(f"{k[0].upper()}={'Y' if v else 'n'}" for k, v in sv.items()))


def calibrate_ppf(mask: np.ndarray, rooms: list[Room]) -> float:
    """Calibrate pixels_per_foot using well-enclosed rectangular rooms.

    Uses narrow band scans (±5px) on BEDROOM, BEDROOM 2, and OFFICE which
    have clear walls on all 4 sides. Falls back to GARAGE with wider band.

    Args:
        mask: Dilated grayscale wall mask.
        rooms: List of rooms.

    Returns:
        Calibrated pixels per foot.
    """
    cal_rooms = ["BEDROOM", "BEDROOM 2", "OFFICE"]
    ppf_samples = []

    for room in rooms:
        if room.name not in cal_rooms:
            continue
        cx = int(round(room.cx_pt * SCALE_FACTOR))
        cy = int(round(room.cy_pt * SCALE_FACTOR))

        # Use band=±40 for calibration — wide enough to find walls through gaps,
        # narrow enough to avoid most cross-wall false positives for enclosed rooms
        sl = band_scan(mask, cx, cy, "left", band=40)
        sr = band_scan(mask, cx, cy, "right", band=40)
        st = band_scan(mask, cx, cy, "up", band=40)
        sb = band_scan(mask, cx, cy, "down", band=40)

        if sl is not None and sr is not None and sr - sl > 50:
            ppf_w = (sr - sl) / room.w_ft
            ppf_samples.append(ppf_w)
            logger.info("  Cal %s width: %d px / %.1f ft = %.2f px/ft",
                         room.name, sr - sl, room.w_ft, ppf_w)
        if st is not None and sb is not None and sb - st > 50:
            ppf_h = (sb - st) / room.h_ft
            ppf_samples.append(ppf_h)
            logger.info("  Cal %s height: %d px / %.1f ft = %.2f px/ft",
                         room.name, sb - st, room.h_ft, ppf_h)

    if ppf_samples:
        # Robust calibration: use median, reject outliers > 20% from median
        ppf_samples.sort()
        median = ppf_samples[len(ppf_samples) // 2]
        filtered = [s for s in ppf_samples if abs(s - median) / median < 0.20]
        if filtered:
            ppf = sum(filtered) / len(filtered)
            logger.info("Calibrated %.2f px/ft from %d/%d measurements (median=%.2f)",
                         ppf, len(filtered), len(ppf_samples), median)
            return ppf
        ppf = median
        logger.info("Calibrated %.2f px/ft (median of %d measurements)", ppf, len(ppf_samples))
        return ppf

    # Fallback: GARAGE width with wider band
    garage = next((r for r in rooms if r.name == "GARAGE"), None)
    if garage:
        cx = int(round(garage.cx_pt * SCALE_FACTOR))
        cy = int(round(garage.cy_pt * SCALE_FACTOR))
        sl = band_scan(mask, cx, cy, "left", band=40)
        sr = band_scan(mask, cx, cy, "right", band=40)
        if sl is not None and sr is not None:
            ppf = (sr - sl) / garage.w_ft
            logger.info("Calibrated %.2f px/ft from GARAGE width", ppf)
            return ppf

    logger.warning("Calibration fallback: using 24.5 px/ft")
    return 24.5


def draw_arrowhead(img: np.ndarray, tip: tuple[int, int],
                   dx: float, dy: float, color: tuple[int, int, int]) -> None:
    """Draw a filled arrowhead pointing in direction (dx, dy).

    Args:
        img: Image to draw on.
        tip: Arrow tip point (x, y).
        dx: Direction x component (normalized).
        dy: Direction y component (normalized).
        color: BGR color.
    """
    px, py = -dy, dx  # perpendicular
    a = (int(tip[0] - dx * ARROW_LEN + px * ARROW_HW),
         int(tip[1] - dy * ARROW_LEN + py * ARROW_HW))
    b = (int(tip[0] - dx * ARROW_LEN - px * ARROW_HW),
         int(tip[1] - dy * ARROW_LEN - py * ARROW_HW))
    pts = np.array([[tip, a, b]], dtype=np.int32)
    cv2.fillPoly(img, pts, color, cv2.LINE_AA)


def draw_dim_h(img: np.ndarray, x1: int, x2: int, y_wall: int,
               offset: int, text: str, below: bool = True) -> None:
    """Draw horizontal dimension line with extension lines and centered text.

    Args:
        img: Image to draw on.
        x1: Left x.
        x2: Right x.
        y_wall: Y of the wall face.
        offset: Pixel offset for dimension line.
        text: Dimension text.
        below: If True, place below wall; else above.
    """
    sgn = 1 if below else -1
    y_dim = y_wall + sgn * offset

    # Extension lines
    y_start = y_wall + sgn * EXT_GAP
    y_end = y_dim + sgn * EXT_OVERSHOOT
    cv2.line(img, (x1, y_start), (x1, y_end), COL_DIM, 1, cv2.LINE_AA)
    cv2.line(img, (x2, y_start), (x2, y_end), COL_DIM, 1, cv2.LINE_AA)

    # Dimension line
    cv2.line(img, (x1, y_dim), (x2, y_dim), COL_DIM, 1, cv2.LINE_AA)

    # Arrowheads
    draw_arrowhead(img, (x1, y_dim), -1, 0, COL_DIM)
    draw_arrowhead(img, (x2, y_dim), 1, 0, COL_DIM)

    # Text
    (tw, th), _ = cv2.getTextSize(text, DIM_FONT, DIM_FONT_SCALE, DIM_THICK)
    tx = (x1 + x2) // 2 - tw // 2
    ty = y_dim - 4
    cv2.rectangle(img, (tx - 2, ty - th - 1), (tx + tw + 2, ty + 2), COL_WHITE, -1)
    cv2.putText(img, text, (tx, ty), DIM_FONT, DIM_FONT_SCALE, COL_DIM, DIM_THICK, cv2.LINE_AA)


def draw_dim_v(img: np.ndarray, y1: int, y2: int, x_wall: int,
               offset: int, text: str, right: bool = True) -> None:
    """Draw vertical dimension line with extension lines and rotated text.

    Args:
        img: Image to draw on.
        y1: Top y.
        y2: Bottom y.
        x_wall: X of the wall face.
        offset: Pixel offset for dimension line.
        text: Dimension text.
        right: If True, place to right of wall; else left.
    """
    sgn = 1 if right else -1
    x_dim = x_wall + sgn * offset

    # Extension lines
    x_start = x_wall + sgn * EXT_GAP
    x_end = x_dim + sgn * EXT_OVERSHOOT
    cv2.line(img, (x_start, y1), (x_end, y1), COL_DIM, 1, cv2.LINE_AA)
    cv2.line(img, (x_start, y2), (x_end, y2), COL_DIM, 1, cv2.LINE_AA)

    # Dimension line
    cv2.line(img, (x_dim, y1), (x_dim, y2), COL_DIM, 1, cv2.LINE_AA)

    # Arrowheads
    draw_arrowhead(img, (x_dim, y1), 0, -1, COL_DIM)
    draw_arrowhead(img, (x_dim, y2), 0, 1, COL_DIM)

    # Rotated text — draw on white background for consistent anti-aliasing
    (tw, th), _ = cv2.getTextSize(text, DIM_FONT, DIM_FONT_SCALE, DIM_THICK)
    txt_img = np.full((th + 8, tw + 8, 3), 255, dtype=np.uint8)
    cv2.putText(txt_img, text, (4, th + 2), DIM_FONT, DIM_FONT_SCALE, COL_DIM, DIM_THICK, cv2.LINE_AA)
    rotated = cv2.rotate(txt_img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    rh, rw = rotated.shape[:2]

    # Position
    ty = (y1 + y2) // 2 - rh // 2
    tx = x_dim - rw - 3 if right else x_dim + 3

    # Clamp
    ih, iw = img.shape[:2]
    tx = max(0, min(tx, iw - rw))
    ty = max(0, min(ty, ih - rh))

    # Draw: paste the white-background rotated text directly
    cv2.rectangle(img, (tx - 1, ty - 1), (tx + rw + 1, ty + rh + 1), COL_WHITE, -1)
    roi = img[ty:ty + rh, tx:tx + rw]
    if roi.shape[:2] == (rh, rw):
        roi[:] = rotated


def draw_room_label(img: np.ndarray, room: Room) -> None:
    """Draw computed area label centered in the room.

    Only draws the area sqft — room names already exist on the blueprint.

    Args:
        img: Image to draw on.
        room: Room data.
    """
    cx = (room.wl + room.wr) // 2
    cy = (room.wt + room.wb) // 2

    area_s = f"{room.area_sqft:.0f} sqft"
    (aw, ah), _ = cv2.getTextSize(area_s, DIM_FONT, AREA_FONT_SCALE, 1)
    ax = cx - aw // 2
    ay = cy + ah + 12
    cv2.rectangle(img, (ax - 2, ay - ah - 1), (ax + aw + 2, ay + 2), COL_WHITE, -1)
    cv2.putText(img, area_s, (ax, ay), DIM_FONT, AREA_FONT_SCALE, COL_DIM, 1, cv2.LINE_AA)


def dim_placement(rooms: list[Room]) -> dict[str, dict]:
    """Decide dimension line placement per room — all dims go OUTSIDE the room.

    Uses explicit per-room placement based on floor plan layout analysis:
    - Horizontal dims default below, except rooms at building bottom or with
      tight clearance below go above.
    - Vertical dims default right, except rooms at building right edge or
      specific rooms that need left placement go left.
    - Offsets increase when clearance on the chosen side is tight.

    Args:
        rooms: List of rooms with wall boundaries.

    Returns:
        Dict of room name → {h_below, v_right, h_off, v_off}.
    """
    # Explicit placement: (h_below, v_right)
    # Determined from floor plan layout analysis and user-specified fixes.
    EXPLICIT: dict[str, tuple[bool, bool]] = {
        "PATIO":          (False, False),  # H above (top edge exterior), V left
        "MASTER BDRM":    (False, True),   # H above top wall, V right of right wall
        "BEDROOM":        (False, False),  # H above top wall, V left of left wall
        "BREAKFAST NOOK": (True,  True),   # H below, V right
        "LIVING ROOM":    (True,  False),  # H below, V left (tight right)
        "BEDROOM 2":      (False, False),  # H above (5px gap to GARAGE below), V left
        "BATHROOM":       (False, False),  # H above (tight below), V left
        "KITCHEN":        (True,  False),  # H below, V left (away from Living Room)
        "CLOSET":         (False, False),  # H above (avoid MASTER BATH below), V left
        "MASTER BATH":    (True,  True),   # H below, V right (separate from CLOSET)
        "GARAGE":         (True,  False),  # H below (exterior below building), V left
        "DINING ROOM":    (True,  False),  # H below, V left
        "FOYER":          (False, False),  # H above (tight below w/ ENTRY), V left
        "LAUNDRY":        (False, False),  # H above, V left
        "OFFICE":         (True,  True),   # H below bottom wall, V right of right wall
        "ENTRY":          (True,  False),  # H below (exterior below), V left
    }

    rooms_by_name = {r.name: r for r in rooms}
    placements = {}

    for room in rooms:
        h_below, v_right = EXPLICIT.get(room.name, (True, True))

        # Compute clearance on the chosen side for offset sizing
        h_clearance = _clearance(room, rooms, "below" if h_below else "above")
        v_clearance = _clearance(room, rooms, "right" if v_right else "left")

        # Use larger offset when clearance is ample (>60), tight offset when tight
        h_off = DIM_OFFSET_1 if h_clearance < 60 else DIM_OFFSET_2
        v_off = DIM_OFFSET_1 if v_clearance < 60 else DIM_OFFSET_2

        placements[room.name] = {
            "h_below": h_below, "v_right": v_right,
            "h_off": h_off, "v_off": v_off,
        }

    return placements


def _clearance(room: Room, rooms: list[Room], direction: str) -> int:
    """Compute clearance from room boundary to nearest neighbor on the given side.

    Args:
        room: The room to compute clearance for.
        rooms: All rooms.
        direction: One of 'below', 'above', 'right', 'left'.

    Returns:
        Clearance in pixels (distance to nearest obstacle).
    """
    iw = int(PAGE_WIDTH_PT * SCALE_FACTOR)
    ih = int(PAGE_HEIGHT_PT * SCALE_FACTOR)
    clearance = ih if direction in ("below", "above") else iw

    for other in rooms:
        if other.name == room.name:
            continue

        if direction in ("below", "above"):
            # Check horizontal overlap
            h_overlap = min(room.wr, other.wr) - max(room.wl, other.wl)
            if h_overlap <= 0:
                continue
            if direction == "below" and other.wt > room.wb:
                clearance = min(clearance, other.wt - room.wb)
            elif direction == "above" and other.wb < room.wt:
                clearance = min(clearance, room.wt - other.wb)
        else:
            # Check vertical overlap
            v_overlap = min(room.wb, other.wb) - max(room.wt, other.wt)
            if v_overlap <= 0:
                continue
            if direction == "right" and other.wl > room.wr:
                clearance = min(clearance, other.wl - room.wr)
            elif direction == "left" and other.wr < room.wl:
                clearance = min(clearance, room.wl - other.wr)

    # Also check image edge clearance
    if direction == "below":
        clearance = min(clearance, ih - room.wb)
    elif direction == "above":
        clearance = min(clearance, room.wt)
    elif direction == "right":
        clearance = min(clearance, iw - room.wr)
    elif direction == "left":
        clearance = min(clearance, room.wl)

    return clearance


def generate_table(rooms: list[Room]) -> np.ndarray:
    """Generate room schedule summary table image.

    Args:
        rooms: List of Room objects.

    Returns:
        Numpy image of the table.
    """
    row_h = 26
    cols = [200, 110, 110, 110]
    tw = sum(cols) + 20
    interior = sorted([r for r in rooms if r.name != "PATIO"],
                      key=lambda r: r.area_sqft, reverse=True)
    title_h = 36
    hdr_h = 30
    th = title_h + hdr_h + (len(interior) + 2) * row_h + 20
    timg = np.ones((th, tw, 3), dtype=np.uint8) * 255

    y = 8
    cv2.putText(timg, "ROOM SCHEDULE", (10, y + 22), DIM_FONT, 0.6, (0, 0, 0), 2, cv2.LINE_AA)
    y += title_h

    hdrs = ["Room", "Width", "Height", "Area (sqft)"]
    x = 10
    for i, h in enumerate(hdrs):
        cv2.putText(timg, h, (x + 4, y + 20), DIM_FONT, 0.40, (0, 0, 0), 1, cv2.LINE_AA)
        x += cols[i]
    y += hdr_h
    cv2.line(timg, (10, y), (tw - 10, y), (0, 0, 0), 2)

    total = 0.0
    for room in interior:
        parts = room.dim_str.split(" X ")
        x = 10
        cv2.putText(timg, room.name, (x + 4, y + 18), DIM_FONT, 0.38, (40, 40, 40), 1, cv2.LINE_AA)
        x += cols[0]
        cv2.putText(timg, parts[0] if len(parts) >= 1 else "", (x + 4, y + 18),
                     DIM_FONT, 0.38, (40, 40, 40), 1, cv2.LINE_AA)
        x += cols[1]
        cv2.putText(timg, parts[1] if len(parts) >= 2 else "", (x + 4, y + 18),
                     DIM_FONT, 0.38, (40, 40, 40), 1, cv2.LINE_AA)
        x += cols[2]
        cv2.putText(timg, f"{room.area_sqft:.1f}", (x + 4, y + 18),
                     DIM_FONT, 0.38, (40, 40, 40), 1, cv2.LINE_AA)
        total += room.area_sqft
        y += row_h
        cv2.line(timg, (10, y), (tw - 10, y), (200, 200, 200), 1)

    y += 4
    cv2.line(timg, (10, y), (tw - 10, y), (0, 0, 0), 2)
    y += 4
    cv2.putText(timg, "TOTAL INTERIOR", (14, y + 18), DIM_FONT, 0.42, (0, 0, 0), 1, cv2.LINE_AA)
    x = 10 + cols[0] + cols[1] + cols[2]
    cv2.putText(timg, f"{total:.1f}", (x + 4, y + 18), DIM_FONT, 0.42, (0, 0, 0), 1, cv2.LINE_AA)

    return timg


def annotate_walls(mask_path: Path, raw_json_path: Path, blueprint_path: Path,
                   output_dir: Path) -> dict:
    """Main annotation pipeline.

    Args:
        mask_path: Path to wall mask PNG.
        raw_json_path: Path to raw elements JSON.
        blueprint_path: Path to blueprint overlay PNG.
        output_dir: Output directory.

    Returns:
        Annotation result dict.
    """
    t0 = datetime.now()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load
    logger.info("Loading mask: %s", mask_path)
    mask_raw = cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE)
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.dilate(mask_raw, kernel, iterations=WALL_DILATE_ITER)

    logger.info("Loading base from PDF: inputs/test-2.pdf")
    import fitz as _fitz
    _doc = _fitz.open("inputs/test-2.pdf")
    _pix = _doc[0].get_pixmap(matrix=_fitz.Matrix(3, 3))
    _pdf_rgb = np.frombuffer(_pix.samples, dtype=np.uint8).reshape(_pix.h, _pix.w, 3).copy()
    _pdf_bgr = cv2.cvtColor(_pdf_rgb, cv2.COLOR_RGB2BGR)
    _doc.close()
    _white = np.full_like(_pdf_bgr, 255)
    img = cv2.addWeighted(_pdf_bgr, 0.30, _white, 0.70, 0)

    # Extract rooms
    logger.info("Extracting rooms")
    rooms = extract_rooms(raw_json_path)
    logger.info("Found %d rooms", len(rooms))

    # Calibrate
    ppf = calibrate_ppf(mask, rooms)

    # Find wall boundaries
    logger.info("Finding wall boundaries (band scan ±%d px)", SCAN_BAND)
    find_walls(mask, rooms, ppf)

    # Draw dimension lines
    logger.info("Drawing dimension lines")
    pl = dim_placement(rooms)

    for room in rooms:
        p = pl[room.name]
        parts = room.dim_str.split(" X ")
        w_txt = parts[0].strip() if len(parts) >= 1 else ""
        h_txt = parts[1].strip() if len(parts) >= 2 else ""

        # Horizontal dimension (width)
        if w_txt and room.wr - room.wl > 20:
            y_ref = room.wb if p["h_below"] else room.wt
            draw_dim_h(img, room.wl, room.wr, y_ref, p["h_off"], w_txt, below=p["h_below"])

        # Vertical dimension (height)
        if h_txt and room.wb - room.wt > 20:
            x_ref = room.wr if p["v_right"] else room.wl
            draw_dim_v(img, room.wt, room.wb, x_ref, p["v_off"], h_txt, right=p["v_right"])

        # Room label
        draw_room_label(img, room)

    # Summary table
    table = generate_table(rooms)
    th, tw_t = table.shape[:2]
    fh, fw = img.shape[:2]

    # Composite: floor plan + table
    comp_h = fh + th + 40
    comp = np.ones((comp_h, fw, 3), dtype=np.uint8) * 255
    comp[:fh, :fw] = img
    tx = (fw - tw_t) // 2
    ty = fh + 20
    sw = min(tw_t, fw - tx)
    sh = min(th, comp_h - ty)
    comp[ty:ty + sh, tx:tx + sw] = table[:sh, :sw]

    # Save PNG
    png_path = output_dir / "test-2_annotated_walls.png"
    cv2.imwrite(str(png_path), comp)
    logger.info("Saved: %s", png_path)

    # Save PDF
    pdf_path = output_dir / "test-2_annotated_walls.pdf"
    doc = fitz.open()

    # Page 1: floor plan
    p1 = doc.new_page(width=PAGE_WIDTH_PT, height=PAGE_HEIGHT_PT)
    _, ib = cv2.imencode(".png", img)
    p1.insert_image(fitz.Rect(0, 0, PAGE_WIDTH_PT, PAGE_HEIGHT_PT), stream=ib.tobytes())

    # Page 2: schedule
    p2_w = max(PAGE_WIDTH_PT, tw_t + 40)
    p2_h = max(300, th + 40)
    p2 = doc.new_page(width=p2_w, height=p2_h)
    _, tb = cv2.imencode(".png", table)
    p2.insert_image(fitz.Rect(20, 20, 20 + tw_t, 20 + th), stream=tb.tobytes())

    doc.save(str(pdf_path))
    doc.close()
    logger.info("Saved: %s", pdf_path)

    # Save JSON
    elapsed = (datetime.now() - t0).total_seconds()
    result = {
        "source": mask_path.name,
        "blueprint": blueprint_path.name,
        "page_size_pt": {"width": PAGE_WIDTH_PT, "height": PAGE_HEIGHT_PT},
        "scale_factor": SCALE_FACTOR,
        "pixels_per_foot": round(ppf, 2),
        "rooms": [],
        "total_interior_sqft": 0.0,
        "processing_time_s": round(elapsed, 2),
    }

    total = 0.0
    for room in rooms:
        parts = room.dim_str.split(" X ")
        rd = {
            "name": room.name,
            "dimensions": room.dim_str,
            "width_ft": room.w_ft,
            "height_ft": room.h_ft,
            "area_sqft": room.area_sqft,
            "label_pt": {"x": round(room.cx_pt, 1), "y": round(room.cy_pt, 1)},
            "walls_px": {"left": room.wl, "right": room.wr, "top": room.wt, "bottom": room.wb},
            "span_px": {"width": room.wr - room.wl, "height": room.wb - room.wt},
            "scan_valid": room.scan_valid,
        }
        result["rooms"].append(rd)
        if room.name != "PATIO":
            total += room.area_sqft

    result["total_interior_sqft"] = round(total, 1)

    json_path = output_dir / "test-2_annotated_walls.json"
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)
    logger.info("Saved: %s", json_path)

    logger.info("Done in %.1fs — %d rooms annotated", elapsed, len(rooms))
    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Annotate wall structure with dimension lines")
    parser.add_argument("mask", type=Path, help="Path to wall mask PNG")
    parser.add_argument("--raw-json", type=Path, default=Path("inputs/test-2_raw_elements.json"))
    parser.add_argument("--blueprint", type=Path, default=Path("inputs/test-2_blueprint_20260402_181712.png"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    annotate_walls(args.mask, args.raw_json, args.blueprint, args.output_dir)


if __name__ == "__main__":
    main()
