"""OCR service: Google Vision API with pytesseract fallback."""

from __future__ import annotations

import base64
import io
import os

import httpx


async def extract_text_from_image_bytes(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Vision or fallback."""
    api_key = os.getenv("GOOGLE_VISION_API_KEY")
    if api_key:
        return await _google_vision_ocr(image_bytes, api_key)
    return _pytesseract_ocr(image_bytes)


async def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes."""
    # Try pdfplumber first (pure text extraction)
    try:
        import pdfplumber
        text = _pdfplumber_extract(pdf_bytes)
        if len(text.strip()) > 50:
            return text
    except ImportError:
        pass

    # Fallback: render first page and OCR
    try:
        return await _pdf_to_image_ocr(pdf_bytes)
    except Exception as exc:
        raise RuntimeError(f"PDF text extraction failed: {exc}") from exc


async def _google_vision_ocr(image_bytes: bytes, api_key: str) -> str:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "requests": [
            {
                "image": {"content": b64_image},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 1}],
            }
        ]
    }
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    responses = data.get("responses", [{}])
    annotation = responses[0].get("fullTextAnnotation", {})
    return annotation.get("text", "")


def _pytesseract_ocr(image_bytes: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
        image = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(image, lang="eng+hin")
    except Exception as exc:
        raise RuntimeError(f"pytesseract OCR failed: {exc}") from exc


def _pdfplumber_extract(pdf_bytes: bytes) -> str:
    import pdfplumber
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)


async def _pdf_to_image_ocr(pdf_bytes: bytes) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texts = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            api_key = os.getenv("GOOGLE_VISION_API_KEY")
            if api_key:
                t = await _google_vision_ocr(img_bytes, api_key)
            else:
                t = _pytesseract_ocr(img_bytes)
            texts.append(t)
        return "\n".join(texts)
    except ImportError:
        raise RuntimeError("PyMuPDF not installed. Install with: pip install pymupdf")
