"""
services/ocr.py
===============
OCR helpers for report images and PDFs.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile

import fitz
import httpx
from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _use_google_vision() -> bool:
    provider = os.getenv("OCR_PROVIDER", "").strip().lower()
    if provider == "tesseract":
        return False
    return bool(os.getenv("GOOGLE_VISION_API_KEY"))


def _get_tesseract_cmd() -> str:
    return os.getenv("TESSERACT_CMD", "tesseract")


def _ocr_with_tesseract_bytes(image_bytes: bytes, suffix: str = ".png") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
        temp_file.write(image_bytes)
        temp_path = temp_file.name

    try:
        result = subprocess.run(
            [_get_tesseract_cmd(), temp_path, "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True,
            text=True,
            check=True,
        )
        text = (result.stdout or "").strip()
        print(f"Tesseract OCR extracted {len(text)} characters")
        return text
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Tesseract not found. Install it and set TESSERACT_CMD if needed."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(f"Tesseract OCR failed: {stderr}") from exc
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


async def _download_whatsapp_media(media_url: str) -> bytes:
    token = _require_env("WHATSAPP_ACCESS_TOKEN")
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.get(
            media_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.content


async def _ocr_base64_image(image_b64: str) -> str:
    api_key = _require_env("GOOGLE_VISION_API_KEY")
    vision_url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"

    payload = {
        "requests": [
            {
                "image": {"content": image_b64},
                "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
            }
        ]
    }

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(vision_url, json=payload)
        response.raise_for_status()
        data = response.json()

    try:
        return data["responses"][0]["fullTextAnnotation"]["text"].strip()
    except (KeyError, IndexError):
        annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
        if annotations:
            return annotations[0].get("description", "").strip()
        print(f"Google Vision OCR returned no text: {data}")
        return ""


async def extract_text_from_image_url(image_url: str) -> str:
    image_bytes = await _download_whatsapp_media(image_url)
    if _use_google_vision():
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        text = await _ocr_base64_image(image_b64)
    else:
        text = _ocr_with_tesseract_bytes(image_bytes)
    print(f"Image OCR extracted {len(text)} characters")
    return text


async def extract_text_from_pdf_url(pdf_url: str) -> str:
    pdf_bytes = await _download_whatsapp_media(pdf_url)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
        temp_file.write(pdf_bytes)
        temp_path = temp_file.name

    document = None
    try:
        document = fitz.open(temp_path)
        pages_text = []
        max_pages = min(document.page_count, 5)

        for page_number in range(max_pages):
            page = document[page_number]
            pages_text.append(page.get_text("text"))

        extracted_text = "\n".join(filter(None, pages_text)).strip()

        if len(extracted_text) >= 100:
            print(f"PDF text layer extracted {len(extracted_text)} characters")
            return extracted_text

        ocr_pages = []
        for page_number in range(min(document.page_count, 2)):
            page = document[page_number]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            page_png = pixmap.tobytes("png")
            if _use_google_vision():
                page_b64 = base64.b64encode(page_png).decode("utf-8")
                page_text = await _ocr_base64_image(page_b64)
            else:
                page_text = _ocr_with_tesseract_bytes(page_png)
            if page_text:
                ocr_pages.append(page_text)

        extracted_text = "\n".join(ocr_pages).strip()
        print(f"PDF OCR extracted {len(extracted_text)} characters")
        return extracted_text

    except Exception as exc:
        print(f"PDF extraction error: {exc}")
        return ""

    finally:
        if document is not None:
            document.close()
        try:
            os.unlink(temp_path)
        except OSError:
            pass
