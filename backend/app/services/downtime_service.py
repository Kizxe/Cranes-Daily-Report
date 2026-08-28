"""Downtime detection.

Poll each device's status key every `downtime_poll_minutes`. Write a
`status_events` row only when the status differs from the currently-open event
for that device. Closing an event fills end_ts + duration_seconds.

Status comes from the sensor's own reported status telemetry key (role: status),
uniformly across all 22 groups — never derived from last-seen timing.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings
from ..db.database import get_conn, read_conn
from .thingsboard_client import ThingsBoardError, tb_client

TZ = ZoneInfo(settings.timezone)

# Which reported statuses count as "up". Everything else is downtime-ish and
# shows in the affected-hours math. Tune to match the ThingsBoard rule chain.
ACTIVE_STATES = {"ACTIVE", "ONLINE", "OK", "UP"}


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _status_devices() -> list[dict]:
    with read_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """
                SELECT d.id AS device_id, d.tb_device_id, k.key_name AS status_key
                FROM devices d
                JOIN device_keys k ON k.device_id = d.id AND k.role = 'status'
                WHERE d.tb_device_id IS NOT NULL
                """
            ).fetchall()
        ]


def _open_event(conn, device_id: int):
    return conn.execute(
        "SELECT id, status, start_ts FROM status_events "
        "WHERE device_id = ? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1",
        (device_id,),
    ).fetchone()


def record_status(device_id: int, status: str, source: str = "poll", ts: str | None = None) -> bool:
    """Insert a new event if status changed. Returns True if an event was written."""
    status = (status or "UNKNOWN").upper()
    now = ts or _now_iso()
    with get_conn() as conn:
        current = _open_event(conn, device_id)
        if current and current["status"] == status:
            return False
        if current:
            start = datetime.fromisoformat(current["start_ts"])
            dur = int((datetime.fromisoformat(now) - start).total_seconds())
            conn.execute(
                "UPDATE status_events SET end_ts = ?, duration_seconds = ? WHERE id = ?",
                (now, max(dur, 0), current["id"]),
            )
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, source) VALUES (?, ?, ?, ?)",
            (device_id, status, now, source),
        )
    return True


async def _poll_one(dev: dict) -> bool:
    try:
        data = await tb_client.latest_timeseries(dev["tb_device_id"], [dev["status_key"]])
    except ThingsBoardError:
        return False
    points = data.get(dev["status_key"]) or []
    if not points:
        return False
    value = str(points[0].get("value"))
    return record_status(dev["device_id"], value, source="poll")


async def poll_all_statuses(trigger: str = "scheduled") -> dict:
    from . import ops

    devices = _status_devices()
    if not devices:
        ops.record("poll", "success", trigger, "0 devices with a tb_device_id + status key")
        return {"polled": 0, "changes": 0}
    try:
        results = await asyncio.gather(*(_poll_one(d) for d in devices))
    except Exception as e:  # noqa: BLE001
        ops.record("poll", "failed", trigger, str(e))
        raise
    changes = sum(1 for r in results if r)
    ops.record("poll", "success", trigger, f"{len(devices)} polled, {changes} change(s)")
    return {"polled": len(devices), "changes": changes}


def downtime_for_date(device_id: int, date: str) -> list[dict]:
    """Status events overlapping a calendar day (local tz), clipped to the day."""
    day_start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=TZ)
    day_end = datetime.fromisoformat(f"{date}T23:59:59").replace(tzinfo=TZ)
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT status, start_ts, end_ts, duration_seconds
            FROM status_events
            WHERE device_id = ?
              AND start_ts <= ?
              AND (end_ts IS NULL OR end_ts >= ?)
            ORDER BY start_ts
            """,
            (device_id, day_end.isoformat(), day_start.isoformat()),
        ).fetchall()
    out = []
    for r in rows:
        s = max(datetime.fromisoformat(r["start_ts"]), day_start)
        e = min(datetime.fromisoformat(r["end_ts"]) if r["end_ts"] else day_end, day_end)
        out.append(
            {
                "status": r["status"],
                "start": s.isoformat(),
                "end": e.isoformat(),
                "seconds_in_day": int((e - s).total_seconds()),
                "is_active": r["status"] in ACTIVE_STATES,
            }
        )
    return out


def day_summary(device_id: int, date: str) -> dict:
    events = downtime_for_date(device_id, date)
    total = sum(e["seconds_in_day"] for e in events) or 1
    active = sum(e["seconds_in_day"] for e in events if e["is_active"])
    affected = sum(e["seconds_in_day"] for e in events if not e["is_active"])
    occurrences = sum(1 for e in events if not e["is_active"])
    return {
        "active_hours": round(active / 3600, 1),
        "affected_hours": round(affected / 3600, 1),
        "active_pct": round(100 * active / total, 1),
        "issue_occurrences": occurrences,
        "events": events,
    }
