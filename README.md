# BioVision — AI Blood Report Explainer

A web platform that explains blood test and lab reports in simple Hindi (or English) using Claude AI and OCR. Upload any report from your laptop or mobile browser — no app installation needed.

## Features

- 📄 Upload blood test/lab reports (JPG, PNG, WEBP, PDF)
- 🧠 Claude AI explains your report in simple Hindi or English
- 🔬 Highlights abnormal values, their meaning, and lifestyle tips
- 📂 Session-based history to revisit past reports
- 📱 Fully responsive — works on Chrome, Safari, Firefox (desktop + mobile)
- ⚡ Fast — results in 30–60 seconds

## Tech Stack

- **Backend**: Python, FastAPI
- **AI**: Anthropic Claude (claude-opus-4-5)
- **OCR**: Google Vision API (with pytesseract fallback)
- **PDF**: pdfplumber + PyMuPDF
- **Database**: SQLite (aiosqlite)
- **Frontend**: Vanilla HTML/CSS/JS (no framework needed)

## Local Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Fill in your API keys
uvicorn main:app --reload --port 8000
```

Open: http://localhost:8000

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ Yes | Your Anthropic API key |
| `GOOGLE_VISION_API_KEY` | Optional | Better OCR for images; falls back to pytesseract if not set |

## Deployment (Railway)

1. Push to GitHub
2. Connect repo to [Railway](https://railway.app)
3. Add environment variables in Railway dashboard
4. Deploy — Railway uses `railway.toml` automatically

## Supported Reports

CBC, Blood Sugar, HbA1c, Lipid Profile, Thyroid (T3/T4/TSH), Liver Function (LFT), Kidney Function (KFT), Vitamins (B12, D3), Urine Routine, and more.

## Notes

- OCR accuracy depends on image quality. Use clear, well-lit photos.
- Explanations are for educational purposes only. Always consult a doctor.
- SQLite is used for local/single-instance deployments. For multi-instance production, use PostgreSQL.

## License

MIT
