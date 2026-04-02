"""FastAPI web server for the PDF Element Explorer.

Serves the single-page frontend and provides the /api/extract
endpoint for PDF upload and element extraction.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from webapp.extraction import extract_from_upload

logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="PDF Element Explorer")


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)) -> dict:
    """Upload a PDF and extract all raw elements + rasterized background.

    Args:
        file: Uploaded PDF file.

    Returns:
        JSON with groups, page_size, scale_factor, background_png.
    """
    content = await file.read()
    logger.info("Received upload: %s (%d KB)", file.filename, len(content) // 1024)
    result = extract_from_upload(content, file.filename or "upload.pdf")
    total = sum(len(g["elements"]) for g in result["groups"])
    logger.info("Extraction complete: %d elements in %d groups", total, len(result["groups"]))
    return result


@app.get("/")
async def root() -> FileResponse:
    """Serve the main HTML page."""
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
