"""
database/users.py
=================
SQLite-backed storage for BioVision users, reports, and payment links.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()


def _resolve_db_path() -> str:
    raw_value = os.getenv("DATABASE_URL") or os.getenv("SQLITE_DB_PATH") or "biovision.db"

    if raw_value.startswith("sqlite:///"):
        return raw_value.replace("sqlite:///", "", 1)

    if "://" in raw_value and not raw_value.startswith("sqlite:///"):
        fallback = os.getenv("SQLITE_DB_PATH", "biovision.db")
        print(
            "Warning: Non-SQLite DATABASE_URL detected. "
            f"Falling back to local SQLite database at {fallback}."
        )
        return fallback

    return raw_value


DB_PATH = _resolve_db_path()


async def init_db() -> None:
    """Create tables if they do not exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                report_count INTEGER DEFAULT 0,
                is_paid BOOLEAN DEFAULT 0,
                paid_until TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_active TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                report_text TEXT,
                explanation TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                payment_link_id TEXT UNIQUE NOT NULL,
                reference_id TEXT,
                short_url TEXT,
                status TEXT DEFAULT 'created',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT
            )
            """
        )
        await db.commit()
    print(f"Database initialized at {DB_PATH}")


async def get_or_create_user(phone: str, name: str = "") -> dict:
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT * FROM users WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()

        if row:
            updates = ["last_active = ?"]
            values = [now]
            if name and name.strip() and row["name"] != name.strip():
                updates.append("name = ?")
                values.append(name.strip())
            values.append(phone)
            await db.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE phone = ?",
                tuple(values),
            )
            await db.commit()

            async with db.execute("SELECT * FROM users WHERE phone = ?", (phone,)) as cursor:
                updated_row = await cursor.fetchone()
                return dict(updated_row)

        await db.execute(
            """
            INSERT INTO users (phone, name, created_at, last_active)
            VALUES (?, ?, ?, ?)
            """,
            (phone, name.strip(), now, now),
        )
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE phone = ?", (phone,)) as cursor:
            new_row = await cursor.fetchone()
            return dict(new_row)


async def get_user_report_count(phone: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT report_count FROM users WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def increment_report_count(phone: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET report_count = report_count + 1, last_active = ? WHERE phone = ?",
            (datetime.utcnow().isoformat(), phone),
        )
        await db.commit()


async def get_user_payment_status(phone: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_paid, paid_until FROM users WHERE phone = ?",
            (phone,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return False

    is_paid, paid_until = row
    if not is_paid:
        return False

    if not paid_until:
        return True

    try:
        return datetime.fromisoformat(str(paid_until)) > datetime.utcnow()
    except ValueError:
        return False


async def get_user_subscription_details(phone: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT is_paid, paid_until, report_count, name FROM users WHERE phone = ?",
            (phone,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}


async def mark_user_as_paid(phone: str, months: int = 1) -> Optional[str]:
    now = datetime.utcnow()
    current_details = await get_user_subscription_details(phone)

    current_paid_until_raw = current_details.get("paid_until")
    current_paid_until = None
    if current_paid_until_raw:
        try:
            current_paid_until = datetime.fromisoformat(str(current_paid_until_raw))
        except ValueError:
            current_paid_until = None

    start_from = current_paid_until if current_paid_until and current_paid_until > now else now
    new_paid_until = start_from + timedelta(days=30 * months)
    paid_until_iso = new_paid_until.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET is_paid = 1, paid_until = ?, last_active = ?
            WHERE phone = ?
            """,
            (paid_until_iso, now.isoformat(), phone),
        )
        await db.commit()

    print(f"User {phone} marked as paid until {paid_until_iso}")
    return paid_until_iso


async def save_report(phone: str, report_text: str, explanation: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO reports (phone, report_text, explanation, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (phone, report_text, explanation, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_user_reports(phone: str, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM reports
            WHERE phone = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (phone, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def save_payment_link(
    phone: str,
    payment_link_id: str,
    reference_id: str,
    short_url: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO payment_links (phone, payment_link_id, reference_id, short_url, status)
            VALUES (?, ?, ?, ?, 'created')
            ON CONFLICT(payment_link_id) DO UPDATE SET
                reference_id = excluded.reference_id,
                short_url = excluded.short_url
            """,
            (phone, payment_link_id, reference_id, short_url),
        )
        await db.commit()


async def get_payment_link(payment_link_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payment_links WHERE payment_link_id = ?",
            (payment_link_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else {}


async def mark_payment_link_paid(payment_link_id: str) -> bool:
    now = datetime.utcnow().isoformat()
    existing = await get_payment_link(payment_link_id)
    if not existing:
        return False

    if existing.get("status") == "paid":
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE payment_links
            SET status = 'paid', paid_at = ?
            WHERE payment_link_id = ?
            """,
            (now, payment_link_id),
        )
        await db.commit()
    return True
