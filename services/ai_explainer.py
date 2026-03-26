"""
services/ai_explainer.py
========================
Patient-friendly Hindi explanations powered by Claude.
"""

from __future__ import annotations

import os

import anthropic
from dotenv import load_dotenv
import httpx

load_dotenv()


SYSTEM_PROMPT = """
Tum ek experienced medical educator ho jo patients ko unki lab reports
samjhane mein madad karta hai.

Tumhara kaam:
- Lab report ka text padho
- Har test ka naam, value, aur normal range identify karo
- Har test ko simple Hindi mein explain karo
- Abnormal values clearly highlight karo
- Patient ko samjhao ki kis cheez par dhyan dena chahiye

Formatting rules:
- WhatsApp friendly format use karo
- *Bold text* ke liye asterisk use karo
- Emojis readable aur helpful hone chahiye
- 🔴 = high, 🟡 = borderline, 🟢 = normal, ⬇️ = low

Output format:
━━━━━━━━━━━━━━━━━━
🩺 *AAPKI REPORT KA SUMMARY*
━━━━━━━━━━━━━━━━━━

📊 *IMPORTANT TESTS:*

[Har important test ke liye]
🔴/🟢/🟡/⬇️ *[TEST NAME]*
📌 Aapka result: [VALUE]
✅ Normal range: [RANGE]
💬 Matlab: [2-3 short lines]

━━━━━━━━━━━━━━━━━━
⚠️ *DHYAN DENE WALI BAATEIN:*
[Sirf important abnormal ya borderline values]

━━━━━━━━━━━━━━━━━━
❓ *DOCTOR SE YE SAWAL POOCHEN:*
[3-4 useful questions]

━━━━━━━━━━━━━━━━━━
⚕️ *DISCLAIMER:*
Ye sirf educational information hai. Final medical advice ke liye doctor se baat karein.

Important rules:
- Kabhi diagnosis claim mat karo
- Kabhi mat bolo ki patient ko koi disease pakka hai
- Agar text unclear ho to honestly bolo
- Sirf Hindi mein jawab do
- Maximum lagbhag 700 words
- Sirf report ke facts explain karo, extra assumptions mat banao
"""


def _get_client() -> anthropic.AsyncAnthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: ANTHROPIC_API_KEY")
    return anthropic.AsyncAnthropic(api_key=api_key)


def _use_ollama() -> bool:
    provider = os.getenv("AI_PROVIDER", "").strip().lower()
    if provider == "ollama":
        return True
    return not os.getenv("ANTHROPIC_API_KEY")


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
        "options": {
            "temperature": 0.2,
        },
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


async def explain_report_in_hindi(ocr_text: str) -> str:
    """Send OCR text to Claude and get a Hindi explanation."""
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
        print(f"AI generated {len(explanation)} characters")
        return explanation or "⚠️ AI response empty aaya. Kripya dobara try karein."

    except RuntimeError as exc:
        print(f"AI configuration error: {exc}")
        return (
            "⚠️ AI service abhi configure nahi hai.\n"
            "Anthropic key set karein ya local Ollama start karein."
        )
    except httpx.HTTPError as exc:
        print(f"AI HTTP error: {exc}")
        return (
            "⚠️ AI service se connect nahi ho paya.\n"
            "Agar free local mode use kar rahe hain to Ollama start karein."
        )
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
