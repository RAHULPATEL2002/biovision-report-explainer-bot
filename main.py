"""
BioVision WhatsApp Bot main application.

Run locally:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from database.users import (
    get_or_create_user,
    get_user_report_count,
    get_user_reports,
    get_user_subscription_details,
    increment_report_count,
    init_db,
    save_report,
)
from services.ai_explainer import explain_report_in_hindi
from services.ocr import extract_text_from_image_url, extract_text_from_pdf_url
from services.payment import (
    activate_subscription_from_payment_link,
    create_payment_link,
    handle_payment_webhook,
    is_user_paid,
    verify_callback_signature,
)
from services.whatsapp import send_text_message

load_dotenv()

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "biovision2025")
FREE_REPORT_LIMIT = 3


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="BioVision WhatsApp Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "BioVision Bot is running! 🩺"}


@app.get("/health")
async def health_check() -> dict[str, Any]:
    configured = {
        "whatsapp_access_token": bool(os.getenv("WHATSAPP_ACCESS_TOKEN")),
        "whatsapp_phone_number_id": bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
        "anthropic_api_key": bool(os.getenv("ANTHROPIC_API_KEY")),
        "google_vision_api_key": bool(os.getenv("GOOGLE_VISION_API_KEY")),
        "razorpay_key_id": bool(os.getenv("RAZORPAY_KEY_ID")),
        "razorpay_key_secret": bool(os.getenv("RAZORPAY_KEY_SECRET")),
        "razorpay_webhook_secret": bool(os.getenv("RAZORPAY_WEBHOOK_SECRET")),
        "app_url": bool(os.getenv("APP_URL")),
    }
    return {
        "status": "ok",
        "verify_token_configured": bool(os.getenv("WHATSAPP_VERIFY_TOKEN")),
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
async def receive_message(request: Request) -> dict[str, str]:
    body = await request.json()

    try:
        message_data = extract_message_payload(body)
        if not message_data:
            return {"status": "ignored"}

        phone = message_data["from"]
        contact_name = message_data.get("name", "")
        message_type = message_data["type"]

        user = await get_or_create_user(phone, contact_name)
        report_count = await get_user_report_count(phone)
        paid = await is_user_paid(phone)

        if message_type == "text":
            incoming_text = message_data.get("text", "").strip().lower()
            await handle_text(phone, incoming_text, user, report_count, paid)
        elif message_type == "image":
            await handle_media(phone, message_data["media_id"], "image", user, report_count, paid)
        elif message_type == "document":
            mime_type = message_data.get("mime_type", "")
            if "pdf" not in mime_type.lower():
                await safe_send_text(
                    phone,
                    "❌ Abhi sirf report ki photo ya PDF support hoti hai. Kripya wahi bhejein.",
                )
            else:
                await handle_media(phone, message_data["media_id"], "pdf", user, report_count, paid)
        else:
            await safe_send_text(
                phone,
                "📎 Is type ka message abhi support nahi hai.\nPhoto, PDF, ya text message bhejiye.",
            )

    except Exception as exc:
        print(f"Webhook processing error: {exc}")

    return {"status": "ok"}


@app.post("/payments/razorpay/webhook")
async def razorpay_webhook(request: Request) -> dict[str, bool]:
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
    query_params = {key: value for key, value in request.query_params.items()}
    payment_link_status = query_params.get("razorpay_payment_link_status", "").lower()
    payment_link_id = query_params.get("razorpay_payment_link_id", "")

    if payment_link_status != "paid":
        return HTMLResponse(
            content=_payment_page_html(
                "Payment Pending",
                "Payment abhi confirm nahi hua. Agar paise कट गए hain to 1-2 minute baad WhatsApp check karein.",
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


async def handle_text(
    phone: str,
    text: str,
    user: dict,
    report_count: int,
    paid: bool,
) -> None:
    if _matches_any(text, ["hi", "hello", "helo", "hey", "start", "menu", "help", "namaste"]):
        await safe_send_text(phone, get_welcome_message(user.get("name", ""), report_count, paid))
        return

    if _matches_any(text, ["kaise", "how", "use", "steps", "guide"]):
        await safe_send_text(phone, get_how_to_use())
        return

    if _matches_any(text, ["price", "pricing", "cost", "kitna"]):
        await safe_send_text(phone, get_pricing_message())
        return

    if _matches_any(text, ["pay", "premium", "subscription", "subscribe", "unlock"]):
        await safe_send_text(phone, await get_payment_required_message(phone, user.get("name", "")))
        return

    if _matches_any(text, ["history", "reports", "past report", "old report"]):
        await safe_send_text(phone, await get_history_message(phone))
        return

    if _matches_any(text, ["status", "plan", "trial", "remaining", "balance"]):
        await safe_send_text(phone, await get_status_message(phone))
        return

    if report_count < FREE_REPORT_LIMIT or paid:
        remaining = max(FREE_REPORT_LIMIT - report_count, 0)
        await safe_send_text(phone, get_report_prompt(remaining, paid))
        return

    await safe_send_text(phone, await get_payment_required_message(phone, user.get("name", "")))


async def handle_media(
    phone: str,
    media_id: str,
    media_type: str,
    user: dict,
    report_count: int,
    paid: bool,
) -> None:
    if report_count >= FREE_REPORT_LIMIT and not paid:
        await safe_send_text(phone, await get_payment_required_message(phone, user.get("name", "")))
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

        new_count = report_count + 1
        remaining = max(FREE_REPORT_LIMIT - new_count, 0)
        await safe_send_text(phone, get_follow_up_message(remaining, paid))

    except Exception as exc:
        print(f"Media processing error: {exc}")
        await safe_send_text(
            phone,
            "⚠️ Report process karte waqt error aaya.\n"
            "Thodi der baad dobara try karein. Agar issue repeat ho to support se contact karein.",
        )


async def safe_send_text(phone: str, text: str) -> bool:
    try:
        await send_text_message(phone, text)
        return True
    except Exception as exc:
        print(f"Unable to send WhatsApp message to {phone}: {exc}")
        return False


async def get_whatsapp_media_url(media_id: str) -> str:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Missing required environment variable: WHATSAPP_ACCESS_TOKEN")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"https://graph.facebook.com/v18.0/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()

    media_url = data.get("url")
    if not media_url:
        raise RuntimeError("WhatsApp media URL missing in response.")
    return media_url


def extract_message_payload(body: dict) -> dict[str, Any]:
    entries = body.get("entry", [])
    if not entries:
        return {}

    changes = entries[0].get("changes", [])
    if not changes:
        return {}

    value = changes[0].get("value", {})
    messages = value.get("messages")
    if not messages:
        return {}

    contacts = value.get("contacts", [])
    contact_name = ""
    if contacts:
        contact_name = contacts[0].get("profile", {}).get("name", "").strip()

    message = messages[0]
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

    return payload


def _matches_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def get_pricing_message() -> str:
    return (
        "💰 *BioVision Pricing*\n\n"
        "🎁 Free Trial: 3 reports free\n"
        "⭐ Premium Plan: ₹199 / 30 days\n"
        "• Unlimited reports\n"
        "• Fast Hindi explanations\n"
        "• Report history access\n"
        "• Better follow-up tracking\n\n"
        "Premium activate karne ke liye `premium` ya `pay` bhejiye."
    )


async def get_payment_required_message(phone: str, name: str) -> str:
    try:
        payment_link = await create_payment_link(phone, name or "Patient")
        payment_text = f"👉 Premium unlock link:\n{payment_link}"
    except Exception as exc:
        print(f"Payment link creation failed: {exc}")
        payment_text = "Razorpay link abhi generate nahi ho paya. Team se contact karein."

    return (
        "⚠️ *Aapke 3 free reports use ho chuke hain.*\n\n"
        "Unlimited reports ke liye Premium plan ₹199 / 30 days hai.\n"
        f"{payment_text}\n\n"
        "Payment ke baad WhatsApp par report bhejte hi access unlock ho jayega."
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
    paid = await is_user_paid(phone)

    lines = [
        "📊 *Aapka account status*",
        f"Used reports: {report_count}",
    ]

    if paid and paid_until_raw:
        lines.append(f"Premium active till: {_format_date(paid_until_raw, include_time=True)}")
    elif paid:
        lines.append("Premium active hai.")
    else:
        remaining = max(FREE_REPORT_LIMIT - report_count, 0)
        lines.append(f"Free reports remaining: {remaining}")

    return "\n".join(lines)


def get_welcome_message(name: str, report_count: int, paid: bool) -> str:
    remaining = max(FREE_REPORT_LIMIT - report_count, 0)
    plan_line = (
        "⭐ Premium active hai. Aap unlimited reports bhej sakte hain."
        if paid
        else f"🎁 Aapke paas {remaining} free report(s) bachi hain."
    )

    return (
        f"🩺 *BioVision mein swagat hai!*\n\n"
        f"Namaste {name or 'ji'}.\n"
        "Main aapki blood test report ko simple Hindi mein explain karta hoon.\n\n"
        f"{plan_line}\n\n"
        "Photo ya PDF bhejiye aur main important values, normal range, aur doctor se poochne wale sawal bataunga.\n\n"
        "Commands: help, price, history, status"
    )


def get_how_to_use() -> str:
    return (
        "📘 *BioVision kaise use karein*\n\n"
        "1. Report ki clear photo ya PDF bhejiye\n"
        "2. 30-60 seconds wait kariye\n"
        "3. Hindi summary, abnormal values, aur doctor questions mil jayenge\n\n"
        "Best results ke liye:\n"
        "• photo seedhi ho\n"
        "• poori report visible ho\n"
        "• blur na ho\n\n"
        "Supported reports: CBC, sugar, HbA1c, lipid profile, thyroid, LFT, KFT, vitamins, urine aur more."
    )


def get_report_prompt(remaining: int, paid: bool) -> str:
    if paid:
        header = "📄 Premium active hai. Apni report ki photo ya PDF bhejiye."
    else:
        header = f"📄 Apni report ki photo ya PDF bhejiye.\nAapke paas {remaining} free report(s) bachi hain."

    return (
        f"{header}\n\n"
        "Tip:\n"
        "• image clear ho\n"
        "• report flat ho\n"
        "• text cut na ho"
    )


def get_follow_up_message(remaining: int, paid: bool) -> str:
    if paid:
        trial_line = "Premium active hai, isliye aap aur reports bhi bhej sakte hain."
    else:
        trial_line = f"Free reports remaining: {remaining}"

    return (
        "━━━━━━━━━━━━━━━━━━\n"
        "⚕️ Ye educational explanation hai. Final medical advice ke liye doctor se consult karein.\n\n"
        f"{trial_line}\n"
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
