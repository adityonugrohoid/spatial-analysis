"""Extract and classify all elements from a residential floor plan PDF.

Two classification layers:
  1. Semantic: walls, doors, windows, fixtures, rooms, annotations, watermark
  2. Raw PDF element types: lines, curves, rectangles, quads, fills, text

Outputs structured JSON and per-group debug PNG visualizations for both.
"""

import argparse
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import base64
import fitz  # PyMuPDF
import numpy as np
import cv2

logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── PDF unit conversion ────────────────────────────────────────────────
PT_TO_MM = 25.4 / 72  # 1 pt = 0.3528 mm (ISO 32000)

# ── Classification thresholds ──────────────────────────────────────────
WALL_WIDTH_MIN = 2.0          # walls are drawn with width >= 2.0 pt
FIXTURE_WIDTH_RANGE = (0.8, 2.0)  # medium-weight lines for doors/fixtures/windows
THIN_WIDTH_MAX = 0.8          # thin detail lines

# ── Window label patterns ──────────────────────────────────────────────
WINDOW_LABELS = {"3040SH", "2030SH", "3050SH", "2040SH", "4040SH"}

# ── Room label detection: text with dimension pattern ──────────────────
ROOM_NAMES = {
    "PATIO", "MASTER BDRM", "BEDROOM", "BREAKFAST NOOK", "LIVING ROOM",
    "BATHROOM", "KITCHEN", "CLOSET", "MASTER BATH", "GARAGE",
    "DINING ROOM", "FOYER", "LAUNDRY", "OFFICE", "ENTRY",
}

# ── Fixture / appliance labels ─────────────────────────────────────────
FIXTURE_LABELS = {
    "PANTRY", "RANGE", "WALL OVEN", "UTILITY SINK", "REFRIGERATOR",
    "SHOWER", "LINEN", "DW", "DRAWERS", "FREEZER", "WASHER", "WH",
    "DRYER", "SEAT", "BUILT-IN", "BOOKSHELF",
}

# ── Annotation labels ─────────────────────────────────────────────────
ANNOTATION_LABELS = {"HALF WALL", "ARCHED OPENING"}

# ── Render settings ────────────────────────────────────────────────────
RENDER_DPI = 200

# ── Group colors for debug PNGs (BGR for OpenCV) ──────────────────────
# Semantic classification colors (BGR)
GROUP_COLORS = {
    "walls":       (0, 0, 200),       # red
    "doors":       (200, 120, 0),     # blue-ish
    "windows":     (0, 180, 0),       # green
    "fixtures":    (200, 0, 200),     # magenta
    "rooms":       (180, 120, 0),     # teal
    "annotations": (0, 140, 255),     # orange
    "watermark":   (180, 180, 180),   # grey
}

# Raw PDF element type colors (BGR)
RAW_GROUP_COLORS = {
    "lines":      (0, 200, 255),      # yellow
    "curves":     (0, 140, 255),      # orange
    "rectangles": (255, 100, 0),      # blue
    "quads":      (255, 0, 200),      # pink
    "fills":      (0, 200, 0),        # green
    "text":       (0, 180, 255),      # gold
}


@dataclass
class ExtractedElement:
    """A single extracted element with classification metadata."""
    type: str           # line, area, counting, arc, curve
    color: list[int]    # [r, g, b] 0-255
    points: list[dict[str, float]]
    group: str = ""
    label: str | None = None
    width: float = 0.0
    filled: bool = False


def _rgb_float_to_int(color: tuple[float, ...] | None) -> list[int]:
    """Convert PyMuPDF 0-1 float RGB to 0-255 int RGB.

    Args:
        color: Tuple of floats in [0, 1], or None.

    Returns:
        List of ints in [0, 255].
    """
    if color is None:
        return [0, 0, 0]
    return [int(round(c * 255)) for c in color]


def _is_grey_watermark(fill: tuple[float, ...] | None) -> bool:
    """Check if a fill color is the grey watermark.

    Args:
        fill: Fill color tuple or None.

    Returns:
        True if it matches the SAMPLE watermark grey.
    """
    if fill is None:
        return False
    if len(fill) == 3:
        return all(0.5 < c < 0.9 for c in fill)
    return False


def _classify_text(text: str) -> str:
    """Classify a text span into a group.

    Args:
        text: The text content.

    Returns:
        Group name: 'windows', 'rooms', 'fixtures', 'annotations', or 'annotations'.
    """
    upper = text.strip().upper()
    if upper in WINDOW_LABELS:
        return "windows"
    if upper in FIXTURE_LABELS:
        return "fixtures"
    for ann in ANNOTATION_LABELS:
        if ann in upper:
            return "annotations"
    # Check if it's a room name
    for room in ROOM_NAMES:
        if room in upper:
            return "rooms"
    # Dimension patterns like 12'0" X 11'6"
    if "'" in upper and "X" in upper:
        return "rooms"
    return "annotations"


def _classify_drawing(d: dict) -> str:
    """Classify a drawing item into a group based on width, fill, shape.

    Args:
        d: PyMuPDF drawing dict.

    Returns:
        Group name.
    """
    fill = d.get("fill")
    width = d.get("width") or 0
    color = d.get("color")

    # White fills are masking shapes (part of door/fixture rendering)
    if fill == (1.0, 1.0, 1.0) or fill == (1, 1, 1):
        return "doors"

    # Grey watermark
    if _is_grey_watermark(fill):
        return "watermark"

    # Walls: thick black lines
    if width >= WALL_WIDTH_MIN:
        return "walls"

    # Medium weight lines: classify by shape
    if FIXTURE_WIDTH_RANGE[0] <= width < FIXTURE_WIDTH_RANGE[1]:
        items = d["items"]
        ops = [item[0] for item in items]

        # Arcs (curves) are typically doors
        if "c" in ops and len(items) <= 4:
            return "doors"

        # Small rectangles or short lines near fixtures
        rect = d["rect"]
        w = rect.width
        h = rect.height
        # Very small shapes are likely fixture details
        if w < 15 and h < 15:
            return "fixtures"

        return "fixtures"

    # Thin lines
    if 0 < width < FIXTURE_WIDTH_RANGE[0]:
        return "fixtures"

    return "fixtures"


def _drawing_to_points(d: dict) -> list[dict[str, float]]:
    """Extract coordinate points from a drawing item.

    Args:
        d: PyMuPDF drawing dict.

    Returns:
        List of {x, y} point dicts.
    """
    points = []
    seen = set()
    for item in d["items"]:
        op = item[0]
        if op == "l":  # line
            for pt in [item[1], item[2]]:
                key = (round(pt.x, 1), round(pt.y, 1))
                if key not in seen:
                    points.append({"x": round(pt.x, 2), "y": round(pt.y, 2)})
                    seen.add(key)
        elif op == "c":  # cubic bezier
            for pt in [item[1], item[2], item[3], item[4]]:
                key = (round(pt.x, 1), round(pt.y, 1))
                if key not in seen:
                    points.append({"x": round(pt.x, 2), "y": round(pt.y, 2)})
                    seen.add(key)
        elif op == "re":  # rectangle
            rect = item[1]
            for corner in [(rect.x0, rect.y0), (rect.x1, rect.y0),
                           (rect.x1, rect.y1), (rect.x0, rect.y1)]:
                key = (round(corner[0], 1), round(corner[1], 1))
                if key not in seen:
                    points.append({"x": round(corner[0], 2), "y": round(corner[1], 2)})
                    seen.add(key)
    return points


def _element_type_from_drawing(d: dict) -> str:
    """Determine the element type from drawing operations.

    Args:
        d: PyMuPDF drawing dict.

    Returns:
        Type string: 'line', 'area', or 'curve'.
    """
    ops = [item[0] for item in d["items"]]
    fill = d.get("fill")

    if fill is not None:
        return "area"
    if "c" in ops:
        return "curve"
    return "line"


def extract_and_classify(pdf_path: Path) -> dict:
    """Extract all elements from a floor plan PDF and classify into groups.

    Args:
        pdf_path: Path to the input PDF.

    Returns:
        Dict with source metadata and classified groups.
    """
    logger.info("Opening %s", pdf_path)
    start = time.time()
    doc = fitz.open(str(pdf_path))
    page = doc[0]

    page_w, page_h = page.rect.width, page.rect.height
    logger.info("Page size: %.0f x %.0f pts (%.1f x %.1f mm)",
                page_w, page_h, page_w * PT_TO_MM, page_h * PT_TO_MM)

    # ── Collect all elements ────────────────────────────────────────
    groups: dict[str, list[dict]] = {
        "walls": [], "doors": [], "windows": [], "fixtures": [],
        "rooms": [], "annotations": [], "watermark": [],
    }

    # Process drawings
    drawings = page.get_drawings()
    logger.info("Processing %d drawing items", len(drawings))

    for d in drawings:
        group = _classify_drawing(d)
        points = _drawing_to_points(d)
        if not points:
            continue

        elem = {
            "type": _element_type_from_drawing(d),
            "color": _rgb_float_to_int(d.get("color")),
            "points": points,
        }
        if d.get("fill") is not None:
            elem["fill"] = _rgb_float_to_int(d["fill"])
        groups[group].append(elem)

    # Process text
    text_dict = page.get_text("dict")
    logger.info("Processing text blocks")

    # Aggregate multi-line text blocks (room names split across lines)
    for block in text_dict.get("blocks", []):
        if block["type"] != 0:
            continue

        lines_text = []
        block_color = 0
        block_size = 0
        for line in block["lines"]:
            for span in line["spans"]:
                t = span["text"].strip()
                if t:
                    lines_text.append(t)
                    block_color = span["color"]
                    block_size = max(block_size, span["size"])

        if not lines_text:
            continue

        full_text = " ".join(lines_text)
        bbox = block["bbox"]

        # Classify each unique text
        # Handle window labels individually (multiple 3040SH in one block)
        window_count = sum(1 for t in lines_text if t.strip().upper() in WINDOW_LABELS)
        non_window = [t for t in lines_text if t.strip().upper() not in WINDOW_LABELS]

        # Add window labels
        for _ in range(window_count):
            r = (block_color >> 16) & 0xFF
            g = (block_color >> 8) & 0xFF
            b = block_color & 0xFF
            groups["windows"].append({
                "type": "counting",
                "color": [r, g, b],
                "points": [
                    {"x": round(bbox[0], 2), "y": round(bbox[1], 2)},
                    {"x": round(bbox[2], 2), "y": round(bbox[3], 2)},
                ],
                "label": lines_text[0] if window_count == len(lines_text) else "3040SH",
            })

        # Process non-window text
        if non_window:
            combined = " ".join(non_window)
            group = _classify_text(combined)
            r = (block_color >> 16) & 0xFF
            g = (block_color >> 8) & 0xFF
            b = block_color & 0xFF
            elem = {
                "type": "counting",
                "color": [r, g, b],
                "points": [
                    {"x": round(bbox[0], 2), "y": round(bbox[1], 2)},
                    {"x": round(bbox[2], 2), "y": round(bbox[3], 2)},
                ],
                "label": combined,
            }
            groups[group].append(elem)

    doc.close()
    elapsed = time.time() - start

    # Build output
    result = {
        "source": str(pdf_path),
        "page_size": {"width": round(page_w, 2), "height": round(page_h, 2), "unit": "pt"},
        "groups": [],
    }

    for group_name, elements in groups.items():
        if elements:
            result["groups"].append({
                "group": group_name,
                "elements": elements,
            })
            logger.info("  %s: %d elements", group_name, len(elements))

    logger.info("Extraction complete in %.2fs — %d groups, %d total elements",
                elapsed, len(result["groups"]),
                sum(len(g["elements"]) for g in result["groups"]))

    return result


def extract_raw_types(pdf_path: Path) -> dict:
    """Extract all elements grouped by raw PDF element type.

    Groups by the fundamental PDF drawing operations without any semantic
    interpretation. This is a 1:1 mapping of what the PDF file stores.

    PDF drawing operations (ISO 32000 content stream operators):
      - 'l' (lineTo): straight line segments between two points
      - 'c' (curveTo): cubic Bezier curves with 4 control points
      - 're' (rectangle): axis-aligned rectangles from origin + width/height
      - 'qu' (quadrilateral): four-sided filled shapes
      - fill: any path with a non-null fill color (regardless of op type)
      - text: character glyphs placed via text-showing operators (Tj, TJ)

    Args:
        pdf_path: Path to the input PDF.

    Returns:
        Dict with source metadata and raw-type groups.
    """
    logger.info("[raw] Opening %s", pdf_path)
    start = time.time()
    doc = fitz.open(str(pdf_path))
    page = doc[0]

    # Normalize rotation: store original, then derotate so all coordinate
    # spaces (drawings, text, pixmap) align without manual transforms.
    original_rotation = page.rotation
    if original_rotation != 0:
        logger.info("[raw] Page rotation detected: %d° — normalizing to 0°", original_rotation)
        page.set_rotation(0)

    page_w, page_h = page.rect.width, page.rect.height

    raw_groups: dict[str, list[dict]] = {
        "lines": [],
        "curves": [],
        "rectangles": [],
        "quads": [],
        "fills": [],
        "text": [],
        "images": [],
        "tables": [],
    }

    drawings = page.get_drawings()
    logger.info("[raw] Processing %d drawing items", len(drawings))

    page_area = page_w * page_h

    for d in drawings:
        color = _rgb_float_to_int(d.get("color"))
        fill = d.get("fill")
        has_fill = fill is not None
        fill_rgb = _rgb_float_to_int(fill) if has_fill else None
        width = d.get("width") or 0

        # If shape has a fill, add to fills group (skip page-wide backgrounds)
        if has_fill:
            # Filter out fills covering >90% of page area (background layers)
            r = d.get("rect")
            if r and (r.width * r.height) > page_area * 0.9:
                logger.debug("[raw] Skipping page-wide fill: %s", r)
                continue
            points = _drawing_to_points(d)
            if points:
                elem = {
                    "type": "fill",
                    "color": color,
                    "fill": fill_rgb,
                    "points": points,
                    "width": round(width, 2),
                }
                raw_groups["fills"].append(elem)

        # Also classify each sub-operation into its native type
        for item in d["items"]:
            op = item[0]

            if op == "l":
                p1, p2 = item[1], item[2]
                raw_groups["lines"].append({
                    "type": "line",
                    "color": color,
                    "points": [
                        {"x": round(p1.x, 2), "y": round(p1.y, 2)},
                        {"x": round(p2.x, 2), "y": round(p2.y, 2)},
                    ],
                    "width": round(width, 2),
                })

            elif op == "c":
                p1, p2, p3, p4 = item[1], item[2], item[3], item[4]
                raw_groups["curves"].append({
                    "type": "curve",
                    "color": color,
                    "points": [
                        {"x": round(p1.x, 2), "y": round(p1.y, 2)},
                        {"x": round(p2.x, 2), "y": round(p2.y, 2)},
                        {"x": round(p3.x, 2), "y": round(p3.y, 2)},
                        {"x": round(p4.x, 2), "y": round(p4.y, 2)},
                    ],
                    "width": round(width, 2),
                })

            elif op == "re":
                # Only add stroked rectangles; fill-only rects are
                # already captured in the fills group
                if d.get("color") is not None:
                    rect = item[1]
                    raw_groups["rectangles"].append({
                        "type": "rectangle",
                        "color": color,
                        "points": [
                            {"x": round(rect.x0, 2), "y": round(rect.y0, 2)},
                            {"x": round(rect.x1, 2), "y": round(rect.y0, 2)},
                            {"x": round(rect.x1, 2), "y": round(rect.y1, 2)},
                            {"x": round(rect.x0, 2), "y": round(rect.y1, 2)},
                        ],
                        "width": round(width, 2),
                    })

            elif op == "qu":
                quad = item[1]
                raw_groups["quads"].append({
                    "type": "quad",
                    "color": color,
                    "points": [
                        {"x": round(quad.ul.x, 2), "y": round(quad.ul.y, 2)},
                        {"x": round(quad.ur.x, 2), "y": round(quad.ur.y, 2)},
                        {"x": round(quad.lr.x, 2), "y": round(quad.lr.y, 2)},
                        {"x": round(quad.ll.x, 2), "y": round(quad.ll.y, 2)},
                    ],
                    "width": round(width, 2),
                })

    # Extract text and images from page content
    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block["type"] == 0:
            # Text block
            for line in block["lines"]:
                line_dir = line.get("dir", (1.0, 0.0))
                text_angle = round(math.degrees(math.atan2(line_dir[1], line_dir[0])), 1)
                for span in line["spans"]:
                    t = span["text"].strip()
                    if not t:
                        continue
                    bbox = span["bbox"]
                    c = span["color"]
                    r = (c >> 16) & 0xFF
                    g = (c >> 8) & 0xFF
                    b = c & 0xFF
                    origin = span.get("origin", (bbox[0], bbox[3]))
                    raw_groups["text"].append({
                        "type": "text",
                        "color": [r, g, b],
                        "points": [
                            {"x": round(bbox[0], 2), "y": round(bbox[1], 2)},
                            {"x": round(bbox[2], 2), "y": round(bbox[3], 2)},
                        ],
                        "label": t,
                        "font_size": round(span["size"], 1),
                        "text_dir": [round(line_dir[0], 4), round(line_dir[1], 4)],
                        "text_angle": text_angle,
                        "origin": {"x": round(origin[0], 2), "y": round(origin[1], 2)},
                    })
        elif block["type"] == 1:
            # Image block — extract actual image bytes as base64
            bbox = block["bbox"]
            img_w = block.get("width", 0)
            img_h = block.get("height", 0)
            img_bytes = block.get("image", b"")
            img_ext = block.get("ext", "png")
            mime = {"png": "image/png", "jpeg": "image/jpeg",
                    "jpg": "image/jpeg", "jxr": "image/jxr",
                    "jpx": "image/jpx", "bmp": "image/bmp"}.get(img_ext, f"image/{img_ext}")
            img_data_uri = f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}" if img_bytes else ""
            # Decompose CTM into rotation + flip
            # CTM = (a, b, c, d, e, f): det(a*d - b*c) < 0 means reflection
            ctm = block.get("transform", (1, 0, 0, 1, 0, 0))
            a, b, c, d = ctm[0], ctm[1], ctm[2], ctm[3]
            det = a * d - b * c
            if det < 0:
                # Reflection: factor out h-flip, compute remaining rotation
                img_rotation = round(math.degrees(math.atan2(-b, -a)), 1)
                img_flip_h = True
            else:
                img_rotation = round(math.degrees(math.atan2(b, a)), 1)
                img_flip_h = False
            raw_groups["images"].append({
                "type": "image",
                "color": [128, 128, 128],
                "points": [
                    {"x": round(bbox[0], 2), "y": round(bbox[1], 2)},
                    {"x": round(bbox[2], 2), "y": round(bbox[3], 2)},
                ],
                "label": f"IMG {img_w}x{img_h}",
                "img_width": img_w,
                "img_height": img_h,
                "image_data": img_data_uri,
                "img_rotation": img_rotation,
                "img_flip_h": img_flip_h,
            })

    # Extract tables
    try:
        tables = page.find_tables()
        for t in tables.tables:
            bbox = t.bbox
            raw_groups["tables"].append({
                "type": "table",
                "color": [64, 128, 64],
                "points": [
                    {"x": round(bbox[0], 2), "y": round(bbox[1], 2)},
                    {"x": round(bbox[2], 2), "y": round(bbox[3], 2)},
                ],
                "label": f"TABLE {t.row_count}x{t.col_count}",
                "rows": t.row_count,
                "cols": t.col_count,
            })
    except Exception:
        pass  # find_tables not available in older PyMuPDF

    doc.close()
    elapsed = time.time() - start

    result = {
        "source": str(pdf_path),
        "page_size": {"width": round(page_w, 2), "height": round(page_h, 2), "unit": "pt"},
        "page_rotation": original_rotation,
        "classification": "raw_pdf_element_types",
        "groups": [],
    }

    for group_name, elements in raw_groups.items():
        if elements:
            result["groups"].append({
                "group": group_name,
                "elements": elements,
            })
            logger.info("  [raw] %s: %d elements", group_name, len(elements))

    logger.info("[raw] Extraction complete in %.2fs — %d total elements",
                elapsed, sum(len(g["elements"]) for g in result["groups"]))

    return result


def generate_raw_debug_images(pdf_path: Path, result: dict, output_dir: Path) -> None:
    """Generate per-group debug PNGs for raw PDF element type classification.

    Args:
        pdf_path: Path to the source PDF.
        result: Raw extraction result dict.
        output_dir: Directory to write debug PNGs.
    """
    base_img = _render_page_base(pdf_path)
    img_h, img_w = base_img.shape[:2]
    page_w = result["page_size"]["width"]
    page_h = result["page_size"]["height"]

    for group_data in result["groups"]:
        group_name = group_data["group"]
        elements = group_data["elements"]
        color = RAW_GROUP_COLORS.get(group_name, (255, 255, 0))

        overlay = (base_img * 0.3).astype(np.uint8)

        for elem in elements:
            pts = elem["points"]
            if len(pts) < 2:
                continue

            px_pts = [_scale_pt_to_px(p["x"], p["y"], page_w, page_h, img_w, img_h)
                      for p in pts]
            elem_type = elem.get("type", "line")

            if elem_type == "text":
                x1, y1 = px_pts[0]
                x2, y2 = px_pts[1]
                cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 1)
                label = elem.get("label", "")
                if len(label) > 30:
                    label = label[:27] + "..."
                cv2.putText(overlay, label, (x1, max(y1 - 3, 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1, cv2.LINE_AA)

            elif elem_type == "fill":
                np_pts = np.array(px_pts, dtype=np.int32)
                cv2.fillPoly(overlay, [np_pts], color=tuple(c // 4 for c in color))
                cv2.polylines(overlay, [np_pts], isClosed=True, color=color, thickness=1)

            elif elem_type == "rectangle":
                x1, y1 = px_pts[0]
                x2, y2 = px_pts[2]
                cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)

            elif elem_type == "curve":
                for i in range(len(px_pts) - 1):
                    cv2.line(overlay, px_pts[i], px_pts[i + 1], color, 2)

            elif elem_type == "quad":
                np_pts = np.array(px_pts, dtype=np.int32)
                cv2.polylines(overlay, [np_pts], isClosed=True, color=color, thickness=2)

            else:  # line
                cv2.line(overlay, px_pts[0], px_pts[1], color, 2)

        # Banner
        banner_h = 40
        cv2.rectangle(overlay, (0, 0), (img_w, banner_h), (40, 40, 40), -1)
        cv2.putText(overlay,
                    f"RAW: {group_name.upper()} ({len(elements)} elements)",
                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)

        out_path = output_dir / f"raw_debug_{group_name}.png"
        cv2.imwrite(str(out_path), overlay)
        logger.info("Saved %s", out_path)


def _render_page_base(pdf_path: Path, dpi: int = RENDER_DPI) -> np.ndarray:
    """Render the PDF page to a numpy array (BGR) for debug overlays.

    Args:
        pdf_path: Path to the PDF.
        dpi: Render resolution.

    Returns:
        BGR numpy array of the rendered page.
    """
    doc = fitz.open(str(pdf_path))
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    doc.close()
    return img


def _scale_pt_to_px(x: float, y: float, page_w: float, page_h: float,
                     img_w: int, img_h: int) -> tuple[int, int]:
    """Convert PDF point coordinates to pixel coordinates.

    Args:
        x: X in PDF points.
        y: Y in PDF points.
        page_w: Page width in points.
        page_h: Page height in points.
        img_w: Image width in pixels.
        img_h: Image height in pixels.

    Returns:
        (px_x, px_y) pixel coordinates.
    """
    return int(x / page_w * img_w), int(y / page_h * img_h)


def generate_debug_images(pdf_path: Path, result: dict, output_dir: Path) -> None:
    """Generate per-group debug PNG images with highlighted elements.

    Each group gets its own image with the base floor plan dimmed and
    group elements highlighted in color.

    Args:
        pdf_path: Path to the source PDF.
        result: Extraction result dict.
        output_dir: Directory to write debug PNGs.
    """
    base_img = _render_page_base(pdf_path)
    img_h, img_w = base_img.shape[:2]
    page_w = result["page_size"]["width"]
    page_h = result["page_size"]["height"]

    for group_data in result["groups"]:
        group_name = group_data["group"]
        elements = group_data["elements"]
        color = GROUP_COLORS.get(group_name, (255, 255, 0))

        # Create dimmed background
        overlay = (base_img * 0.3).astype(np.uint8)

        for elem in elements:
            pts = elem["points"]
            if len(pts) < 2:
                continue

            px_pts = [_scale_pt_to_px(p["x"], p["y"], page_w, page_h, img_w, img_h)
                      for p in pts]

            elem_type = elem.get("type", "line")

            if elem_type == "counting" and "label" in elem:
                # Draw bounding box and label
                x1, y1 = px_pts[0]
                x2, y2 = px_pts[1]
                cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)
                label = elem["label"]
                font_scale = 0.4
                cv2.putText(overlay, label, (x1, max(y1 - 4, 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, 1,
                            cv2.LINE_AA)

            elif elem_type == "area":
                # Draw filled polygon
                np_pts = np.array(px_pts, dtype=np.int32)
                cv2.fillPoly(overlay, [np_pts], color=tuple(c // 3 for c in color))
                cv2.polylines(overlay, [np_pts], isClosed=True, color=color, thickness=2)

            elif elem_type == "curve":
                # Draw as connected line segments (approximating curves)
                for i in range(len(px_pts) - 1):
                    cv2.line(overlay, px_pts[i], px_pts[i + 1], color, 2)

            else:
                # Draw lines
                thickness = 3 if group_name == "walls" else 2
                for i in range(len(px_pts) - 1):
                    cv2.line(overlay, px_pts[i], px_pts[i + 1], color, thickness)

        # Add group label banner
        banner_h = 40
        cv2.rectangle(overlay, (0, 0), (img_w, banner_h), (40, 40, 40), -1)
        cv2.putText(overlay, f"{group_name.upper()} ({len(elements)} elements)",
                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)

        out_path = output_dir / f"debug_{group_name}.png"
        cv2.imwrite(str(out_path), overlay)
        logger.info("Saved %s", out_path)


def main() -> None:
    """CLI entry point for floor plan element extraction."""
    parser = argparse.ArgumentParser(
        description="Extract and classify floor plan elements from a PDF."
    )
    parser.add_argument("input", type=Path, help="Path to the input PDF file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output JSON path (default: outputs/<stem>_elements.json)"
    )
    parser.add_argument(
        "--debug-dir", type=Path, default=None,
        help="Directory for debug PNGs (default: outputs/)"
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input file not found: %s", args.input)
        return

    output_path = args.output or Path("outputs") / f"{args.input.stem}_elements.json"
    debug_dir = args.debug_dir or Path("outputs")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    # ── Semantic classification ─────────────────────────────────────
    result = extract_and_classify(args.input)
    output_path.write_text(json.dumps(result, indent=2))
    logger.info("Semantic JSON: %s", output_path)
    generate_debug_images(args.input, result, debug_dir)

    # ── Raw PDF element type classification ──────────────────────────
    raw_result = extract_raw_types(args.input)
    raw_output = output_path.parent / f"{args.input.stem}_raw_elements.json"
    raw_output.write_text(json.dumps(raw_result, indent=2))
    logger.info("Raw JSON: %s", raw_output)
    generate_raw_debug_images(args.input, raw_result, debug_dir)

    logger.info("Done.")


if __name__ == "__main__":
    main()
