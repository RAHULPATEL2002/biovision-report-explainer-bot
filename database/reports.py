"""SQLite database for storing reports."""

from __future__ import annotations

import aiosqlite
from datetime import datetime

DB_PATH = "biovision.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                filename TEXT,
                extracted_text TEXT,
                explanation TEXT,
                language TEXT DEFAULT 'hindi',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def save_report(
    session_id: str,
    filename: str,
    extracted_text: str,
    explanation: str,
    language: str = "hindi",
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO reports (session_id, filename, extracted_text, explanation, language, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, filename, extracted_text, explanation, language, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_reports(session_id: str, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, filename, language, created_at, explanation FROM reports WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_report_by_id(report_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM reports WHERE id = ?",
            (report_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
