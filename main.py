"""
BioVision - AI Blood Report Explainer Web Platform
Run: uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import base64
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from database.reports import init_db, save_report, get_reports, get_report_by_id
from services.ai_explainer import explain_report
from services.ocr import extract_text_from_image_bytes, extract_text_from_pdf_bytes

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="BioVision - AI Report Explainer", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "anthropic_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "google_vision_configured": bool(os.getenv("GOOGLE_VISION_API_KEY")),
    }


@app.post("/api/explain")
async def explain_report_endpoint(
    file: UploadFile = File(...),
    language: str = Form(default="hindi"),
    session_id: str = Form(default=""),
):
    if not session_id:
        session_id = str(uuid.uuid4())

    allowed_types = [
        "image/jpeg", "image/jpg", "image/png", "image/webp",
        "application/pdf",
    ]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="Only JPG, PNG, WEBP images and PDF files are supported.",
        )

    file_bytes = await file.read()

    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Max 10MB allowed.")

    try:
        if file.content_type == "application/pdf":
            extracted_text = await extract_text_from_pdf_bytes(file_bytes)
        else:
            extracted_text = await extract_text_from_image_bytes(file_bytes)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not read report: {exc}")

    if len(extracted_text.strip()) < 30:
        raise HTTPException(
            status_code=422,
            detail="Report text could not be extracted clearly. Please upload a clearer image.",
        )

    try:
        explanation = await explain_report(extracted_text, language=language)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI explanation failed: {exc}")

    report_id = await save_report(
        session_id=session_id,
        filename=file.filename or "report",
        extracted_text=extracted_text,
        explanation=explanation,
        language=language,
    )

    return JSONResponse({
        "report_id": report_id,
        "session_id": session_id,
        "explanation": explanation,
        "filename": file.filename,
    })


@app.get("/api/reports/{session_id}")
async def get_session_reports(session_id: str):
    reports = await get_reports(session_id)
    return {"reports": reports}


@app.get("/api/report/{report_id}")
async def get_single_report(report_id: int):
    report = await get_report_by_id(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    return report
