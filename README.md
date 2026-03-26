# BioVision WhatsApp Bot

BioVision WhatsApp Bot helps users understand blood test and lab reports in simple Hindi using FastAPI, OpenRouter or Claude AI, OCR, WhatsApp Cloud API, and direct UPI or Razorpay payment flows.

## Features

- WhatsApp webhook integration with Meta WhatsApp Cloud API
- Hindi lab report explanations powered by OpenRouter, Anthropic, or Ollama
- OCR support for report images and PDFs
- Free trial with paid report unlock flow
- Direct UPI payment link and QR flow with admin approval
- Report history stored in SQLite

## Tech Stack

- Python
- FastAPI
- OpenRouter / Anthropic / Ollama
- Google Vision OCR
- WhatsApp Cloud API
- UPI / Razorpay
- SQLite

## Local Run

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn main:app --reload --port 8000
```

Open:

- `http://127.0.0.1:8000`
- `http://127.0.0.1:8000/health`

## Environment Variables

Create a `.env` file from `.env.example` and fill in:

- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_VERIFY_TOKEN`
- `OPENROUTER_API_KEY`
- `GOOGLE_VISION_API_KEY`
- `UPI_VPA`
- `UPI_PAYEE_NAME`
- `ADMIN_API_KEY`
- `APP_URL`

## Deployment

This project is ready to deploy on Railway using `railway.toml`.

## Notes

- `.env` is ignored by Git and should never be committed.
- The current database is SQLite. For production, use a persistent volume or migrate to PostgreSQL.
