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

# Which statuses escalate a site to ATTENTION on the cover page. STATIC and STALLED
# are warnings, not attention — that is what the approved report does (BSC East Wing
# runs 1 Static + 1 Stalled and still prints HEALTHY).
ATTENTION_STATES = {"INACTIVE", "OFFLINE", "DOWN", "UNKNOWN"}

# A clipped day runs 00:00:00 -> 23:59:59, so full coverage is 86399s, not 86400.
DAY_SECONDS = 86399


def severity(status: str) -> str:
    """'ok' | 'warn' | 'bad' — the one place status maps to a colour.

    Feeds the report's status pills and recommendation colour, the cover-page health
    pill, and the site-detail page, so they can never disagree.
    """
    s = (status or "UNKNOWN").upper()
    if s in ACTIVE_STATES:
        return "ok"
    if s in ATTENTION_STATES:
        return "bad"
    return "warn"


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def current_status_map(date: str) -> dict[int, str]:
    """{device_id: "ACTIVE"|...} from the latest snapshot value on `date`.

    Two things this gets right that a naive lookup does not:
    - the status key name is resolved per device from device_keys.role='status'
      (devices are not all required to call it "status"),
    - MAX(capture_ts) is taken per (device, key), not once globally for the date.
      A partial later capture of one device must not blank out every other device.
    """
    with read_conn() as conn:
        status_key = {
            r["device_id"]: r["key_name"]
            for r in conn.execute(
                "SELECT device_id, key_name FROM device_keys WHERE role = 'status'"
            )
        }
        rows = conn.execute(
            """
            SELECT s.device_id, s.key_name, s.value
            FROM snapshots s
            JOIN (
                SELECT device_id, key_name, MAX(capture_ts) AS mx
                FROM snapshots
                WHERE capture_date = ? AND value IS NOT NULL
                GROUP BY device_id, key_name
            ) m ON m.device_id = s.device_id
               AND m.key_name = s.key_name
               AND m.mx = s.capture_ts
            WHERE s.capture_date = ?
            """,
            (date, date),
        ).fetchall()
        ids = [r["id"] for r in conn.execute("SELECT id FROM devices")]

    latest = {(r["device_id"], r["key_name"]): r["value"] for r in rows}
    out: dict[int, str] = {}
    for device_id in ids:
        key = status_key.get(device_id, "status")
        out[device_id] = (latest.get((device_id, key)) or "UNKNOWN").upper()
    return out


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
    covered = sum(e["seconds_in_day"] for e in events)
    active = sum(e["seconds_in_day"] for e in events if e["is_active"])
    affected = sum(e["seconds_in_day"] for e in events if not e["is_active"])
    occurrences = sum(1 for e in events if not e["is_active"])
    # Percentage of the DAY, not of the covered window. Dividing by `covered` would
    # let a device with only 4h of events read 100% active.
    return {
        "active_hours": round(active / 3600, 1),
        "affected_hours": round(affected / 3600, 1),
        "covered_hours": round(covered / 3600, 1),
        "active_pct": round(100 * active / DAY_SECONDS, 1),
        "issue_occurrences": occurrences,
        "events": events,
    }
