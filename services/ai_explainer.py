"""
services/ai_explainer.py
========================
Patient-friendly Hindi explanations powered by OpenRouter, Anthropic, or Ollama.
"""

from __future__ import annotations

import os

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """
Tum BioVision ke medical AI ho. Tumhara kaam hai ki
lab reports ko simple Hindi mein explain karo jaise
ek dost explain kare - medical jargon bilkul nahi.

Format hamesha readable aur WhatsApp-friendly rakho:

━━━━━━━━━━━━━━━━━━
🩺 *AAPKI REPORT KA SUMMARY*
━━━━━━━━━━━━━━━━━━

Har important test value ke liye:
[EMOJI] *TEST KA NAAM*
📌 Aapka result: [value] [unit]
✅ Normal range: [range]
💬 [Simple Hindi mein 2-3 lines]

Normal values ke liye 🟢
Abnormal values ke liye 🔴
Borderline ke liye 🟡

End mein hamesha:
━━━━━━━━━━━━━━━━━━
❓ *DOCTOR SE YE SAWAL POOCHEN:*
1. [question]
2. [question]
3. [question]

⚕️ Ye sirf educational information hai.
Final medical advice ke liye doctor se milen.

Important rules:
- Diagnosis claim mat karo
- Sirf Hindi mein jawab do
- Report ke facts se bahar assumptions mat banao
- 700 words ke around rakho
"""


def _get_client() -> anthropic.AsyncAnthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: ANTHROPIC_API_KEY")
    return anthropic.AsyncAnthropic(api_key=api_key)


def _ai_provider() -> str:
    provider = os.getenv("AI_PROVIDER", "openrouter").strip().lower()
    if provider:
        return provider
    return "openrouter"


def _use_openrouter() -> bool:
    provider = _ai_provider()
    if provider == "openrouter":
        return True
    return False


def _use_ollama() -> bool:
    provider = _ai_provider()
    if provider == "ollama":
        return True
    return provider not in {"openrouter", "anthropic"} and not os.getenv("ANTHROPIC_API_KEY")


async def _explain_with_ollama(user_prompt: str) -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(f"{base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()

    message = data.get("message", {}) or {}
    content = (message.get("content") or "").strip()
    if not content:
        raise RuntimeError("Ollama returned an empty response.")
    return content


async def _explain_with_openrouter(user_prompt: str) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing required environment variable: OPENROUTER_API_KEY")

    model = os.getenv("AI_MODEL", "openrouter/free").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("APP_URL", "https://biovision.app"),
        "X-Title": "BioVision Lab Report Bot",
    }
    payload = {
        "model": model,
        "max_tokens": 2048,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"].strip()
    if not content:
        raise RuntimeError("OpenRouter returned an empty response.")
    return content


async def explain_report_in_hindi(ocr_text: str) -> str:
    """Send OCR text to the configured AI provider and get a Hindi explanation."""
    if not ocr_text or not ocr_text.strip():
        return "⚠️ Report text mil nahi paya. Kripya clear photo ya PDF dobara bhejein."

    trimmed_text = ocr_text.strip()
    if len(trimmed_text) > 6000:
        trimmed_text = trimmed_text[:6000] + "\n[report truncated]"

    user_prompt = f"""
Neeche lab report ka extracted text diya gaya hai.
Isse patient-friendly Hindi mein explain karo.

LAB REPORT TEXT:
{trimmed_text}
"""

    try:
        if _use_openrouter():
            explanation = await _explain_with_openrouter(user_prompt)
            print(f"OpenRouter generated {len(explanation)} characters")
            return explanation

        if _use_ollama():
            explanation = await _explain_with_ollama(user_prompt)
            print(f"Ollama generated {len(explanation)} characters")
            return explanation

        client = _get_client()
        message = await client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=1400,
            temperature=0.2,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        explanation = ""
        for block in message.content:
            if getattr(block, "type", "") == "text":
                explanation += block.text

        explanation = explanation.strip()
        print(f"Anthropic generated {len(explanation)} characters")
        return explanation or "⚠️ AI response empty aaya. Kripya dobara try karein."

    except RuntimeError as exc:
        print(f"AI configuration error: {exc}")
        return (
            "⚠️ AI service abhi configure nahi hai.\n"
            "OpenRouter key, Anthropic key, ya local Ollama configure karein."
        )
    except httpx.HTTPStatusError as exc:
        print(f"AI HTTP status error: {exc.response.status_code} - {exc.response.text}")
        return "⚠️ AI service se error aaya. Thodi der baad dobara try karein."
    except httpx.HTTPError as exc:
        print(f"AI HTTP error: {exc}")
        return "⚠️ AI service se connect nahi ho paya. Thodi der baad dobara try karein."
    except anthropic.RateLimitError:
        return "⚠️ Abhi server busy hai. 1-2 minute baad dobara report bhejein."
    except anthropic.APIError as exc:
        print(f"Anthropic API error: {exc}")
        return (
            "⚠️ AI service mein temporary problem aayi hai.\n"
            "Thodi der baad dobara try karein."
        )


async def test() -> None:
    sample_report = """
    COMPLETE BLOOD COUNT (CBC)
    Patient: Rahul Patel  Age: 25
    Hemoglobin: 9.2 g/dL  (Normal: 13.0-17.0)
    WBC Count: 11,500 /cumm  (Normal: 4,000-11,000)
    Platelet Count: 1,85,000 /cumm  (Normal: 1,50,000-4,00,000)
    Blood Sugar (Fasting): 118 mg/dL  (Normal: 70-100)
    HbA1c: 6.1%  (Normal: Below 5.7%)
    """
    print(await explain_report_in_hindi(sample_report))


if __name__ == "__main__":
    import asyncio

    asyncio.run(test())
