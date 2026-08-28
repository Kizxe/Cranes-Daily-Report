from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from ..config import settings
from ..db.database import read_conn
from ..services import downtime_service

router = APIRouter(prefix="/downtime", tags=["downtime"])
TZ = ZoneInfo(settings.timezone)


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


@router.post("/poll")
async def poll_now() -> dict:
    """Trigger a status poll immediately (normally on a timer)."""
    return await downtime_service.poll_all_statuses(trigger="manual")


@router.get("/devices/{device_id}")
def device_downtime(device_id: int, date: str = Query(default_factory=_today)) -> dict:
    with read_conn() as conn:
        dev = conn.execute("SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not dev:
        raise HTTPException(404, "device not found")
    return {"device_id": device_id, "device": dev["name"], "date": date,
            **downtime_service.day_summary(device_id, date)}


@router.get("/groups/{group_id}")
def group_downtime(group_id: int, date: str = Query(default_factory=_today)) -> dict:
    with read_conn() as conn:
        devices = conn.execute(
            "SELECT id, name, device_type FROM devices WHERE group_id = ? "
            "ORDER BY sort_order, name",
            (group_id,),
        ).fetchall()
    # One map for the whole group — the site-detail page needs status to decide which
    # recommendations are auto-filled, and it must agree with the report.
    status_of = downtime_service.current_status_map(date)
    out = []
    for d in devices:
        status = status_of.get(d["id"], "UNKNOWN")
        summ = downtime_service.day_summary(d["id"], date)
        summ.pop("events", None)
        out.append({
            "device_id": d["id"], "device": d["name"],
            "device_type": d["device_type"],
            "status": status,
            "severity": downtime_service.severity(status),
            "is_active": status in downtime_service.ACTIVE_STATES,
            **summ,
        })
    return {"group_id": group_id, "date": date, "devices": out}


@router.get("/events/{device_id}")
def raw_events(device_id: int, limit: int = 200) -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM status_events WHERE device_id = ? ORDER BY start_ts DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()]
