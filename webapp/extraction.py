"""PDF element extraction and rasterization for the web app.

Wraps extract_raw_types() from src/extract_floorplan.py and adds
3x page rasterization for the blueprint background layer.
"""

import base64
import io
import logging
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

# Add src/ to import path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from extract_floorplan import extract_raw_types

logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SCALE_FACTOR = 3


def rasterize_page(pdf_path: Path) -> str:
    """Render the first page of a PDF at 3x resolution as a base64 PNG.

    Normalizes page rotation to 0° before rasterizing so the pixmap
    aligns with the derotated coordinate space used by extract_raw_types().

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Data URI string: 'data:image/png;base64,...'
    """
    doc = fitz.open(str(pdf_path))
    page = doc[0]
    if page.rotation != 0:
        logger.info("Normalizing page rotation %d° -> 0° for rasterization", page.rotation)
        page.set_rotation(0)
    mat = fitz.Matrix(SCALE_FACTOR, SCALE_FACTOR)
    pix = page.get_pixmap(matrix=mat)
    png_bytes = pix.tobytes("png")
    doc.close()

    b64 = base64.b64encode(png_bytes).decode("ascii")
    logger.info("Rasterized page at %dx: %d x %d px (%d KB)",
                SCALE_FACTOR, pix.width, pix.height, len(png_bytes) // 1024)
    return f"data:image/png;base64,{b64}"


def extract_from_upload(file_bytes: bytes, filename: str) -> dict:
    """Extract raw elements and rasterized background from uploaded PDF bytes.

    Args:
        file_bytes: Raw PDF file content.
        filename: Original filename for metadata.

    Returns:
        Dict with elements, page_size, scale_factor, and background_png.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        result = extract_raw_types(tmp_path)
        result["source"] = filename
        result["scale_factor"] = SCALE_FACTOR
        result["background_png"] = rasterize_page(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return result
