"""
services/whatsapp.py
====================
Helpers for sending WhatsApp Cloud API messages.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

WHATSAPP_API_URL = "https://graph.facebook.com/v18.0"
WHATSAPP_TEXT_LIMIT = 4096


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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
            f"{WHATSAPP_API_URL}/{phone_number_id}/messages",
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
            f"{WHATSAPP_API_URL}/{phone_number_id}/messages",
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
