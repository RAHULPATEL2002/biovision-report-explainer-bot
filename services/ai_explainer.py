"""AI explanation service using Anthropic Claude."""

from __future__ import annotations

import os
import anthropic

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in environment.")
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


HINDI_SYSTEM_PROMPT = """You are BioVision, an expert medical report explainer.
You explain blood test and lab reports in simple, clear Hindi that any non-medical person can understand.

Your explanation must include:
1. 📋 **Report Summary** - Ek line mein report kaisi hai (normal/abnormal/attention needed)
2. 🔬 **Important Values** - Har test ka naam, result, normal range, aur status (normal/high/low)
3. ⚠️ **Abnormal Values** - Jo values normal range se bahar hain, unke baare mein detail mein batao
4. 💡 **Kya matlab hai?** - Simple language mein health implications
5. 🍎 **Lifestyle Tips** - Diet, exercise, ya other simple suggestions
6. 👨‍⚕️ **Doctor se poochhe** - 3-4 important questions jo doctor se poochhe

Rules:
- Use simple, conversational Hindi (Hinglish is fine)
- Be accurate but not alarmist
- Always recommend consulting a doctor for final advice
- Use emojis to make it readable
- Format clearly with sections
- Do not make final medical diagnoses
"""

ENGLISH_SYSTEM_PROMPT = """You are BioVision, an expert medical report explainer.
You explain blood test and lab reports in simple, clear English that any non-medical person can understand.

Your explanation must include:
1. 📋 **Report Summary** - One line overview of how the report looks
2. 🔬 **Key Values** - Each test name, result, normal range, and status (normal/high/low)
3. ⚠️ **Abnormal Values** - Details about values outside normal range
4. 💡 **What does it mean?** - Simple health implications
5. 🍎 **Lifestyle Tips** - Diet, exercise, or other simple suggestions
6. 👨‍⚕️ **Ask Your Doctor** - 3-4 important questions to ask

Rules:
- Use simple, conversational English
- Be accurate but not alarmist
- Always recommend consulting a doctor for final advice
- Use emojis to make it readable
- Format clearly with sections
- Do not make final medical diagnoses
"""


async def explain_report(extracted_text: str, language: str = "hindi") -> str:
    client = _get_client()

    system_prompt = HINDI_SYSTEM_PROMPT if language == "hindi" else ENGLISH_SYSTEM_PROMPT

    user_message = f"""Please explain this lab report:\n\n{extracted_text}"""

    response = await client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text
