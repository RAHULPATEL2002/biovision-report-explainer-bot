"""
BioVision WhatsApp Bot main application.

Run locally:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from database.users import (
    approve_upi_payment,
    consume_user_paid_credit,
    get_latest_open_upi_payment,
    get_or_create_user,
    get_pending_upi_payments,
    get_upi_payment_by_ref,
    get_user_paid_credits,
    get_user_report_count,
    get_user_reports,
    get_user_subscription_details,
    increment_report_count,
    init_db,
    reject_upi_payment,
    save_report,
    submit_upi_payment_utr,
    update_user_language,
)
from services.ai_explainer import explain_report_in_hindi
from services.ocr import extract_text_from_image_url, extract_text_from_pdf_url
from services.payment import (
    activate_subscription_from_payment_link,
    create_payment_link,
    handle_payment_webhook,
    is_user_paid,
    payment_provider,
    payments_enabled,
    verify_callback_signature,
)
from services.upi import (
    get_or_create_upi_request,
    get_report_price_inr,
    get_upi_qr_png_bytes,
    upi_manual_enabled,
)
from services.whatsapp import send_image_url, send_text_message

load_dotenv()

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "biovision2025")
FREE_REPORT_LIMIT = int(os.getenv("FREE_REPORT_LIMIT", "3"))
PROCESSING_CONCURRENCY = max(1, int(os.getenv("PROCESSING_CONCURRENCY", "3")))
WHATSAPP_GRAPH_API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    app.state.background_tasks = set()
    app.state.media_processing_semaphore = asyncio.Semaphore(PROCESSING_CONCURRENCY)
    app.state.active_jobs = 0
    yield

    tasks = list(app.state.background_tasks)
    for task in tasks:
        task.cancel()


app = FastAPI(title="BioVision WhatsApp Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "BioVision Bot is running! 🩺"}


@app.get("/health")
async def health_check() -> dict[str, Any]:
    provider = payment_provider()
    whatsapp_provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower() or "meta"
    configured = {
        "whatsapp_provider": whatsapp_provider,
        "whatsapp_access_token": _is_real_env_value(os.getenv("WHATSAPP_ACCESS_TOKEN")),
        "whatsapp_phone_number_id": _is_real_env_value(os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
        "evolution_api_url": _is_real_env_value(os.getenv("EVOLUTION_API_URL")),
        "evolution_api_key": _is_real_env_value(os.getenv("EVOLUTION_API_KEY")),
        "evolution_instance_name": _is_real_env_value(os.getenv("EVOLUTION_INSTANCE_NAME")),
        "openrouter_api_key": _is_real_env_value(os.getenv("OPENROUTER_API_KEY")),
        "anthropic_api_key": _is_real_env_value(os.getenv("ANTHROPIC_API_KEY")),
        "google_vision_api_key": _is_real_env_value(os.getenv("GOOGLE_VISION_API_KEY")),
        "ocr_space_api_key": _is_real_env_value(os.getenv("OCR_SPACE_API_KEY")),
        "upi_vpa": _is_real_env_value(os.getenv("UPI_VPA")),
        "upi_payee_name": _is_real_env_value(os.getenv("UPI_PAYEE_NAME")),
        "razorpay_key_id": _is_real_env_value(os.getenv("RAZORPAY_KEY_ID")),
        "razorpay_key_secret": _is_real_env_value(os.getenv("RAZORPAY_KEY_SECRET")),
        "app_url": _is_real_env_value(os.getenv("APP_URL")),
        "admin_api_key": _is_real_env_value(os.getenv("ADMIN_API_KEY")),
    }
    whatsapp_ready = (
        configured["evolution_api_url"]
        and configured["evolution_api_key"]
        and configured["evolution_instance_name"]
        if whatsapp_provider == "evolution"
        else configured["whatsapp_access_token"] and configured["whatsapp_phone_number_id"]
    )
    return {
        "status": "ok",
        "verify_token_configured": bool(os.getenv("WHATSAPP_VERIFY_TOKEN")),
        "processing_concurrency": PROCESSING_CONCURRENCY,
        "active_jobs": getattr(app.state, "active_jobs", 0),
        "queued_background_tasks": len(getattr(app.state, "background_tasks", set())),
        "payment_provider": provider,
        "payments_enabled": payments_enabled(),
        "whatsapp_ready": whatsapp_ready,
        "services": configured,
    }


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        print("Meta webhook verified successfully.")
        return Response(content=params["hub.challenge"], media_type="text/plain")
    return Response(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request) -> dict[str, Any]:
    body = await request.json()

    try:
        messages = extract_message_payloads(body)
        if not messages:
            return {"status": "ignored"}

        for message_data in messages:
            _spawn_background_task(process_message_job(message_data))

        return {"status": "accepted", "queued_messages": len(messages)}
    except Exception as exc:
        print(f"Webhook processing error: {exc}")
        return {"status": "error"}


@app.post("/payments/razorpay/webhook")
async def razorpay_webhook(request: Request) -> dict[str, bool]:
    if payment_provider() != "razorpay" or not payments_enabled():
        return {"processed": False}

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    payload = await request.json()

    try:
        processed = await handle_payment_webhook(payload, raw_body, signature)
        return {"processed": processed}
    except Exception as exc:
        print(f"Razorpay webhook error: {exc}")
        return {"processed": False}


@app.get("/payment-success", response_class=HTMLResponse)
async def payment_success(request: Request) -> HTMLResponse:
    if payment_provider() != "razorpay" or not payments_enabled():
        return HTMLResponse(
            content=_payment_page_html(
                "Payments Disabled",
                "Is deployment mein Razorpay payment callback active nahi hai.",
            ),
            status_code=200,
        )

    query_params = {key: value for key, value in request.query_params.items()}
    payment_link_status = query_params.get("razorpay_payment_link_status", "").lower()
    payment_link_id = query_params.get("razorpay_payment_link_id", "")

    if payment_link_status != "paid":
        return HTMLResponse(
            content=_payment_page_html(
                "Payment Pending",
                "Payment abhi confirm nahi hua. Agar paise kat gaye hain to 1-2 minute baad WhatsApp check karein.",
            ),
            status_code=200,
        )

    if not verify_callback_signature(query_params):
        return HTMLResponse(
            content=_payment_page_html(
                "Invalid Signature",
                "Payment callback verify nahi ho paya. Support team se contact karein.",
            ),
            status_code=400,
        )

    activated, _ = await activate_subscription_from_payment_link(payment_link_id)
    if not activated:
        return HTMLResponse(
            content=_payment_page_html(
                "Payment Received",
                "Payment mila, lekin subscription mapping automatic nahi hui. WhatsApp support ko message karein.",
            ),
            status_code=200,
        )

    return HTMLResponse(
        content=_payment_page_html(
            "Premium Activated",
            "Payment successful! WhatsApp par wapas jaiye aur apni report ki photo ya PDF bhejiye.",
        ),
        status_code=200,
    )


@app.get("/payments/upi/qr/{payment_ref}.png")
async def upi_payment_qr(payment_ref: str) -> StreamingResponse:
    payment = await get_upi_payment_by_ref(payment_ref)
    if not payment or not payment.get("upi_uri"):
        raise HTTPException(status_code=404, detail="Payment request not found.")

    png_bytes = get_upi_qr_png_bytes(payment["upi_uri"])
    return StreamingResponse(iter([png_bytes]), media_type="image/png")


@app.get("/admin/upi/pending")
async def admin_list_pending_payments(x_admin_key: str = Header(default="")) -> dict[str, Any]:
    _assert_admin_key(x_admin_key)
    payments = await get_pending_upi_payments()
    return {"pending_payments": payments}


@app.post("/admin/upi/{payment_ref}/approve")
async def admin_approve_payment(
    payment_ref: str,
    credits: int = 1,
    x_admin_key: str = Header(default=""),
) -> dict[str, Any]:
    _assert_admin_key(x_admin_key)
    payment = await approve_upi_payment(payment_ref, max(1, credits))
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found.")

    await safe_send_text(
        payment["phone"],
        "✅ Payment verify ho gaya.\n"
        f"Aapke account mein {payment.get('approved_credits', 1)} paid report credit add ho gaya hai.\n"
        "Ab apni report ki photo ya PDF bhejiye.",
    )
    return {"approved_payment": payment}


@app.post("/admin/upi/{payment_ref}/reject")
async def admin_reject_payment(
    payment_ref: str,
    x_admin_key: str = Header(default=""),
) -> dict[str, Any]:
    _assert_admin_key(x_admin_key)
    payment = await reject_upi_payment(payment_ref)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found.")

    await safe_send_text(
        payment["phone"],
        "⚠️ Payment verification complete nahi ho payi.\n"
        "Kripya sahi UTR ke saath dobara `paid <UTR>` bhejiye ya support se contact karein.",
    )
    return {"rejected_payment": payment}


def _spawn_background_task(coro: Any) -> None:
    task = asyncio.create_task(coro)
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


async def process_message_job(message_data: dict[str, Any]) -> None:
    app.state.active_jobs += 1

    try:
        phone = message_data["from"]
        contact_name = message_data.get("name", "")
        message_type = message_data["type"]

        user = await get_or_create_user(phone, contact_name)
        report_count = await get_user_report_count(phone)
        subscription_paid = await is_user_paid(phone)
        paid_credits = await get_user_paid_credits(phone)
        payments_on = payments_enabled()

        if message_type == "text":
            incoming_text = message_data.get("text", "").strip()
            await handle_text(phone, incoming_text, user, report_count, subscription_paid, paid_credits, payments_on)
        elif message_type == "image":
            async with app.state.media_processing_semaphore:
                await handle_media(
                    phone,
                    message_data["media_id"],
                    "image",
                    user,
                    report_count,
                    subscription_paid,
                    paid_credits,
                    payments_on,
                )
        elif message_type == "document":
            mime_type = message_data.get("mime_type", "")
            if "pdf" not in mime_type.lower():
                await safe_send_text(
                    phone,
                    "❌ Abhi sirf report ki photo ya PDF support hoti hai. Kripya wahi bhejein.",
                )
            else:
                async with app.state.media_processing_semaphore:
                    await handle_media(
                        phone,
                        message_data["media_id"],
                        "pdf",
                        user,
                        report_count,
                        subscription_paid,
                        paid_credits,
                        payments_on,
                    )
        else:
            await safe_send_text(
                phone,
                "📎 Is type ka message abhi support nahi hai.\nPhoto, PDF, ya text message bhejiye.",
            )

    except Exception as exc:
        print(f"Background message processing error: {exc}")
    finally:
        app.state.active_jobs = max(0, app.state.active_jobs - 1)


async def handle_text(
    phone: str,
    text: str,
    user: dict,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> None:
    lower_text = text.strip().lower()

    if _matches_any(lower_text, ["hi", "hello", "helo", "hey", "start", "menu", "help", "namaste"]):
        await safe_send_text(
            phone,
            get_welcome_message(user.get("name", ""), report_count, subscription_paid, paid_credits, payments_on),
        )
        return

    if _matches_any(lower_text, ["kaise", "how", "use", "steps", "guide"]):
        await safe_send_text(phone, get_how_to_use())
        return

    if lower_text.startswith("paid ") or lower_text.startswith("utr "):
        utr = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        await safe_send_text(phone, await handle_payment_proof_submission(phone, utr))
        return

    if _matches_any(lower_text, ["price", "pricing", "cost", "kitna"]):
        await safe_send_text(phone, get_pricing_message(payments_on))
        return

    if _matches_any(lower_text, ["pay", "premium", "subscription", "subscribe", "unlock"]):
        await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on)
        return

    if _matches_any(lower_text, ["history", "reports", "past report", "old report"]):
        await safe_send_text(phone, await get_history_message(phone))
        return

    if _matches_any(lower_text, ["status", "plan", "trial", "remaining", "balance", "credit"]):
        await safe_send_text(phone, await get_status_message(phone))
        return

    if await user_can_submit_report(report_count, subscription_paid, paid_credits, payments_on):
        remaining = max(FREE_REPORT_LIMIT - report_count, 0)
        await safe_send_text(phone, get_report_prompt(remaining, subscription_paid, paid_credits, payments_on))
        return

    await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on)


async def handle_media(
    phone: str,
    media_id: str,
    media_type: str,
    user: dict,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> None:
    if not await user_can_submit_report(report_count, subscription_paid, paid_credits, payments_on):
        await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on)
        return

    await safe_send_text(
        phone,
        "⏳ *Report process ho rahi hai...*\n"
        "Please 30-60 seconds wait karein.\n\n"
        "AI aapki report padh raha hai.",
    )

    try:
        media_url = await get_whatsapp_media_url(media_id)
        if media_type == "image":
            extracted_text = await extract_text_from_image_url(media_url)
        else:
            extracted_text = await extract_text_from_pdf_url(media_url)

        if len(extracted_text.strip()) < 50:
            await safe_send_text(
                phone,
                "❌ Report clearly read nahi ho payi.\n\n"
                "Dobara bhejte waqt dhyan dein:\n"
                "• photo seedhi aur clear ho\n"
                "• poori report frame mein ho\n"
                "• roshni achhi ho",
            )
            return

        explanation = await explain_report_in_hindi(extracted_text)
        explanation_sent = await safe_send_text(phone, explanation)

        if explanation.startswith("⚠️"):
            return

        if not explanation_sent:
            print(f"Skipping report save for {phone} because explanation delivery failed.")
            return

        await save_report(phone, extracted_text, explanation)
        await increment_report_count(phone)

        used_paid_credit = False
        if payments_on and report_count >= FREE_REPORT_LIMIT and not subscription_paid:
            used_paid_credit = await consume_user_paid_credit(phone)

        new_count = report_count + 1
        remaining = max(FREE_REPORT_LIMIT - new_count, 0)
        credits_left = await get_user_paid_credits(phone)
        await safe_send_text(
            phone,
            get_follow_up_message(remaining, subscription_paid, payments_on, used_paid_credit, credits_left),
        )

    except Exception as exc:
        print(f"Media processing error: {exc}")
        await safe_send_text(
            phone,
            "⚠️ Report process karte waqt error aaya.\n"
            "Thodi der baad dobara try karein. Agar issue repeat ho to support se contact karein.",
        )


async def send_upi_payment_prompt(
    phone: str,
    report_count: int,
    paid_credits: int,
    payments_on: bool,
) -> None:
    if not payments_on:
        await safe_send_text(
            phone,
            "✅ Is build mein payments disabled hain.\nSeedha apni report ki photo ya PDF bhejiye.",
        )
        return

    if payment_provider() == "razorpay":
        await safe_send_text(phone, await get_razorpay_payment_message(phone))
        return

    if not upi_manual_enabled():
        await safe_send_text(
            phone,
            "⚠️ Payment system abhi configure nahi hai. Support se contact karein.",
        )
        return

    payment_request = await get_or_create_upi_request(phone)
    price = payment_request.get("amount_inr", get_report_price_inr())
    payment_ref = payment_request["payment_ref"]
    upi_uri = payment_request["upi_uri"]
    payee_name = os.getenv("UPI_PAYEE_NAME", "BioVision")
    qr_url = _upi_qr_url(payment_ref)

    message = (
        f"💳 *Report unlock payment*\n\n"
        f"Ek paid report ke liye *₹{price}* pay kariye.\n"
        f"UPI ID: *{os.getenv('UPI_VPA', '')}*\n"
        f"Payee: *{payee_name}*\n"
        f"Ref: *{payment_ref}*\n\n"
        f"Tap to pay:\n{upi_uri}\n\n"
        "Payment ke baad yahin reply kariye:\n"
        f"`paid YOUR_UTR`\n\n"
        f"Agar UTR nahi pata ho to apne UPI app mein transaction reference dekhiye."
    )
    await safe_send_text(phone, message)

    if qr_url:
        await safe_send_image(
            phone,
            qr_url,
            f"QR scan karke ₹{price} pay karein. Payment ke baad `paid YOUR_UTR` bhejiye.",
        )

    if report_count >= FREE_REPORT_LIMIT:
        await safe_send_text(
            phone,
            f"Aapke free reports use ho chuke hain. Paid credits available: {paid_credits}",
        )


async def handle_payment_proof_submission(phone: str, utr: str) -> str:
    if not utr:
        return "⚠️ UTR missing hai. Example: `paid 123456789012`"

    payment = await submit_upi_payment_utr(phone, utr)
    if not payment:
        return "⚠️ Koi pending payment request nahi mili. Pehle `pay` bhejiye."

    return (
        "✅ Payment proof receive ho gaya.\n"
        f"Ref: {payment['payment_ref']}\n"
        "Team verification ke baad aapko WhatsApp par credit mil jayega.\n"
        "Usually yeh kuch minutes mein ho jana chahiye."
    )


async def user_can_submit_report(
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> bool:
    if report_count < FREE_REPORT_LIMIT:
        return True
    if subscription_paid:
        return True
    if payments_on and paid_credits > 0:
        return True
    return not payments_on


async def safe_send_text(phone: str, text: str) -> bool:
    try:
        await send_text_message(phone, text)
        return True
    except Exception as exc:
        print(f"Unable to send WhatsApp message to {phone}: {exc}")
        return False


async def safe_send_image(phone: str, image_url: str, caption: str = "") -> bool:
    try:
        await send_image_url(phone, image_url, caption)
        return True
    except Exception as exc:
        print(f"Unable to send WhatsApp image to {phone}: {exc}")
        return False


async def get_whatsapp_media_url(media_id: str) -> str:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Missing required environment variable: WHATSAPP_ACCESS_TOKEN")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()

    media_url = data.get("url")
    if not media_url:
        raise RuntimeError("WhatsApp media URL missing in response.")
    return media_url


def extract_message_payloads(body: dict) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", []) or []
            contacts = value.get("contacts", []) or []
            contact_name = ""
            if contacts:
                contact_name = contacts[0].get("profile", {}).get("name", "").strip()

            for message in messages:
                message_type = message.get("type")
                payload: dict[str, Any] = {
                    "from": message.get("from", ""),
                    "type": message_type,
                    "name": contact_name,
                }

                if message_type == "text":
                    payload["text"] = message.get("text", {}).get("body", "")
                elif message_type == "image":
                    payload["media_id"] = message.get("image", {}).get("id", "")
                elif message_type == "document":
                    payload["media_id"] = message.get("document", {}).get("id", "")
                    payload["mime_type"] = message.get("document", {}).get("mime_type", "")

                if payload.get("from") and payload.get("type"):
                    payloads.append(payload)

    return payloads


def get_pricing_message(payments_on: bool) -> str:
    if not payments_on:
        return (
            "💰 *BioVision Pricing*\n\n"
            "Is build mein payments disabled hain.\n"
            "Aap reports free mein bhej sakte hain."
        )

    if payment_provider() == "upi_manual":
        price = get_report_price_inr()
        return (
            "💰 *BioVision Pricing*\n\n"
            f"🎁 First {FREE_REPORT_LIMIT} reports free\n"
            f"💳 Uske baad *₹{price} per report*\n"
            "UPI se direct payment kar sakte hain.\n"
            "Payment link/QR ke liye `pay` bhejiye."
        )

    return (
        "💰 *BioVision Pricing*\n\n"
        "🎁 Free Trial: 3 reports free\n"
        "⭐ Premium Plan: ₹199 / 30 days\n"
        "Premium activate karne ke liye `pay` bhejiye."
    )


async def get_razorpay_payment_message(phone: str) -> str:
    try:
        payment_link = await create_payment_link(phone, "Patient")
        payment_text = f"👉 Premium unlock link:\n{payment_link}"
    except Exception as exc:
        print(f"Payment link creation failed: {exc}")
        payment_text = "Razorpay link abhi generate nahi ho paya. Team se contact karein."

    return (
        "⚠️ *Aapke free reports use ho chuke hain.*\n\n"
        "Unlimited reports ke liye Premium plan ₹199 / 30 days hai.\n"
        f"{payment_text}\n\n"
        "Payment ke baad report bhejte hi access unlock ho jayega."
    )


async def get_history_message(phone: str) -> str:
    reports = await get_user_reports(phone, limit=5)
    if not reports:
        return (
            "📂 *History abhi empty hai.*\n\n"
            "Apni pehli report ki photo ya PDF bhejiye, main uska summary save kar dunga."
        )

    lines = ["📂 *Aapki recent reports:*", ""]
    for index, report in enumerate(reports, start=1):
        created_at = _format_date(report.get("created_at", ""))
        explanation = (report.get("explanation") or "").replace("\n", " ").strip()
        snippet = explanation[:110].strip()
        if len(explanation) > 110:
            snippet += "..."
        lines.append(f"{index}. {created_at} - {snippet or 'Summary saved'}")

    return "\n".join(lines)


async def get_status_message(phone: str) -> str:
    details = await get_user_subscription_details(phone)
    if not details:
        return "Status dekhne ke liye pehle is number se bot use karein."

    report_count = details.get("report_count", 0)
    paid_until_raw = details.get("paid_until")
    subscription_paid = await is_user_paid(phone)
    paid_credits = await get_user_paid_credits(phone)
    pending_payment = await get_latest_open_upi_payment(phone)

    lines = [
        "📊 *Aapka account status*",
        f"Used reports: {report_count}",
        f"Paid credits: {paid_credits}",
    ]

    if subscription_paid and paid_until_raw:
        lines.append(f"Premium active till: {_format_date(paid_until_raw, include_time=True)}")

    if pending_payment:
        lines.append(
            f"Pending payment: {pending_payment['payment_ref']} ({pending_payment.get('status', 'pending')})"
        )

    remaining_free = max(FREE_REPORT_LIMIT - report_count, 0)
    lines.append(f"Free reports remaining: {remaining_free}")
    return "\n".join(lines)


def get_welcome_message(
    name: str,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> str:
    remaining = max(FREE_REPORT_LIMIT - report_count, 0)
    if subscription_paid:
        plan_line = "⭐ Premium active hai. Aap unlimited reports bhej sakte hain."
    elif payments_on and payment_provider() == "upi_manual":
        plan_line = (
            f"🎁 Aapke paas {remaining} free report(s) bachi hain.\n"
            f"💳 Paid credits: {paid_credits}"
        )
    elif not payments_on:
        plan_line = "🆓 Demo mode active hai. Aap reports free mein bhej sakte hain."
    else:
        plan_line = f"🎁 Aapke paas {remaining} free report(s) bachi hain."

    return (
        f"🩺 *BioVision mein swagat hai!*\n\n"
        f"Namaste {name or 'ji'}.\n"
        "Main aapki blood test report ko simple Hindi mein explain karta hoon.\n\n"
        f"{plan_line}\n\n"
        "Photo ya PDF bhejiye aur main important values, normal range, aur doctor se poochne wale sawal bataunga.\n\n"
        "Commands: help, price, pay, paid <UTR>, history, status"
    )


def get_how_to_use() -> str:
    return (
        "📘 *BioVision kaise use karein*\n\n"
        "1. Report ki clear photo ya PDF bhejiye\n"
        "2. 30-60 seconds wait kariye\n"
        "3. Hindi summary, abnormal values, aur doctor questions mil jayenge\n\n"
        "Free reports khatam hone ke baad `pay` bhejiye aur UPI se payment karke `paid <UTR>` reply karein."
    )


def get_report_prompt(
    remaining: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> str:
    if subscription_paid:
        header = "📄 Premium active hai. Apni report ki photo ya PDF bhejiye."
    elif payments_on and paid_credits > 0:
        header = f"📄 Aapke paas {paid_credits} paid credit(s) hain. Report bhejiye."
    elif not payments_on:
        header = "📄 Demo mode active hai. Apni report ki photo ya PDF bhejiye."
    else:
        header = f"📄 Apni report ki photo ya PDF bhejiye.\nAapke paas {remaining} free report(s) bachi hain."

    return (
        f"{header}\n\n"
        "Tip:\n"
        "• image clear ho\n"
        "• report flat ho\n"
        "• text cut na ho"
    )


def get_follow_up_message(
    remaining: int,
    subscription_paid: bool,
    payments_on: bool,
    used_paid_credit: bool,
    credits_left: int,
) -> str:
    if subscription_paid:
        plan_line = "Premium active hai, aap aur reports bhi bhej sakte hain."
    elif used_paid_credit:
        plan_line = f"Ek paid credit use hua. Credits left: {credits_left}"
    elif not payments_on:
        plan_line = "Demo mode active hai, aap aur reports bhi bhej sakte hain."
    else:
        plan_line = f"Free reports remaining: {remaining}"

    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "⚕️ Ye educational explanation hai. Final medical advice ke liye doctor se consult karein.\n\n"
        f"{plan_line}\n"
        "History dekhne ke liye `history` bhejiye."
    )


def _format_date(raw_value: str, include_time: bool = False) -> str:
    if not raw_value:
        return "Unknown date"

    try:
        parsed = datetime.fromisoformat(raw_value)
        if include_time:
            return parsed.strftime("%d %b %Y, %I:%M %p")
        return parsed.strftime("%d %b %Y")
    except ValueError:
        return raw_value


def _payment_page_html(title: str, body: str) -> str:
    return f"""
    <html>
        <head>
            <title>{title}</title>
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    background: #f4f8f7;
                    color: #16302b;
                    display: flex;
                    min-height: 100vh;
                    align-items: center;
                    justify-content: center;
                    margin: 0;
                    padding: 24px;
                }}
                .card {{
                    max-width: 520px;
                    background: white;
                    border-radius: 18px;
                    padding: 28px;
                    box-shadow: 0 20px 60px rgba(22, 48, 43, 0.12);
                }}
                h1 {{
                    margin-top: 0;
                }}
                p {{
                    line-height: 1.6;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>{title}</h1>
                <p>{body}</p>
            </div>
        </body>
    </html>
    """


def _matches_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _is_real_env_value(value: str | None) -> bool:
    if not value:
        return False

    lowered = value.strip().lower()
    placeholder_markers = [
        "your_",
        "your_key_here",
        "https://your-",
        "sk-ant-your",
        "sk-or-v1-your_key_here",
        "aiza_your",
        "rzp_live_or_test_",
        "rzp_test_your",
        "upi@bank",
    ]
    return not any(marker in lowered for marker in placeholder_markers)


def _upi_qr_url(payment_ref: str) -> str:
    app_url = os.getenv("APP_URL", "").rstrip("/")
    if not _is_real_env_value(app_url):
        return ""
    return f"{app_url}/payments/upi/qr/{payment_ref}.png"


def _assert_admin_key(header_value: str) -> None:
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not admin_key or header_value != admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key.")


def _text(language: str, hindi_text: str, english_text: str) -> str:
    return english_text if _normalize_language(language) == "en" else hindi_text


def _normalize_language(language: str | None) -> str:
    raw = (language or "").strip().lower()
    if raw.startswith("en"):
        return "en"
    if raw.startswith("hi"):
        return "hi"
    return ""


def _get_user_language(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    return _normalize_language(str(user.get("preferred_language", "")))


def _parse_language_choice(text: str) -> str:
    normalized = text.strip().lower()
    if normalized in {"1", "hindi", "hindi.", "हिंदी", "हिन्दी"}:
        return "hi"
    if normalized in {"2", "english", "english.", "en"}:
        return "en"
    return ""


def get_language_prompt(name: str = "", current_language: str = "") -> str:
    greeting = f" {name.strip()}." if name.strip() else "."
    current = _normalize_language(current_language)
    current_line = ""
    if current:
        current_line = _text(
            current,
            "\nCurrent language: Hindi",
            "\nCurrent language: English",
        )
    return (
        f"🌐 Welcome{greeting}\n"
        "Please choose your preferred language first:\n\n"
        "1. Hindi\n"
        "2. English\n\n"
        "Reply with `1`, `2`, `Hindi`, or `English`."
        f"{current_line}"
    )


def _is_real_env_value(value: str | None) -> bool:
    if not value:
        return False

    lowered = value.strip().lower()
    placeholder_markers = [
        "your_",
        "your_key_here",
        "https://your-",
        "your-app.railway.app",
        "sk-ant-your",
        "sk-or-v1-your_key_here",
        "aiza_your",
        "rzp_live_or_test_",
        "rzp_test_your",
        "upi@bank",
        "replace_with_",
    ]
    return not any(marker in lowered for marker in placeholder_markers)


def _assert_admin_key(header_value: str) -> None:
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if not _is_real_env_value(admin_key):
        raise HTTPException(status_code=503, detail="Admin API key is not configured.")
    if header_value != admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key.")


async def handle_text(
    phone: str,
    text: str,
    user: dict,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> None:
    lower_text = text.strip().lower()
    language = _get_user_language(user)

    if not language:
        chosen_language = _parse_language_choice(lower_text)
        if chosen_language:
            await update_user_language(phone, chosen_language)
            await safe_send_text(
                phone,
                get_welcome_message(
                    user.get("name", ""),
                    report_count,
                    subscription_paid,
                    paid_credits,
                    payments_on,
                    chosen_language,
                ),
            )
            await safe_send_text(
                phone,
                get_report_prompt(
                    max(FREE_REPORT_LIMIT - report_count, 0),
                    subscription_paid,
                    paid_credits,
                    payments_on,
                    chosen_language,
                ),
            )
            return

        await safe_send_text(phone, get_language_prompt(user.get("name", "")))
        return

    chosen_language = _parse_language_choice(lower_text)
    if lower_text in {"language", "lang", "bhasha"}:
        await safe_send_text(phone, get_language_prompt(user.get("name", ""), language))
        return

    if chosen_language and chosen_language != language:
        await update_user_language(phone, chosen_language)
        await safe_send_text(
            phone,
            _text(
                chosen_language,
                "✅ Language Hindi mein set ho gayi.",
                "✅ Language switched to English.",
            ),
        )
        await safe_send_text(
            phone,
            get_welcome_message(
                user.get("name", ""),
                report_count,
                subscription_paid,
                paid_credits,
                payments_on,
                chosen_language,
            ),
        )
        return

    if _matches_any(lower_text, ["hi", "hello", "helo", "hey", "start", "menu", "help", "namaste"]):
        await safe_send_text(
            phone,
            get_welcome_message(
                user.get("name", ""),
                report_count,
                subscription_paid,
                paid_credits,
                payments_on,
                language,
            ),
        )
        return

    if _matches_any(lower_text, ["kaise", "how", "use", "steps", "guide"]):
        await safe_send_text(phone, get_how_to_use(language))
        return

    if lower_text.startswith("paid ") or lower_text.startswith("utr "):
        utr = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        await safe_send_text(phone, await handle_payment_proof_submission(phone, utr, language))
        return

    if _matches_any(lower_text, ["price", "pricing", "cost", "kitna"]):
        await safe_send_text(phone, get_pricing_message(payments_on, language))
        return

    if _matches_any(lower_text, ["pay", "premium", "subscription", "subscribe", "unlock"]):
        await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on, language)
        return

    if _matches_any(lower_text, ["history", "reports", "past report", "old report"]):
        await safe_send_text(phone, await get_history_message(phone, language))
        return

    if _matches_any(lower_text, ["status", "plan", "trial", "remaining", "balance", "credit"]):
        await safe_send_text(phone, await get_status_message(phone, language))
        return

    if await user_can_submit_report(report_count, subscription_paid, paid_credits, payments_on):
        remaining = max(FREE_REPORT_LIMIT - report_count, 0)
        await safe_send_text(
            phone,
            get_report_prompt(remaining, subscription_paid, paid_credits, payments_on, language),
        )
        return

    await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on, language)


async def handle_media(
    phone: str,
    media_id: str,
    media_type: str,
    user: dict,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> None:
    language = _get_user_language(user)
    if not language:
        await safe_send_text(phone, get_language_prompt(user.get("name", "")))
        return

    if not await user_can_submit_report(report_count, subscription_paid, paid_credits, payments_on):
        await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on, language)
        return

    await safe_send_text(
        phone,
        _text(
            language,
            "⏳ *Report process ho rahi hai...*\nPlease 30-60 seconds wait karein.\n\nAI aapki report padh raha hai.",
            "⏳ *Your report is being processed...*\nPlease wait for 30-60 seconds.\n\nOur AI is reading your report.",
        ),
    )

    try:
        media_url = await get_whatsapp_media_url(media_id)
        if media_type == "image":
            extracted_text = await extract_text_from_image_url(media_url)
        else:
            extracted_text = await extract_text_from_pdf_url(media_url)

        if len(extracted_text.strip()) < 50:
            await safe_send_text(
                phone,
                _text(
                    language,
                    "❌ Report clearly read nahi ho payi.\n\nDobara bhejte waqt dhyan dein:\n• photo seedhi aur clear ho\n• poori report frame mein ho\n• roshni achhi ho",
                    "❌ We could not clearly read the report.\n\nPlease try again and make sure:\n• the image is straight and clear\n• the full report is visible\n• the lighting is good",
                ),
            )
            return

        explanation = await explain_report_in_hindi(extracted_text, language=language)
        explanation_sent = await safe_send_text(phone, explanation)

        if explanation.startswith("⚠️"):
            return

        if not explanation_sent:
            print(f"Skipping report save for {phone} because explanation delivery failed.")
            return

        await save_report(phone, extracted_text, explanation)
        await increment_report_count(phone)

        used_paid_credit = False
        if payments_on and report_count >= FREE_REPORT_LIMIT and not subscription_paid:
            used_paid_credit = await consume_user_paid_credit(phone)

        new_count = report_count + 1
        remaining = max(FREE_REPORT_LIMIT - new_count, 0)
        credits_left = await get_user_paid_credits(phone)
        await safe_send_text(
            phone,
            get_follow_up_message(
                remaining,
                subscription_paid,
                payments_on,
                used_paid_credit,
                credits_left,
                language,
            ),
        )
    except Exception as exc:
        print(f"Media processing error: {exc}")
        await safe_send_text(
            phone,
            _text(
                language,
                "⚠️ Report process karte waqt error aaya.\nThodi der baad dobara try karein. Agar issue repeat ho to support se contact karein.",
                "⚠️ There was an error while processing your report.\nPlease try again after a short while. If the issue continues, contact support.",
            ),
        )


async def send_upi_payment_prompt(
    phone: str,
    report_count: int,
    paid_credits: int,
    payments_on: bool,
    language: str,
) -> None:
    if not payments_on:
        await safe_send_text(
            phone,
            _text(
                language,
                "✅ Is build mein payments disabled hain.\nSeedha apni report ki photo ya PDF bhejiye.",
                "✅ Payments are disabled in this build.\nPlease send your report image or PDF directly.",
            ),
        )
        return

    if payment_provider() == "razorpay":
        await safe_send_text(phone, await get_razorpay_payment_message(phone, language))
        return

    if not upi_manual_enabled():
        await safe_send_text(
            phone,
            _text(
                language,
                "⚠️ Payment system abhi configure nahi hai. Support se contact karein.",
                "⚠️ The payment system is not configured yet. Please contact support.",
            ),
        )
        return

    payment_request = await get_or_create_upi_request(phone)
    price = payment_request.get("amount_inr", get_report_price_inr())
    payment_ref = payment_request["payment_ref"]
    upi_uri = payment_request["upi_uri"]
    payee_name = os.getenv("UPI_PAYEE_NAME", "BioVision")
    qr_url = _upi_qr_url(payment_ref)

    message = _text(
        language,
        f"💳 *Report unlock payment*\n\nEk paid report ke liye *₹{price}* pay kariye.\nUPI ID: *{os.getenv('UPI_VPA', '')}*\nPayee: *{payee_name}*\nRef: *{payment_ref}*\n\nTap to pay:\n{upi_uri}\n\nPayment ke baad yahin reply kariye:\n`paid YOUR_UTR`\n\nAgar UTR nahi pata ho to apne UPI app mein transaction reference dekhiye.",
        f"💳 *Unlock your report*\n\nPlease pay *₹{price}* for one paid report.\nUPI ID: *{os.getenv('UPI_VPA', '')}*\nPayee: *{payee_name}*\nRef: *{payment_ref}*\n\nTap to pay:\n{upi_uri}\n\nAfter payment, reply here with:\n`paid YOUR_UTR`\n\nIf you do not know the UTR, please check the transaction reference in your UPI app.",
    )
    await safe_send_text(phone, message)

    if qr_url:
        await safe_send_image(
            phone,
            qr_url,
            _text(
                language,
                f"QR scan karke ₹{price} pay karein. Payment ke baad `paid YOUR_UTR` bhejiye.",
                f"Scan this QR to pay ₹{price}. After payment, send `paid YOUR_UTR`.",
            ),
        )

    if report_count >= FREE_REPORT_LIMIT:
        await safe_send_text(
            phone,
            _text(
                language,
                f"Aapke free reports use ho chuke hain. Paid credits available: {paid_credits}",
                f"Your free reports are already used. Paid credits available: {paid_credits}",
            ),
        )


async def handle_payment_proof_submission(phone: str, utr: str, language: str) -> str:
    if not utr:
        return _text(
            language,
            "⚠️ UTR missing hai. Example: `paid 123456789012`",
            "⚠️ UTR is missing. Example: `paid 123456789012`",
        )

    payment = await submit_upi_payment_utr(phone, utr)
    if not payment:
        return _text(
            language,
            "⚠️ Koi pending payment request nahi mili. Pehle `pay` bhejiye.",
            "⚠️ No pending payment request was found. Please send `pay` first.",
        )

    return _text(
        language,
        "✅ Payment proof receive ho gaya.\n"
        f"Ref: {payment['payment_ref']}\n"
        "Team verification ke baad aapko WhatsApp par credit mil jayega.\n"
        "Usually yeh kuch minutes mein ho jana chahiye.",
        "✅ Payment proof received.\n"
        f"Ref: {payment['payment_ref']}\n"
        "Once our team verifies it, your WhatsApp account will get the report credit.\n"
        "This usually takes a few minutes.",
    )


def get_pricing_message(payments_on: bool, language: str) -> str:
    if not payments_on:
        return _text(
            language,
            "💰 *BioVision Pricing*\n\nIs build mein payments disabled hain.\nAap reports free mein bhej sakte hain.",
            "💰 *BioVision Pricing*\n\nPayments are disabled in this build.\nYou can send reports for free.",
        )

    if payment_provider() == "upi_manual":
        price = get_report_price_inr()
        return _text(
            language,
            f"💰 *BioVision Pricing*\n\n🎁 First {FREE_REPORT_LIMIT} reports free\n💳 Uske baad *₹{price} per report*\nUPI se direct payment kar sakte hain.\nPayment link/QR ke liye `pay` bhejiye.",
            f"💰 *BioVision Pricing*\n\n🎁 First {FREE_REPORT_LIMIT} reports are free\n💳 After that, it is *₹{price} per report*\nYou can pay directly by UPI.\nSend `pay` to receive the payment link/QR.",
        )

    return _text(
        language,
        "💰 *BioVision Pricing*\n\n🎁 Free Trial: 3 reports free\n⭐ Premium Plan: ₹199 / 30 days\nPremium activate karne ke liye `pay` bhejiye.",
        "💰 *BioVision Pricing*\n\n🎁 Free Trial: 3 reports free\n⭐ Premium Plan: ₹199 / 30 days\nSend `pay` to activate Premium.",
    )


async def get_razorpay_payment_message(phone: str, language: str) -> str:
    try:
        payment_link = await create_payment_link(phone, "Patient")
        payment_text = f"👉 Premium unlock link:\n{payment_link}"
    except Exception as exc:
        print(f"Payment link creation failed: {exc}")
        payment_text = _text(
            language,
            "Razorpay link abhi generate nahi ho paya. Team se contact karein.",
            "We could not generate the Razorpay link right now. Please contact support.",
        )

    return _text(
        language,
        "⚠️ *Aapke free reports use ho chuke hain.*\n\n"
        "Unlimited reports ke liye Premium plan ₹199 / 30 days hai.\n"
        f"{payment_text}\n\n"
        "Payment ke baad report bhejte hi access unlock ho jayega.",
        "⚠️ *Your free reports are already used.*\n\n"
        "For unlimited reports, the Premium plan is ₹199 / 30 days.\n"
        f"{payment_text}\n\n"
        "After payment, your access will unlock automatically when you send the report.",
    )


async def get_history_message(phone: str, language: str) -> str:
    reports = await get_user_reports(phone, limit=5)
    if not reports:
        return _text(
            language,
            "📂 *History abhi empty hai.*\n\nApni pehli report ki photo ya PDF bhejiye, main uska summary save kar dunga.",
            "📂 *Your history is empty right now.*\n\nSend your first report image or PDF and I will save its summary here.",
        )

    lines = [_text(language, "📂 *Aapki recent reports:*", "📂 *Your recent reports:*"), ""]
    for index, report in enumerate(reports, start=1):
        created_at = _format_date(report.get("created_at", ""))
        explanation = (report.get("explanation") or "").replace("\n", " ").strip()
        snippet = explanation[:110].strip()
        if len(explanation) > 110:
            snippet += "..."
        lines.append(f"{index}. {created_at} - {snippet or 'Summary saved'}")

    return "\n".join(lines)


async def get_status_message(phone: str, language: str) -> str:
    details = await get_user_subscription_details(phone)
    if not details:
        return _text(
            language,
            "Status dekhne ke liye pehle is number se bot use karein.",
            "Use the bot once from this number first to view your status.",
        )

    report_count = details.get("report_count", 0)
    paid_until_raw = details.get("paid_until")
    subscription_paid = await is_user_paid(phone)
    paid_credits = await get_user_paid_credits(phone)
    pending_payment = await get_latest_open_upi_payment(phone)

    lines = [
        _text(language, "📊 *Aapka account status*", "📊 *Your account status*"),
        f"Used reports: {report_count}",
        f"Paid credits: {paid_credits}",
    ]

    if subscription_paid and paid_until_raw:
        lines.append(f"Premium active till: {_format_date(paid_until_raw, include_time=True)}")

    if pending_payment:
        lines.append(
            f"Pending payment: {pending_payment['payment_ref']} ({pending_payment.get('status', 'pending')})"
        )

    remaining_free = max(FREE_REPORT_LIMIT - report_count, 0)
    lines.append(f"Free reports remaining: {remaining_free}")
    return "\n".join(lines)


def get_welcome_message(
    name: str,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
    language: str,
) -> str:
    remaining = max(FREE_REPORT_LIMIT - report_count, 0)
    if subscription_paid:
        plan_line = _text(
            language,
            "⭐ Premium active hai. Aap unlimited reports bhej sakte hain.",
            "⭐ Premium is active. You can send unlimited reports.",
        )
    elif payments_on and payment_provider() == "upi_manual":
        plan_line = _text(
            language,
            f"🎁 Aapke paas {remaining} free report(s) bachi hain.\n💳 Paid credits: {paid_credits}",
            f"🎁 You have {remaining} free report(s) left.\n💳 Paid credits: {paid_credits}",
        )
    elif not payments_on:
        plan_line = _text(
            language,
            "🆓 Demo mode active hai. Aap reports free mein bhej sakte hain.",
            "🆓 Demo mode is active. You can send reports for free.",
        )
    else:
        plan_line = _text(
            language,
            f"🎁 Aapke paas {remaining} free report(s) bachi hain.",
            f"🎁 You have {remaining} free report(s) left.",
        )

    return _text(
        language,
        f"🩺 *BioVision mein swagat hai!*\n\nNamaste {name or 'ji'}.\n"
        "Main aapki blood test report ko simple Hindi mein explain karta hoon.\n\n"
        f"{plan_line}\n\n"
        "Photo ya PDF bhejiye aur main important values, normal range, aur doctor se poochne wale sawal bataunga.\n\n"
        "Commands: help, price, pay, paid <UTR>, history, status, language",
        f"🩺 *Welcome to BioVision!*\n\nHello {name or 'there'}.\n"
        "I explain blood test reports in simple English.\n\n"
        f"{plan_line}\n\n"
        "Send a report image or PDF and I will highlight important values, normal ranges, and useful doctor questions.\n\n"
        "Commands: help, price, pay, paid <UTR>, history, status, language",
    )


def get_how_to_use(language: str) -> str:
    return _text(
        language,
        "📘 *BioVision kaise use karein*\n\n"
        "1. Report ki clear photo ya PDF bhejiye\n"
        "2. 30-60 seconds wait kariye\n"
        "3. Hindi summary, abnormal values, aur doctor questions mil jayenge\n\n"
        "Free reports khatam hone ke baad `pay` bhejiye aur UPI se payment karke `paid <UTR>` reply karein.",
        "📘 *How to use BioVision*\n\n"
        "1. Send a clear image or PDF of the report\n"
        "2. Wait for 30-60 seconds\n"
        "3. You will receive a simple summary, abnormal values, and doctor questions\n\n"
        "After your free reports are used, send `pay`, complete the UPI payment, and reply with `paid <UTR>`.",
    )


def get_report_prompt(
    remaining: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
    language: str,
) -> str:
    if subscription_paid:
        header = _text(
            language,
            "📄 Premium active hai. Apni report ki photo ya PDF bhejiye.",
            "📄 Premium is active. Please send your report image or PDF.",
        )
    elif payments_on and paid_credits > 0:
        header = _text(
            language,
            f"📄 Aapke paas {paid_credits} paid credit(s) hain. Report bhejiye.",
            f"📄 You have {paid_credits} paid credit(s). Please send your report.",
        )
    elif not payments_on:
        header = _text(
            language,
            "📄 Demo mode active hai. Apni report ki photo ya PDF bhejiye.",
            "📄 Demo mode is active. Please send your report image or PDF.",
        )
    else:
        header = _text(
            language,
            f"📄 Apni report ki photo ya PDF bhejiye.\nAapke paas {remaining} free report(s) bachi hain.",
            f"📄 Please send your report image or PDF.\nYou have {remaining} free report(s) left.",
        )

    return _text(
        language,
        f"{header}\n\nTip:\n• image clear ho\n• report flat ho\n• text cut na ho",
        f"{header}\n\nTips:\n• keep the image clear\n• keep the report flat\n• make sure the text is not cut off",
    )


def get_follow_up_message(
    remaining: int,
    subscription_paid: bool,
    payments_on: bool,
    used_paid_credit: bool,
    credits_left: int,
    language: str,
) -> str:
    if subscription_paid:
        plan_line = _text(
            language,
            "Premium active hai, aap aur reports bhi bhej sakte hain.",
            "Premium is active, so you can send more reports.",
        )
    elif used_paid_credit:
        plan_line = _text(
            language,
            f"Ek paid credit use hua. Credits left: {credits_left}",
            f"One paid credit was used. Credits left: {credits_left}",
        )
    elif not payments_on:
        plan_line = _text(
            language,
            "Demo mode active hai, aap aur reports bhi bhej sakte hain.",
            "Demo mode is active, so you can send more reports.",
        )
    else:
        plan_line = f"Free reports remaining: {remaining}"

    return _text(
        language,
        "━━━━━━━━━━━━━━━━━━\n"
        "⚕️ Ye educational explanation hai. Final medical advice ke liye doctor se consult karein.\n\n"
        f"{plan_line}\n"
        "History dekhne ke liye `history` bhejiye.",
        "━━━━━━━━━━━━━━━━━━\n"
        "⚕️ This is educational information only. Please consult a doctor for final medical advice.\n\n"
        f"{plan_line}\n"
        "Send `history` to view your saved reports.",
    )


def _whatsapp_provider() -> str:
    provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
    return provider or "meta"


def _extract_phone_from_remote_jid(remote_jid: str) -> str:
    value = (remote_jid or "").strip()
    if not value or value == "status@broadcast" or value.endswith("@g.us"):
        return ""
    local_part = value.split("@", 1)[0]
    digits = "".join(character for character in local_part if character.isdigit())
    return digits or local_part


def _unwrap_evolution_message(message: dict[str, Any]) -> dict[str, Any]:
    current = message or {}
    while isinstance(current, dict):
        for wrapper in (
            "ephemeralMessage",
            "viewOnceMessage",
            "viewOnceMessageV2",
            "viewOnceMessageV2Extension",
            "documentWithCaptionMessage",
        ):
            payload = current.get(wrapper)
            if isinstance(payload, dict):
                nested = payload.get("message")
                if isinstance(nested, dict):
                    current = nested
                    break
        else:
            return current
    return {}


def _extract_evolution_text(message: dict[str, Any]) -> str:
    current = _unwrap_evolution_message(message)
    if current.get("conversation"):
        return str(current["conversation"])
    extended = current.get("extendedTextMessage", {})
    if isinstance(extended, dict) and extended.get("text"):
        return str(extended["text"])
    image = current.get("imageMessage", {})
    if isinstance(image, dict) and image.get("caption"):
        return str(image["caption"])
    document = current.get("documentMessage", {})
    if isinstance(document, dict) and document.get("caption"):
        return str(document["caption"])
    return ""


def _detect_evolution_message_type(message: dict[str, Any]) -> str:
    current = _unwrap_evolution_message(message)
    if current.get("conversation") or (isinstance(current.get("extendedTextMessage"), dict) and current["extendedTextMessage"].get("text")):
        return "text"
    if isinstance(current.get("imageMessage"), dict):
        return "image"
    if isinstance(current.get("documentMessage"), dict):
        return "document"
    return ""


def _find_evolution_media_url(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("mediaUrl", "mediaURL", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://", "data:")):
                return value
        for value in payload.values():
            found = _find_evolution_media_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_evolution_media_url(value)
            if found:
                return found
    return ""


async def _get_evolution_media_data_uri(message_object: dict[str, Any], mime_type: str = "") -> str:
    api_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
    api_key = os.getenv("EVOLUTION_API_KEY", "").strip()
    instance_name = os.getenv("EVOLUTION_INSTANCE_NAME", "").strip()

    if not api_url or not api_key or not instance_name:
        raise RuntimeError("Missing Evolution API configuration for media fetch.")

    payload = {"message": message_object}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{api_url}/chat/getBase64FromMediaMessage/{instance_name}",
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    base64_value = (data.get("base64") or "").strip()
    if not base64_value:
        raise RuntimeError("Evolution API returned empty media payload.")

    resolved_mime = (data.get("mimetype") or mime_type or "application/octet-stream").strip()
    return f"data:{resolved_mime};base64,{base64_value}"


def extract_message_payloads(body: dict) -> list[dict[str, Any]]:
    if body.get("event") == "messages.upsert" and isinstance(body.get("data"), dict):
        event_data = body["data"]
        key = event_data.get("key", {}) or {}
        if key.get("fromMe"):
            return []

        phone = _extract_phone_from_remote_jid(str(key.get("remoteJid", "")))
        if not phone:
            return []

        message = event_data.get("message", {}) or {}
        message_type = _detect_evolution_message_type(message)
        if not message_type:
            return []

        payload: dict[str, Any] = {
            "from": phone,
            "type": message_type,
            "name": (event_data.get("pushName") or "").strip(),
            "source_provider": "evolution",
            "message_object": event_data,
            "media_url": _find_evolution_media_url(message) or _find_evolution_media_url(event_data),
        }

        if message_type == "text":
            payload["text"] = _extract_evolution_text(message)
        elif message_type == "document":
            document_message = _unwrap_evolution_message(message).get("documentMessage", {}) or {}
            payload["mime_type"] = document_message.get("mimetype", "")
        elif message_type == "image":
            image_message = _unwrap_evolution_message(message).get("imageMessage", {}) or {}
            payload["mime_type"] = image_message.get("mimetype", "")

        return [payload]

    payloads: list[dict[str, Any]] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", []) or []
            contacts = value.get("contacts", []) or []
            contact_name = ""
            if contacts:
                contact_name = contacts[0].get("profile", {}).get("name", "").strip()

            for message in messages:
                message_type = message.get("type")
                payload: dict[str, Any] = {
                    "from": message.get("from", ""),
                    "type": message_type,
                    "name": contact_name,
                    "source_provider": "meta",
                }

                if message_type == "text":
                    payload["text"] = message.get("text", {}).get("body", "")
                elif message_type == "image":
                    payload["media_id"] = message.get("image", {}).get("id", "")
                elif message_type == "document":
                    payload["media_id"] = message.get("document", {}).get("id", "")
                    payload["mime_type"] = message.get("document", {}).get("mime_type", "")

                if payload.get("from") and payload.get("type"):
                    payloads.append(payload)

    return payloads


async def get_whatsapp_media_url(message_data: dict[str, Any]) -> str:
    source_provider = message_data.get("source_provider") or _whatsapp_provider()
    if source_provider == "evolution":
        media_url = (message_data.get("media_url") or "").strip()
        if media_url:
            return media_url

        message_object = message_data.get("message_object")
        if not isinstance(message_object, dict):
            raise RuntimeError("Evolution API media payload missing message object.")

        return await _get_evolution_media_data_uri(message_object, str(message_data.get("mime_type", "")))

    media_id = str(message_data.get("media_id", "")).strip()
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Missing required environment variable: WHATSAPP_ACCESS_TOKEN")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()

    media_url = data.get("url")
    if not media_url:
        raise RuntimeError("WhatsApp media URL missing in response.")
    return media_url


async def process_message_job(message_data: dict[str, Any]) -> None:
    app.state.active_jobs += 1

    try:
        phone = message_data["from"]
        contact_name = message_data.get("name", "")
        message_type = message_data["type"]

        user = await get_or_create_user(phone, contact_name)
        report_count = await get_user_report_count(phone)
        subscription_paid = await is_user_paid(phone)
        paid_credits = await get_user_paid_credits(phone)
        payments_on = payments_enabled()

        if message_type == "text":
            incoming_text = message_data.get("text", "").strip()
            await handle_text(phone, incoming_text, user, report_count, subscription_paid, paid_credits, payments_on)
        elif message_type == "image":
            async with app.state.media_processing_semaphore:
                await handle_media(
                    phone,
                    message_data,
                    user,
                    report_count,
                    subscription_paid,
                    paid_credits,
                    payments_on,
                )
        elif message_type == "document":
            mime_type = message_data.get("mime_type", "")
            if "pdf" not in mime_type.lower():
                await safe_send_text(
                    phone,
                    _text(
                        _get_user_language(user),
                        "❌ Abhi sirf report ki photo ya PDF support hoti hai. Kripya wahi bhejein.",
                        "❌ Only report images and PDFs are supported right now. Please send one of those.",
                    ),
                )
            else:
                async with app.state.media_processing_semaphore:
                    await handle_media(
                        phone,
                        message_data,
                        user,
                        report_count,
                        subscription_paid,
                        paid_credits,
                        payments_on,
                    )
        else:
            await safe_send_text(
                phone,
                _text(
                    _get_user_language(user),
                    "📎 Is type ka message abhi support nahi hai.\nPhoto, PDF, ya text message bhejiye.",
                    "📎 This message type is not supported yet.\nPlease send a photo, PDF, or text message.",
                ),
            )
    except Exception as exc:
        print(f"Background message processing error: {exc}")
    finally:
        app.state.active_jobs = max(0, app.state.active_jobs - 1)


async def handle_media(
    phone: str,
    message_data: dict[str, Any],
    user: dict,
    report_count: int,
    subscription_paid: bool,
    paid_credits: int,
    payments_on: bool,
) -> None:
    language = _get_user_language(user)
    if not language:
        await safe_send_text(phone, get_language_prompt(user.get("name", "")))
        return

    if not await user_can_submit_report(report_count, subscription_paid, paid_credits, payments_on):
        await send_upi_payment_prompt(phone, report_count, paid_credits, payments_on, language)
        return

    await safe_send_text(
        phone,
        _text(
            language,
            "⏳ *Report process ho rahi hai...*\nPlease 30-60 seconds wait karein.\n\nAI aapki report padh raha hai.",
            "⏳ *Your report is being processed...*\nPlease wait for 30-60 seconds.\n\nOur AI is reading your report.",
        ),
    )

    try:
        media_source = await get_whatsapp_media_url(message_data)
        if message_data["type"] == "image":
            extracted_text = await extract_text_from_image_url(media_source)
        else:
            extracted_text = await extract_text_from_pdf_url(media_source)

        if len(extracted_text.strip()) < 50:
            await safe_send_text(
                phone,
                _text(
                    language,
                    "❌ Report clearly read nahi ho payi.\n\nDobara bhejte waqt dhyan dein:\n• photo seedhi aur clear ho\n• poori report frame mein ho\n• roshni achhi ho",
                    "❌ We could not clearly read the report.\n\nPlease try again and make sure:\n• the image is straight and clear\n• the full report is visible\n• the lighting is good",
                ),
            )
            return

        explanation = await explain_report_in_hindi(extracted_text, language=language)
        explanation_sent = await safe_send_text(phone, explanation)

        if explanation.startswith("⚠️"):
            return

        if not explanation_sent:
            print(f"Skipping report save for {phone} because explanation delivery failed.")
            return

        await save_report(phone, extracted_text, explanation)
        await increment_report_count(phone)

        used_paid_credit = False
        if payments_on and report_count >= FREE_REPORT_LIMIT and not subscription_paid:
            used_paid_credit = await consume_user_paid_credit(phone)

        new_count = report_count + 1
        remaining = max(FREE_REPORT_LIMIT - new_count, 0)
        credits_left = await get_user_paid_credits(phone)
        await safe_send_text(
            phone,
            get_follow_up_message(
                remaining,
                subscription_paid,
                payments_on,
                used_paid_credit,
                credits_left,
                language,
            ),
        )
    except Exception as exc:
        print(f"Media processing error: {exc}")
        await safe_send_text(
            phone,
            _text(
                language,
                "⚠️ Report process karte waqt error aaya.\nThodi der baad dobara try karein. Agar issue repeat ho to support se contact karein.",
                "⚠️ There was an error while processing your report.\nPlease try again after a short while. If the issue continues, contact support.",
            ),
        )
