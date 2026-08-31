"""Local-time clock for anything written to the database.

SQLite's `datetime('now')` — the default on every *_at column in schema.sql — is UTC.
In Asia/Kuala_Lumpur that prints the 23:59 job as 15:59, which makes "did last night
run?" unreadable at a glance. Every timestamp the app writes goes through here instead.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.timezone)


def now() -> datetime:
    return datetime.now(TZ)


def now_iso(timespec: str = "seconds") -> str:
    return now().isoformat(timespec=timespec)
