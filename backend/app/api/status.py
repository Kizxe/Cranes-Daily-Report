from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..config import settings
from ..db.database import read_conn
from ..services import ops, scheduler

router = APIRouter(prefix="/status", tags=["status"])
TZ = ZoneInfo(settings.timezone)


@router.get("/last-run")
def last_run() -> dict:
    """Feeds the dashboard banner: did last night's capture + report actually work?"""
    with read_conn() as conn:
        last_snap = conn.execute("SELECT MAX(capture_ts) t FROM snapshots").fetchone()["t"]
    return {
        "now": datetime.now(TZ).isoformat(),
        "last_capture": ops.last_run("capture"),
        "last_report": ops.last_run("report"),
        "last_poll": ops.last_run("poll"),
        "last_nightly": ops.last_run("nightly"),
        "latest_snapshot_ts": last_snap,
        "next_runs": scheduler.next_runs(),
    }


@router.get("/runs")
def runs(limit: int = 40) -> list[dict]:
    return ops.recent(limit)
