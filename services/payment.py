"""
services/payment.py
===================
Razorpay payment helpers for BioVision subscriptions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Mapping

import httpx
from dotenv import load_dotenv

load_dotenv()

RAZORPAY_BASE = "https://api.razorpay.com/v1"
MONTHLY_PRICE_PAISE = 19900


def _looks_like_placeholder(value: str | None) -> bool:
    if not value:
        return True

    lowered = value.strip().lower()
    return (
        lowered.startswith("your_")
        or "your_key_here" in lowered
        or lowered.startswith("rzp_live_or_test_")
        or lowered.startswith("rzp_test_your")
    )


def payments_enabled() -> bool:
    flag = os.getenv("PAYMENTS_ENABLED", "false").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False

    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    return not _looks_like_placeholder(key_id) and not _looks_like_placeholder(key_secret)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_auth_header() -> str:
    key = _require_env("RAZORPAY_KEY_ID")
    secret = _require_env("RAZORPAY_KEY_SECRET")
    creds = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("utf-8")
    return f"Basic {creds}"


def _build_reference_id(phone: str) -> str:
    return f"biovision-{phone[-10:]}"


async def create_payment_link(phone: str, name: str) -> str:
    """Create a Razorpay payment link and store it locally."""
    if not payments_enabled():
        raise RuntimeError("Payments are disabled.")

    app_url = os.getenv("APP_URL", "").rstrip("/")
    callback_url = os.getenv("PAYMENT_CALLBACK_URL") or (
        f"{app_url}/payment-success" if app_url else "https://example.com/payment-success"
    )
    reference_id = _build_reference_id(phone)

    payload = {
        "amount": MONTHLY_PRICE_PAISE,
        "currency": "INR",
        "description": "BioVision Premium - Unlimited Lab Reports (30 Days)",
        "reference_id": reference_id,
        "customer": {
            "name": name or "Patient",
            "contact": phone,
        },
        "notify": {
            "sms": True,
            "email": False,
        },
        "reminder_enable": True,
        "notes": {
            "whatsapp_number": phone,
            "product": "biovision_monthly",
        },
        "callback_url": callback_url,
        "callback_method": "get",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{RAZORPAY_BASE}/payment_links",
            headers={
                "Authorization": _get_auth_header(),
                "Content-Type": "application/json",
            },
            json=payload,
        )

    data = response.json()
    if response.status_code not in (200, 201):
        print(f"Razorpay payment link error: {data}")
        raise RuntimeError("Unable to create Razorpay payment link.")

    payment_link_id = data.get("id")
    short_url = data.get("short_url")

    if payment_link_id and short_url:
        from database.users import save_payment_link

        await save_payment_link(
            phone=phone,
            payment_link_id=payment_link_id,
            reference_id=reference_id,
            short_url=short_url,
        )

    return short_url or callback_url


async def is_user_paid(phone: str) -> bool:
    if not payments_enabled():
        return True

    from database.users import get_user_payment_status

    return await get_user_payment_status(phone)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not payments_enabled():
        return False

    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        print("Razorpay webhook secret not configured; skipping signature verification.")
        return True

    generated = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(generated, signature or "")


def verify_callback_signature(query_params: Mapping[str, str]) -> bool:
    if not payments_enabled():
        return False

    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_secret:
        return False

    payment_link_id = query_params.get("razorpay_payment_link_id", "")
    payment_link_reference_id = query_params.get("razorpay_payment_link_reference_id", "")
    payment_link_status = query_params.get("razorpay_payment_link_status", "")
    payment_id = query_params.get("razorpay_payment_id", "")
    signature = query_params.get("razorpay_signature", "")

    if not payment_link_id or not payment_link_status or not payment_id or not signature:
        return False

    signed_payload = "|".join(
        [payment_link_id, payment_link_reference_id, payment_link_status, payment_id]
    )
    generated = hmac.new(
        key_secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(generated, signature)


async def activate_subscription_from_payment_link(payment_link_id: str) -> tuple[bool, bool]:
    if not payments_enabled():
        return False, False

    from database.users import get_payment_link, mark_payment_link_paid, mark_user_as_paid

    payment_link = await get_payment_link(payment_link_id)
    if not payment_link:
        return False, False

    first_time_paid = await mark_payment_link_paid(payment_link_id)
    if not first_time_paid:
        return True, False

    await mark_user_as_paid(payment_link["phone"])
    return True, True


async def handle_payment_webhook(payload: dict, raw_body: bytes, signature: str) -> bool:
    if not payments_enabled():
        return False

    if not verify_webhook_signature(raw_body, signature):
        print("Invalid Razorpay webhook signature.")
        return False

    if payload.get("event") != "payment_link.paid":
        return False

    payment_link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_link_id = payment_link_entity.get("id", "")
    notes = payment_link_entity.get("notes", {}) or {}
    phone = notes.get("whatsapp_number")

    if not payment_link_id:
        return False

    activated, newly_paid = await activate_subscription_from_payment_link(payment_link_id)
    if not activated:
        return False

    if phone and newly_paid:
        from services.whatsapp import send_text_message

        await send_text_message(
            phone,
            "✅ *Payment successful! BioVision Premium active ho gaya hai.*\n\n"
            "Ab aap unlimited reports bhej sakte hain.\n"
            "Photo ya PDF bhejiye aur main explain kar dunga.",
        )

    return True
