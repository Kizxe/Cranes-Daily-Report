from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from ..config import settings
from ..db.database import read_conn
from ..services import downtime_service, reconcile_service

router = APIRouter(prefix="/downtime", tags=["downtime"])
TZ = ZoneInfo(settings.timezone)


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


@router.post("/poll")
async def poll_now() -> dict:
    """Trigger a status poll immediately (normally on a timer)."""
    return await downtime_service.poll_all_statuses(trigger="manual")


@router.post("/reconcile")
async def reconcile(date: str = Query(default_factory=_today, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                    site: str | None = None, refresh: bool = True,
                    dry_run: bool = False) -> dict:
    """Rebuild a day's status_events from ThingsBoard's key history.

    The poll loop only samples every few minutes, so a device that dropped and
    recovered in between never reached status_events. This reads every transition TB
    recorded for the day. `refresh=false` fills gaps only, leaving days the poll
    already covered untouched. The scheduler runs this hourly against today.
    """
    return await reconcile_service.reconcile_day(
        date, site=site, refresh=refresh, dry_run=dry_run,
        lookback_days=settings.reconcile_lookback_days, trigger="manual")


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
    denominator_seconds = downtime_service.report_denominator_seconds(date, "manual")
    out = []
    for d in devices:
        status = status_of.get(d["id"], "UNKNOWN")
        summ = downtime_service.day_summary(d["id"], date, denominator_seconds)
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


@router.get("/groups/{group_id}/samples")
def group_status_samples(group_id: int, date: str = Query(default_factory=_today),
                         limit: int = Query(200, ge=1, le=2000)) -> dict:
    """Non-ACTIVE intervals observed by each scheduled poll."""
    return {"group_id": group_id, "date": date,
            "rows": downtime_service.status_interval_rows(group_id, date, limit)}


@router.get("/events/{device_id}")
def raw_events(device_id: int, limit: int = 200,
               date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")) -> list[dict]:
    """Status events for a device.

    With `date`, the windows are the ones the report and `day_summary` use: only those
    overlapping that local day, and **clipped to it** — a window that started yesterday
    starts at 00:00:00 here, with `carried_in` set so the caller can say where it really
    began. Without clipping the drill-down printed yesterday's timestamps under today's
    date picker, and a duration covering both days.
    """
    if date:
        events = downtime_service.downtime_for_date(device_id, date)
        events.reverse()                       # newest first, like the unfiltered list
        return [
            {
                "device_id": device_id,
                "status": e["status"],
                "start_ts": e["start"],
                "end_ts": e["end"],
                "duration_seconds": e["seconds_in_day"],
                "carried_in": e["carried_in"],
                "carried_out": e["carried_out"],
                "ongoing": e["ongoing"],
                "actual_start_ts": e["raw_start"],
                "actual_end_ts": e["raw_end"],
            }
            for e in events[:limit]
        ]

    with read_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM status_events WHERE device_id = ? ORDER BY start_ts DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        ]
