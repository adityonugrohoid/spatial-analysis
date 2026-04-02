"""Generate a PDF report with raw debug PNG mini-views and explanations.

Creates a structured overview document showing each raw PDF element type
group as a thumbnail with count, description, and PDF operator reference.
"""

import argparse
import json
import logging
import time
from pathlib import Path

import fitz  # PyMuPDF
import cv2
import numpy as np

logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Page layout (Letter landscape: 792 x 612 pts) ─────────────────────
PAGE_W = 792
PAGE_H = 612
MARGIN = 30
COLS = 3
ROWS = 2

# ── Colors ─────────────────────────────────────────────────────────────
TITLE_COLOR = (0.1, 0.1, 0.1)
HEADING_COLOR = (0.0, 0.2, 0.6)
BODY_COLOR = (0.2, 0.2, 0.2)
BORDER_COLOR = (0.5, 0.5, 0.5)
BG_COLOR = (0.96, 0.96, 0.98)
ACCENT_COLOR = (0.0, 0.3, 0.7)

# ── Raw element type metadata ──────────────────────────────────────────
RAW_GROUPS = [
    {
        "key": "lines",
        "title": "Lines",
        "operator": "l (lineTo)",
        "count_key": "lines",
        "description": (
            "Straight segments between two points. "
            "Forms walls, cabinet edges, window frames, "
            "fixture outlines, and all rectilinear geometry."
        ),
        "file": "raw_debug_lines.png",
    },
    {
        "key": "curves",
        "title": "Curves",
        "operator": "c (curveTo)",
        "count_key": "curves",
        "description": (
            "Cubic Bezier curves defined by 4 control points. "
            "Door swing arcs, toilet/sink bowl shapes, "
            "and the SAMPLE watermark letter outlines."
        ),
        "file": "raw_debug_curves.png",
    },
    {
        "key": "rectangles",
        "title": "Rectangles",
        "operator": "re (rect)",
        "count_key": "rectangles",
        "description": (
            "Axis-aligned rectangles from origin + size. "
            "Window pane fills, appliance bounding boxes, "
            "small fixture outlines, and the footer border."
        ),
        "file": "raw_debug_rectangles.png",
    },
    {
        "key": "quads",
        "title": "Quads",
        "operator": "qu (quad)",
        "count_key": "quads",
        "description": (
            "Four-sided filled polygons, not axis-aligned. "
            "Rare in this drawing: 3 small shapes in the "
            "master bath / closet area (fixture details)."
        ),
        "file": "raw_debug_quads.png",
    },
    {
        "key": "fills",
        "title": "Fills",
        "operator": "any op + fill color",
        "count_key": "fills",
        "description": (
            "Closed paths with a fill color attribute set. "
            "Grey watermark shapes, white door-gap masks, "
            "black fixture fills, and window pane shading."
        ),
        "file": "raw_debug_fills.png",
    },
    {
        "key": "text",
        "title": "Text",
        "operator": "Tj / TJ (text show)",
        "count_key": "text",
        "description": (
            "Character glyphs placed via font rendering. "
            "Room names, dimension strings, window codes, "
            "fixture labels, and the footer caption."
        ),
        "file": "raw_debug_text.png",
    },
]


def _load_counts(json_path: Path) -> dict[str, int]:
    """Load element counts per group from the raw JSON.

    Args:
        json_path: Path to the raw elements JSON file.

    Returns:
        Dict mapping group name to element count.
    """
    data = json.loads(json_path.read_text())
    counts = {}
    for g in data.get("groups", []):
        counts[g["group"]] = len(g["elements"])
    return counts


def _wrap_text(text: str, font: fitz.Font, fontsize: float, max_width: float) -> list[str]:
    """Word-wrap text to fit within a maximum width.

    Args:
        text: Input text string.
        font: PyMuPDF Font object.
        fontsize: Font size in points.
        max_width: Maximum line width in points.

    Returns:
        List of wrapped lines.
    """
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        test = f"{current_line} {word}".strip()
        tw = font.text_length(test, fontsize=fontsize)
        if tw > max_width and current_line:
            lines.append(current_line)
            current_line = word
        else:
            current_line = test

    if current_line:
        lines.append(current_line)

    return lines


def generate_report(output_path: Path, debug_dir: Path, json_path: Path) -> None:
    """Generate a PDF report with raw debug mini-views and explanations.

    Layout: 3 columns x 2 rows of cards, each containing a thumbnail
    image, element count, PDF operator, and description.

    Args:
        output_path: Path to write the output PDF.
        debug_dir: Directory containing raw_debug_*.png files.
        json_path: Path to the raw elements JSON for counts.
    """
    start = time.time()
    counts = _load_counts(json_path)
    total_elements = sum(counts.values())

    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    font = fitz.Font("helv")
    font_mono = fitz.Font("cour")

    # ── Title bar ──────────────────────────────────────────────────
    title_h = 50
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(0, 0, PAGE_W, title_h))
    shape.finish(fill=(0.12, 0.16, 0.24), color=None)
    shape.commit()

    tw = fitz.TextWriter(page.rect)
    tw.append(fitz.Point(MARGIN, 32),
              "Raw PDF Element Type Classification",
              fontsize=16, font=font)
    tw.write_text(page, color=(1, 1, 1))

    tw2 = fitz.TextWriter(page.rect)
    subtitle = f"test-2.pdf  |  {total_elements} total elements  |  6 groups  |  ISO 32000 operators"
    tw2.append(fitz.Point(PAGE_W - MARGIN - font.text_length(subtitle, fontsize=8), 32),
               subtitle, fontsize=8, font=font)
    tw2.write_text(page, color=(0.7, 0.75, 0.85))

    # ── Grid layout ────────────────────────────────────────────────
    grid_top = title_h + 12
    card_gap = 10
    usable_w = PAGE_W - 2 * MARGIN - (COLS - 1) * card_gap
    usable_h = PAGE_H - grid_top - MARGIN - (ROWS - 1) * card_gap
    card_w = usable_w / COLS
    card_h = usable_h / ROWS

    # Thumbnail takes top portion, text below
    thumb_h = card_h * 0.52
    text_area_h = card_h - thumb_h
    card_pad = 6

    for idx, group_info in enumerate(RAW_GROUPS):
        col = idx % COLS
        row = idx // COLS

        cx = MARGIN + col * (card_w + card_gap)
        cy = grid_top + row * (card_h + card_gap)

        count = counts.get(group_info["count_key"], 0)

        # ── Card background ────────────────────────────────────
        card_rect = fitz.Rect(cx, cy, cx + card_w, cy + card_h)
        shape = page.new_shape()
        shape.draw_rect(card_rect)
        shape.finish(fill=BG_COLOR, color=BORDER_COLOR, width=0.5)
        shape.commit()

        # ── Thumbnail image (downscaled JPEG for file size) ───
        img_path = debug_dir / group_info["file"]
        if img_path.exists():
            thumb_rect = fitz.Rect(
                cx + card_pad,
                cy + card_pad,
                cx + card_w - card_pad,
                cy + thumb_h - 2,
            )
            # Downscale to ~600px wide and compress as JPEG
            img = cv2.imread(str(img_path))
            scale = 600 / img.shape[1]
            small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            page.insert_image(thumb_rect, stream=buf.tobytes())

            # Thin border around thumbnail
            shape = page.new_shape()
            shape.draw_rect(thumb_rect)
            shape.finish(color=BORDER_COLOR, width=0.3)
            shape.commit()

        # ── Text area below thumbnail ──────────────────────────
        text_x = cx + card_pad
        text_top = cy + thumb_h + 2
        text_max_w = card_w - 2 * card_pad

        tw = fitz.TextWriter(page.rect)
        cursor_y = text_top

        # Title + count
        title_str = f"{group_info['title']}"
        cursor_y += 11
        tw.append(fitz.Point(text_x, cursor_y), title_str,
                  fontsize=10, font=font)

        count_str = f"  ({count})"
        title_tw = font.text_length(title_str, fontsize=10)
        tw.append(fitz.Point(text_x + title_tw, cursor_y), count_str,
                  fontsize=8, font=font)
        tw.write_text(page, color=HEADING_COLOR)

        # Operator badge
        cursor_y += 12
        op_str = group_info["operator"]
        op_tw = font_mono.text_length(op_str, fontsize=6.5)

        # Badge background
        badge_rect = fitz.Rect(text_x, cursor_y - 7, text_x + op_tw + 8, cursor_y + 3)
        shape = page.new_shape()
        shape.draw_rect(badge_rect)
        shape.finish(fill=(0.9, 0.92, 0.96), color=(0.7, 0.75, 0.8), width=0.3)
        shape.commit()

        tw_op = fitz.TextWriter(page.rect)
        tw_op.append(fitz.Point(text_x + 4, cursor_y), op_str,
                     fontsize=6.5, font=font_mono)
        tw_op.write_text(page, color=(0.3, 0.3, 0.5))

        # Description (word-wrapped)
        cursor_y += 10
        desc_lines = _wrap_text(group_info["description"], font, 6.5, text_max_w)
        tw_desc = fitz.TextWriter(page.rect)
        for line in desc_lines:
            cursor_y += 8.5
            if cursor_y > cy + card_h - card_pad:
                break
            tw_desc.append(fitz.Point(text_x, cursor_y), line,
                           fontsize=6.5, font=font)
        tw_desc.write_text(page, color=BODY_COLOR)

    # ── Footer ─────────────────────────────────────────────────────
    footer_y = PAGE_H - 12
    tw_footer = fitz.TextWriter(page.rect)
    footer_text = "Generated from inputs/test-2.pdf  |  Raw classification: no semantic interpretation applied"
    tw_footer.append(fitz.Point(MARGIN, footer_y), footer_text,
                     fontsize=6, font=font)
    tw_footer.write_text(page, color=(0.5, 0.5, 0.5))

    doc.save(str(output_path))
    doc.close()
    elapsed = time.time() - start
    logger.info("Report saved to %s (%.2fs)", output_path, elapsed)


def main() -> None:
    """CLI entry point for report generation."""
    parser = argparse.ArgumentParser(
        description="Generate a PDF report with raw debug mini-views."
    )
    parser.add_argument("input", type=Path, help="Source PDF (for naming)")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output PDF path (default: outputs/<stem>_report.pdf)"
    )
    parser.add_argument(
        "--debug-dir", type=Path, default=Path("outputs"),
        help="Directory with raw_debug_*.png files"
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="Raw elements JSON (default: outputs/<stem>_raw_elements.json)"
    )
    args = parser.parse_args()

    output_path = args.output or Path("outputs") / f"{args.input.stem}_raw_elements_extraction.pdf"
    json_path = args.json or Path("outputs") / f"{args.input.stem}_raw_elements.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not json_path.exists():
        logger.error("Raw JSON not found: %s — run extract_floorplan.py first", json_path)
        return

    generate_report(output_path, args.debug_dir, json_path)


if __name__ == "__main__":
    main()
