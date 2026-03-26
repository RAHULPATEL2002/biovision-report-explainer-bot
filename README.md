# BioVision WhatsApp Bot

BioVision WhatsApp Bot helps users understand blood test and lab reports in simple Hindi or English using FastAPI, OpenRouter or Claude AI, OCR, and either Evolution API or WhatsApp Cloud API, plus direct UPI or Razorpay payment flows.

## Features

- WhatsApp webhook integration with Evolution API or Meta WhatsApp Cloud API
- Hindi and English lab report explanations powered by OpenRouter, Anthropic, or Ollama
- OCR support for report images and PDFs through OCR.Space, Google Vision, or Tesseract
- Free trial with paid report unlock flow
- Direct UPI payment link and QR flow with admin approval
- Report history stored in SQLite
- Dockerfile included for VM / EasyPanel style deployment

## Tech Stack

- Python
- FastAPI
- OpenRouter / Anthropic / Ollama
- OCR.Space / Google Vision / Tesseract OCR
- Evolution API / WhatsApp Cloud API
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

- `WHATSAPP_PROVIDER`
- `EVOLUTION_API_URL` / `EVOLUTION_API_KEY` / `EVOLUTION_INSTANCE_NAME`
- or `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_VERIFY_TOKEN`
- `OPENROUTER_API_KEY`
- `OCR_SPACE_API_KEY` or `GOOGLE_VISION_API_KEY`
- `UPI_VPA`
- `UPI_PAYEE_NAME`
- `ADMIN_API_KEY`
- `APP_URL`

## Deployment

This project can be deployed on Railway using `railway.toml`, or on a VM / EasyPanel using the included `Dockerfile`.

## Notes

- `.env` is ignored by Git and should never be committed.
- The current database is SQLite. For production, use a persistent volume or migrate to PostgreSQL.
