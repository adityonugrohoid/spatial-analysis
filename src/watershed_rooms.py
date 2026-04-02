"""Room segmentation and annotation pipeline using watershed algorithm.

Takes a cleaned wall-only mask PNG and text seed points,
runs morphological closing + watershed to segment rooms,
calibrates pixel areas against a reference room, and produces
annotated outputs with room labels and computed areas.

Usage:
    python src/watershed_rooms.py inputs/test-2_mask_20260402_181715.png
"""

import argparse
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import cv2
import numpy as np
from skimage.segmentation import watershed as skimage_watershed

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

WALL_DILATE_ITERATIONS = 2  # thicken 1-2px walls to ~5-6px
SEED_RADIUS = 5
BACKGROUND_LABEL = 17
CORNER_INSET = 20
BLEND_ALPHA = 0.4  # color overlay opacity

PASTEL_COLORS = [
    (180, 220, 255),  # light blue
    (255, 200, 180),  # peach
    (200, 255, 200),  # mint
    (255, 230, 180),  # light orange
    (220, 200, 255),  # lavender
    (255, 200, 220),  # pink
    (200, 240, 240),  # light cyan
    (240, 240, 180),  # light yellow
    (220, 255, 220),  # pale green
    (255, 220, 255),  # light magenta
    (200, 220, 240),  # steel blue
    (240, 210, 200),  # warm gray
    (210, 240, 210),  # sage
    (255, 240, 200),  # cream
    (230, 210, 240),  # lilac
    (200, 230, 220),  # teal tint
    (180, 180, 180),  # background gray
]


# ── Data classes ─────────────────────────────────────────────

@dataclass
class Seed:
    """A room seed point with metadata."""
    label: str
    dimensions: str
    width_ft: float
    height_ft: float
    area_sqft: float
    x_pt: float
    y_pt: float
    x_px: int  # recomputed for actual mask size
    y_px: int  # recomputed for actual mask size


@dataclass
class RoomResult:
    """Computed room segmentation result."""
    name: str
    stated_dimensions: str
    stated_sqft: float
    computed_pixels: int
    computed_sqft: float
    accuracy_pct: float


@dataclass
class Calibration:
    """Pixel-to-sqft calibration from reference room."""
    reference_room: str
    reference_sqft: float
    reference_pixels: int
    sqft_per_pixel: float


# ── Pipeline functions ───────────────────────────────────────

def load_and_prepare_mask(mask_path: Path) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Load wall mask and convert to clean binary.

    Args:
        mask_path: Path to the wall mask PNG (white walls on black).

    Returns:
        Tuple of (binary wall mask with white=walls, grayscale original, width, height).
    """
    mask_bgr = cv2.imread(str(mask_path))
    if mask_bgr is None:
        raise FileNotFoundError(f"Cannot read mask: {mask_path}")

    h, w = mask_bgr.shape[:2]
    logger.info(f"Loaded mask: {w}x{h}")

    if len(mask_bgr.shape) == 3:
        gray = cv2.cvtColor(mask_bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = mask_bgr

    _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    logger.info(f"Wall pixels (white): {np.count_nonzero(binary)}, room pixels (black): {np.count_nonzero(binary == 0)}")

    return binary, gray, w, h


def prepare_wall_mask(wall_mask: np.ndarray, output_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """Dilate walls to thicken them and create room mask.

    Walls in the input are white (255) and only 1-2px wide. Dilation
    thickens them to create more robust barriers for watershed.

    No morphological close is applied because doorway gaps (~77px at 3x
    resolution) are far too wide to bridge without merging rooms.

    Args:
        wall_mask: Binary mask with white walls on black background.
        output_dir: Directory for debug images.

    Returns:
        Tuple of (dilated wall mask, room mask where rooms=True).
    """
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    thickened = cv2.dilate(wall_mask, dilate_kernel, iterations=WALL_DILATE_ITERATIONS)
    logger.info(f"Dilated walls ({WALL_DILATE_ITERATIONS} iters): {np.count_nonzero(wall_mask)} -> {np.count_nonzero(thickened)} px")

    cv2.imwrite(str(output_dir / "debug_walls_dilated.png"), thickened)

    room_mask = thickened == 0  # True where rooms are
    return thickened, room_mask


def load_seeds(seeds_path: Path, mask_w: int, mask_h: int, page_w: float, page_h: float) -> list[Seed]:
    """Load seed points and rescale to actual mask dimensions.

    Args:
        seeds_path: Path to the watershed seeds JSON.
        mask_w: Actual mask width in pixels.
        mask_h: Actual mask height in pixels.
        page_w: Page width in points.
        page_h: Page height in points.

    Returns:
        List of Seed objects with pixel coordinates matching the mask.
    """
    with open(seeds_path) as f:
        data = json.load(f)

    scale_x = mask_w / page_w
    scale_y = mask_h / page_h
    logger.info(f"Seed coordinate scaling: {scale_x:.4f}x, {scale_y:.4f}x (page {page_w}x{page_h} -> mask {mask_w}x{mask_h})")

    seeds = []
    for s in data["seeds"]:
        x_px = int(round(s["x_pt"] * scale_x))
        y_px = int(round(s["y_pt"] * scale_y))
        x_px = max(0, min(x_px, mask_w - 1))
        y_px = max(0, min(y_px, mask_h - 1))

        seeds.append(Seed(
            label=s["label"],
            dimensions=s["dimensions"],
            width_ft=s["width_ft"],
            height_ft=s["height_ft"],
            area_sqft=s["area_sqft"],
            x_pt=s["x_pt"],
            y_pt=s["y_pt"],
            x_px=x_px,
            y_px=y_px,
        ))
        logger.debug(f"  {s['label']}: pt({s['x_pt']:.1f}, {s['y_pt']:.1f}) -> px({x_px}, {y_px})")

    return seeds


def create_markers(
    room_mask: np.ndarray,
    seeds: list[Seed],
    mask_w: int,
    mask_h: int,
    output_dir: Path,
) -> np.ndarray:
    """Create watershed markers from seed points.

    Args:
        room_mask: Boolean mask where True = room pixel.
        seeds: List of room seed points.
        mask_w: Mask width.
        mask_h: Mask height.
        output_dir: Directory for debug images.

    Returns:
        int32 marker array with labeled seed regions.
    """
    markers = np.zeros((mask_h, mask_w), dtype=np.int32)

    for i, seed in enumerate(seeds):
        label_id = i + 1
        x, y = seed.x_px, seed.y_px

        if not room_mask[y, x]:
            logger.warning(f"Seed '{seed.label}' at ({x}, {y}) lands on wall — nudging")
            x, y = nudge_seed(room_mask, x, y, mask_w, mask_h)
            seed.x_px, seed.y_px = x, y
            logger.info(f"  Nudged to ({x}, {y})")

        cv2.circle(markers, (x, y), SEED_RADIUS, int(label_id), -1)
        logger.info(f"  Marker {label_id}: {seed.label} at ({x}, {y})")

    # Dense background markers around perimeter (every 50px)
    # This prevents edge rooms from absorbing exterior space
    bg_spacing = 50
    bg_points = []
    for x in range(CORNER_INSET, mask_w - CORNER_INSET, bg_spacing):
        bg_points.append((x, CORNER_INSET))
        bg_points.append((x, mask_h - CORNER_INSET))
    for y in range(CORNER_INSET, mask_h - CORNER_INSET, bg_spacing):
        bg_points.append((CORNER_INSET, y))
        bg_points.append((mask_w - CORNER_INSET, y))
    bg_placed = 0
    for bx, by in bg_points:
        if room_mask[by, bx]:
            cv2.circle(markers, (bx, by), SEED_RADIUS, BACKGROUND_LABEL, -1)
            bg_placed += 1
    logger.info(f"  Placed {bg_placed} background seeds around perimeter")

    # Debug visualization
    marker_vis = np.zeros((mask_h, mask_w, 3), dtype=np.uint8)
    for label_id in range(1, BACKGROUND_LABEL + 1):
        color = PASTEL_COLORS[label_id - 1] if label_id - 1 < len(PASTEL_COLORS) else (255, 255, 255)
        marker_vis[markers == label_id] = color
    cv2.imwrite(str(output_dir / "debug_markers.png"), marker_vis)
    logger.info("Saved debug_markers.png")

    return markers


def nudge_seed(room_mask: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """Nudge a seed point off a wall pixel by searching outward in a spiral.

    Args:
        room_mask: Boolean mask where True = room pixel.
        x: Original x coordinate.
        y: Original y coordinate.
        w: Mask width.
        h: Mask height.

    Returns:
        New (x, y) on a room pixel.
    """
    for radius in range(1, 50):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if abs(dx) != radius and abs(dy) != radius:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and room_mask[ny, nx]:
                    return nx, ny
    return x, y


def run_watershed(
    wall_mask: np.ndarray,
    room_mask: np.ndarray,
    markers: np.ndarray,
    blueprint_path: Path,
    output_dir: Path,
) -> np.ndarray:
    """Run watershed using blueprint gradient as landscape, boosted at walls.

    The blueprint image contains walls, fixtures, annotations, and hatching
    that create edges at room boundaries. The gradient of the blueprint is
    used as the watershed landscape, with wall pixels boosted to maximum
    to ensure walls act as strong ridges.

    Args:
        wall_mask: Binary mask with white walls (255).
        room_mask: Boolean mask where True = room pixel.
        markers: int32 marker array with seed labels.
        blueprint_path: Path to blueprint overlay PNG.
        output_dir: Directory for debug images.

    Returns:
        Labeled array (each pixel = label ID, boundaries included).
    """
    h, w = wall_mask.shape[:2]

    # Load blueprint and compute gradient
    blueprint = cv2.imread(str(blueprint_path))
    if blueprint is None or blueprint.shape[:2] != (h, w):
        logger.warning("Blueprint unavailable, falling back to flat landscape")
        landscape = np.zeros((h, w), dtype=np.uint8)
    else:
        gray = cv2.cvtColor(blueprint, cv2.COLOR_BGR2GRAY)
        # Compute gradient: high values at edges (walls, room transitions)
        sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        gradient = np.sqrt(sobelx**2 + sobely**2)
        landscape = cv2.normalize(gradient, None, 0, 200, cv2.NORM_MINMAX).astype(np.uint8)

    # Boost wall pixels to maximum — strongest ridges
    landscape[wall_mask > 0] = 255

    cv2.imwrite(str(output_dir / "debug_landscape.png"), landscape)

    # Watershed on full image (no mask) — gradient ridges are the boundaries
    result = skimage_watershed(landscape, markers=markers)

    # Debug visualization
    vis = np.zeros((h, w, 3), dtype=np.uint8)
    for label_id in range(1, BACKGROUND_LABEL + 1):
        color = PASTEL_COLORS[label_id - 1] if label_id - 1 < len(PASTEL_COLORS) else (200, 200, 200)
        vis[result == label_id] = color
    cv2.imwrite(str(output_dir / "debug_watershed.png"), vis)
    logger.info("Saved debug_watershed.png")

    return result


def compute_areas(
    result: np.ndarray,
    seeds: list[Seed],
) -> tuple[Calibration, list[RoomResult]]:
    """Compute room areas using GARAGE as calibration reference.

    Args:
        result: Watershed result array.
        seeds: List of room seeds with stated areas.

    Returns:
        Tuple of (calibration info, list of room results).
    """
    # Count pixels per label
    pixel_counts: dict[int, int] = {}
    for i in range(len(seeds)):
        label_id = i + 1
        pixel_counts[label_id] = int(np.count_nonzero(result == label_id))

    # Find GARAGE for calibration
    garage_idx = next(i for i, s in enumerate(seeds) if s.label == "GARAGE")
    garage_label = garage_idx + 1
    garage_pixels = pixel_counts[garage_label]
    garage_sqft = seeds[garage_idx].area_sqft
    sqft_per_pixel = garage_sqft / garage_pixels if garage_pixels > 0 else 0

    calibration = Calibration(
        reference_room="GARAGE",
        reference_sqft=garage_sqft,
        reference_pixels=garage_pixels,
        sqft_per_pixel=sqft_per_pixel,
    )
    logger.info(f"Calibration: GARAGE = {garage_pixels} px = {garage_sqft} sqft -> {sqft_per_pixel:.6f} sqft/px")

    # Compute all rooms
    rooms = []
    for i, seed in enumerate(seeds):
        label_id = i + 1
        px_count = pixel_counts[label_id]
        computed_sqft = px_count * sqft_per_pixel
        accuracy = abs(computed_sqft - seed.area_sqft) / seed.area_sqft * 100 if seed.area_sqft > 0 else 0

        rooms.append(RoomResult(
            name=seed.label,
            stated_dimensions=seed.dimensions,
            stated_sqft=seed.area_sqft,
            computed_pixels=px_count,
            computed_sqft=round(computed_sqft, 2),
            accuracy_pct=round(accuracy, 1),
        ))
        logger.info(f"  {seed.label}: {px_count} px = {computed_sqft:.1f} sqft (stated {seed.area_sqft}, err {accuracy:.1f}%)")

    return calibration, rooms


def generate_annotated_image(
    result: np.ndarray,
    seeds: list[Seed],
    rooms: list[RoomResult],
    blueprint_path: Path,
    output_dir: Path,
    stem: str,
) -> None:
    """Generate annotated PNG overlaying room colors + labels on blueprint.

    Args:
        result: Watershed result array.
        seeds: Room seed points.
        rooms: Computed room results.
        blueprint_path: Path to the blueprint overlay PNG.
        output_dir: Output directory.
        stem: File stem for naming.
    """
    h, w = result.shape

    # Load blueprint
    blueprint = cv2.imread(str(blueprint_path))
    if blueprint is None:
        logger.warning(f"Blueprint not found at {blueprint_path}, using white background")
        blueprint = np.ones((h, w, 3), dtype=np.uint8) * 255
    else:
        if blueprint.shape[:2] != (h, w):
            blueprint = cv2.resize(blueprint, (w, h))

    # Create color overlay
    overlay = np.zeros((h, w, 3), dtype=np.uint8)
    for i in range(len(seeds)):
        label_id = i + 1
        color = PASTEL_COLORS[i] if i < len(PASTEL_COLORS) else (200, 200, 200)
        overlay[result == label_id] = color

    # Blend: overlay at BLEND_ALPHA, blueprint at (1 - BLEND_ALPHA)
    blended = cv2.addWeighted(overlay, BLEND_ALPHA, blueprint, 1.0 - BLEND_ALPHA, 0)

    # Detect boundaries where adjacent labels differ
    # Shift result array in 4 directions and compare
    boundaries = np.zeros((h, w), dtype=np.uint8)
    for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        shifted = np.roll(np.roll(result, dy, axis=0), dx, axis=1)
        boundaries[(result != shifted) & (result > 0) & (shifted > 0)] = 255
    boundary_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    thick_boundaries = cv2.dilate(boundaries, boundary_kernel, iterations=1)
    blended[thick_boundaries > 0] = (255, 255, 255)

    # Draw room labels
    for i, (seed, room) in enumerate(zip(seeds, rooms)):
        cx, cy = seed.x_px, seed.y_px

        # Room name (bold via drawing twice with slight offset)
        name = seed.label
        font = cv2.FONT_HERSHEY_SIMPLEX
        name_scale = 0.45
        dim_scale = 0.35
        area_scale = 0.35

        # Measure text for centering
        (nw, nh), _ = cv2.getTextSize(name, font, name_scale, 1)
        (dw, dh), _ = cv2.getTextSize(seed.dimensions, font, dim_scale, 1)
        area_text = f"{room.computed_sqft:.0f} sqft ({room.accuracy_pct:.0f}%)"
        (aw, ah), _ = cv2.getTextSize(area_text, font, area_scale, 1)

        line_gap = 4
        total_h = nh + dh + ah + line_gap * 2
        y_start = cy - total_h // 2

        # Background box for readability
        max_tw = max(nw, dw, aw) + 10
        box_x1 = cx - max_tw // 2
        box_y1 = y_start - 4
        box_x2 = cx + max_tw // 2
        box_y2 = y_start + total_h + 4
        cv2.rectangle(blended, (box_x1, box_y1), (box_x2, box_y2), (255, 255, 255), -1)
        cv2.rectangle(blended, (box_x1, box_y1), (box_x2, box_y2), (100, 100, 100), 1)

        # Room name
        nx = cx - nw // 2
        ny = y_start + nh
        cv2.putText(blended, name, (nx, ny), font, name_scale, (0, 0, 0), 2, cv2.LINE_AA)
        cv2.putText(blended, name, (nx, ny), font, name_scale, (40, 40, 40), 1, cv2.LINE_AA)

        # Dimensions
        dx = cx - dw // 2
        dy = ny + dh + line_gap
        cv2.putText(blended, seed.dimensions, (dx, dy), font, dim_scale, (80, 80, 80), 1, cv2.LINE_AA)

        # Computed area + accuracy
        ax = cx - aw // 2
        ay = dy + ah + line_gap
        err_color = (0, 120, 0) if room.accuracy_pct < 15 else (0, 0, 200)
        cv2.putText(blended, area_text, (ax, ay), font, area_scale, err_color, 1, cv2.LINE_AA)

    out_path = output_dir / f"{stem}_annotated_rooms.png"
    cv2.imwrite(str(out_path), blended)
    logger.info(f"Saved {out_path.name}")

    # Also generate annotated PDF
    try:
        import fitz
        pdf_path = output_dir / f"{stem}_annotated_rooms.pdf"
        doc = fitz.open()
        # Page size matches the image at 72 DPI
        page_w_pt = w * 72 / 150  # assume 150 DPI for reasonable PDF page size
        page_h_pt = h * 72 / 150
        page = doc.new_page(width=page_w_pt, height=page_h_pt)
        # Embed the annotated PNG
        img_rect = fitz.Rect(0, 0, page_w_pt, page_h_pt)
        page.insert_image(img_rect, filename=str(out_path))
        doc.save(str(pdf_path))
        doc.close()
        logger.info(f"Saved {pdf_path.name}")
    except ImportError:
        logger.warning("PyMuPDF not available, skipping PDF generation")
    except Exception as e:
        logger.warning(f"PDF generation failed: {e}")


def save_room_schedule(
    calibration: Calibration,
    rooms: list[RoomResult],
    output_dir: Path,
    stem: str,
) -> None:
    """Save room schedule as JSON.

    Args:
        calibration: Pixel-to-sqft calibration data.
        rooms: List of room results.
        output_dir: Output directory.
        stem: File stem for naming.
    """
    # Interior = everything except PATIO
    interior = [r for r in rooms if r.name != "PATIO"]
    total_stated = sum(r.stated_sqft for r in interior)
    total_computed = sum(r.computed_sqft for r in interior)

    # Flag high-error rooms with notes
    room_dicts = []
    for r in rooms:
        rd = asdict(r)
        if r.accuracy_pct > 100:
            rd["note"] = "high error — likely absorbing corridor/hallway space (no separating walls)"
        elif r.accuracy_pct > 40:
            rd["note"] = "moderate error — open-plan area or partial wall separation"
        room_dicts.append(rd)

    # Accuracy tiers
    good = [r for r in rooms if r.accuracy_pct <= 15]
    moderate = [r for r in rooms if 15 < r.accuracy_pct <= 50]
    poor = [r for r in rooms if r.accuracy_pct > 50]

    schedule = {
        "calibration": asdict(calibration),
        "rooms": room_dicts,
        "total_interior_sqft_stated": round(total_stated, 2),
        "total_interior_sqft_computed": round(total_computed, 2),
        "accuracy_summary": {
            "rooms_under_15pct_error": len(good),
            "rooms_15_to_50pct_error": len(moderate),
            "rooms_over_50pct_error": len(poor),
            "notes": [
                "Rooms in open-plan areas (no separating walls) absorb corridor/transition space",
                "BREAKFAST NOOK + KITCHEN + LIVING ROOM form an open great room area",
                "FOYER absorbs hallway space connecting DINING ROOM, ENTRY, and OFFICE",
            ],
        },
    }

    out_path = output_dir / f"{stem}_room_schedule.json"
    with open(out_path, "w") as f:
        json.dump(schedule, f, indent=2)
    logger.info(f"Saved {out_path.name}")


# ── Main pipeline ────────────────────────────────────────────

def run_pipeline(mask_path: Path) -> None:
    """Execute the full room segmentation pipeline.

    Args:
        mask_path: Path to the wall mask PNG.
    """
    t0 = time.time()
    stem = mask_path.stem.rsplit("_mask_", 1)[0] if "_mask_" in mask_path.stem else mask_path.stem

    # Resolve paths
    input_dir = mask_path.parent
    output_dir = mask_path.parent.parent / "outputs"
    output_dir.mkdir(exist_ok=True)

    seeds_path = input_dir / f"{stem}_watershed_seeds.json"
    blueprint_path = input_dir / f"{stem}_blueprint_20260402_181712.png"

    if not seeds_path.exists():
        raise FileNotFoundError(f"Seeds file not found: {seeds_path}")

    # Step 1: Load wall mask (white walls on black)
    logger.info("Step 1: Loading wall mask")
    wall_mask, _, mask_w, mask_h = load_and_prepare_mask(mask_path)

    # Step 2: Dilate walls for thicker barriers
    logger.info("Step 2: Wall thickening")
    dilated_walls, room_mask = prepare_wall_mask(wall_mask, output_dir)

    # Step 3: Load seeds and create markers
    logger.info("Step 3: Creating watershed markers")
    seeds = load_seeds(seeds_path, mask_w, mask_h, 792.0, 612.0)
    markers = create_markers(room_mask, seeds, mask_w, mask_h, output_dir)

    # Step 4: Watershed using blueprint gradient + wall boost
    logger.info("Step 4: Running watershed")
    result = run_watershed(dilated_walls, room_mask, markers, blueprint_path, output_dir)

    # Step 5: Compute areas
    logger.info("Step 5: Computing areas")
    calibration, rooms = compute_areas(result, seeds)

    # Step 6: Annotated output
    logger.info("Step 6: Generating annotated output")
    generate_annotated_image(result, seeds, rooms, blueprint_path, output_dir, stem)

    # Step 7: Room schedule JSON
    logger.info("Step 7: Saving room schedule")
    save_room_schedule(calibration, rooms, output_dir, stem)

    elapsed = time.time() - t0
    logger.info(f"Pipeline complete in {elapsed:.2f}s")

    # Print summary table
    print(f"\n{'Room':<20} {'Stated':>10} {'Computed':>10} {'Error':>8}")
    print("-" * 52)
    for r in rooms:
        print(f"{r.name:<20} {r.stated_sqft:>8.1f}ft² {r.computed_sqft:>8.1f}ft² {r.accuracy_pct:>6.1f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Room segmentation via watershed")
    parser.add_argument("mask", type=str, help="Path to wall mask PNG")
    args = parser.parse_args()
    run_pipeline(Path(args.mask))
