"""
services/whatsapp.py
====================
Helpers for sending WhatsApp messages through Meta Cloud API or Evolution API.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

WHATSAPP_API_URL = "https://graph.facebook.com"
WHATSAPP_TEXT_LIMIT = 4096


def whatsapp_provider() -> str:
    provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
    return provider or "meta"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_api_base_url() -> str:
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0").strip()
    return f"{WHATSAPP_API_URL}/{version}"


def _get_evolution_base_url() -> str:
    return _require_env("EVOLUTION_API_URL").rstrip("/")


def _get_evolution_instance_name() -> str:
    return _require_env("EVOLUTION_INSTANCE_NAME").strip()


def _get_evolution_headers() -> dict[str, str]:
    return {
        "apikey": _require_env("EVOLUTION_API_KEY"),
        "Content-Type": "application/json",
    }


def _normalize_recipient(to: str) -> str:
    cleaned = (to or "").strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return "".join(character for character in cleaned if character.isdigit()) or cleaned


def _chunk_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    if len(cleaned) <= limit:
        return [cleaned]

    chunks: list[str] = []
    current = ""

    for paragraph in cleaned.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= limit:
            current = paragraph
            continue

        start = 0
        while start < len(paragraph):
            end = start + limit
            chunks.append(paragraph[start:end])
            start = end

    if current:
        chunks.append(current)

    return chunks


async def _send_single_text_message(to: str, text: str) -> dict:
    if whatsapp_provider() == "evolution":
        recipient = _normalize_recipient(to)
        payload = {
            "number": recipient,
            "text": text,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{_get_evolution_base_url()}/message/sendText/{_get_evolution_instance_name()}",
                headers=_get_evolution_headers(),
                json=payload,
            )
        result = response.json()
        if response.status_code >= 300:
            print(f"Evolution sendText error: {result}")
            raise RuntimeError("Evolution API text send failed.")
        return result

    phone_number_id = _require_env("WHATSAPP_PHONE_NUMBER_ID")
    token = _require_env("WHATSAPP_ACCESS_TOKEN")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{_get_api_base_url()}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    result = response.json()
    if response.status_code >= 300:
        print(f"WhatsApp send error: {result}")
        raise RuntimeError("WhatsApp message send failed.")
    return result


async def send_text_message(to: str, text: str) -> dict:
    """Send text, splitting it into multiple WhatsApp-safe chunks if required."""
    chunks = _chunk_text(text)
    last_result: dict = {}
    for chunk in chunks:
        last_result = await _send_single_text_message(to, chunk)
    return last_result


async def send_image_url(to: str, image_url: str, caption: str = "") -> dict:
    if whatsapp_provider() == "evolution":
        recipient = _normalize_recipient(to)
        payload = {
            "number": recipient,
            "mediatype": "image",
            "mimetype": "image/png",
            "caption": caption,
            "media": image_url,
            "fileName": "biovision-image.png",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{_get_evolution_base_url()}/message/sendMedia/{_get_evolution_instance_name()}",
                headers=_get_evolution_headers(),
                json=payload,
            )
        result = response.json()
        if response.status_code >= 300:
            print(f"Evolution sendMedia error: {result}")
            raise RuntimeError("Evolution API image send failed.")
        return result

    phone_number_id = _require_env("WHATSAPP_PHONE_NUMBER_ID")
    token = _require_env("WHATSAPP_ACCESS_TOKEN")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{_get_api_base_url()}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    result = response.json()
    if response.status_code >= 300:
        print(f"WhatsApp image send error: {result}")
        raise RuntimeError("WhatsApp image send failed.")
    return result
