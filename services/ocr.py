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
from urllib.parse import unquote_to_bytes

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
    if provider in {"tesseract", "ocr_space"}:
        return False
    return bool(os.getenv("GOOGLE_VISION_API_KEY"))


def _use_ocr_space() -> bool:
    provider = os.getenv("OCR_PROVIDER", "").strip().lower()
    return provider == "ocr_space" and bool(os.getenv("OCR_SPACE_API_KEY"))


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
    if media_url.startswith("data:"):
        _, encoded = media_url.split(",", 1)
        if ";base64" in media_url[: media_url.find(",")]:
            return base64.b64decode(encoded)
        return unquote_to_bytes(encoded)

    headers: dict[str, str] = {}
    provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
    if provider == "meta":
        token = _require_env("WHATSAPP_ACCESS_TOKEN")
        headers["Authorization"] = f"Bearer {token}"
    elif provider == "evolution":
        evolution_api_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
        evolution_api_key = os.getenv("EVOLUTION_API_KEY", "").strip()
        if evolution_api_url and evolution_api_key and media_url.startswith(evolution_api_url):
            headers["apikey"] = evolution_api_key

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.get(media_url, headers=headers)
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


async def _ocr_space_file(file_bytes: bytes, filename: str) -> str:
    api_key = _require_env("OCR_SPACE_API_KEY")
    data = {
        "apikey": api_key,
        "language": os.getenv("OCR_SPACE_LANGUAGE", "eng"),
        "isTable": "false",
        "OCREngine": os.getenv("OCR_SPACE_ENGINE", "2"),
        "scale": "true",
    }

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            "https://api.ocr.space/parse/image",
            data=data,
            files={"file": (filename, file_bytes)},
        )
        response.raise_for_status()
        payload = response.json()

    if payload.get("IsErroredOnProcessing"):
        print(f"OCR.Space returned error: {payload}")
        return ""

    parsed_results = payload.get("ParsedResults", [])
    text_blocks = [item.get("ParsedText", "").strip() for item in parsed_results if item.get("ParsedText")]
    return "\n".join(text_blocks).strip()


async def extract_text_from_image_url(image_url: str) -> str:
    image_bytes = await _download_whatsapp_media(image_url)
    if _use_google_vision():
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        text = await _ocr_base64_image(image_b64)
    elif _use_ocr_space():
        text = await _ocr_space_file(image_bytes, "report-image.png")
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
        if _use_ocr_space():
            extracted_text = await _ocr_space_file(pdf_bytes, "report.pdf")
            if extracted_text:
                print(f"OCR.Space extracted {len(extracted_text)} characters from PDF")
                return extracted_text

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
