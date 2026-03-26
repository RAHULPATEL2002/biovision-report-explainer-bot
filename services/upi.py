"""
services/upi.py
===============
Manual UPI payment helpers for pay-by-link / pay-by-QR flows.
"""

from __future__ import annotations

import os
from io import BytesIO
from urllib.parse import urlencode

import qrcode
from dotenv import load_dotenv

from database.users import create_upi_payment_request, get_latest_open_upi_payment

load_dotenv()


def upi_manual_enabled() -> bool:
    provider = os.getenv("PAYMENT_PROVIDER", "upi_manual").strip().lower()
    if provider != "upi_manual":
        return False

    flag = os.getenv("PAYMENTS_ENABLED", "false").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False

    return bool(os.getenv("UPI_VPA") and os.getenv("UPI_PAYEE_NAME"))


def get_report_price_inr() -> int:
    return max(1, int(os.getenv("REPORT_PRICE_INR", "30")))


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _build_payment_ref(phone: str) -> str:
    suffix = phone[-6:] if phone else "000000"
    return f"BVR{suffix}{os.urandom(3).hex().upper()}"


def build_upi_uri(payment_ref: str, amount_inr: int, note: str) -> str:
    params = {
        "pa": _require_env("UPI_VPA"),
        "pn": _require_env("UPI_PAYEE_NAME"),
        "am": str(amount_inr),
        "cu": "INR",
        "tn": note,
        "tr": payment_ref,
    }
    return "upi://pay?" + urlencode(params)


async def get_or_create_upi_request(phone: str, purpose: str = "report_access") -> dict:
    amount_inr = get_report_price_inr()
    existing = await get_latest_open_upi_payment(phone)
    if existing:
        return existing

    payment_ref = _build_payment_ref(phone)
    note = f"BioVision {purpose} {payment_ref}"
    upi_uri = build_upi_uri(payment_ref, amount_inr, note)
    return await create_upi_payment_request(
        payment_ref=payment_ref,
        phone=phone,
        amount_inr=amount_inr,
        upi_uri=upi_uri,
        note=note,
    )


def get_upi_qr_png_bytes(upi_uri: str) -> bytes:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
