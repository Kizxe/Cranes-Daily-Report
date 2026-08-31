"""Downtime detection.

Poll each device's status key every `downtime_poll_minutes`. Write a
`status_events` row only when the status differs from the currently-open event
for that device. Closing an event fills end_ts + duration_seconds.

Status is a key the ThingsBoard rule chain computes and writes to the site's TRIGGER
device — `deviceStatus_<sensor>` (ACTIVE / STATIC / NO DATA / ...), or the
`active_<sensor>` boolean for sensors that have no deviceStatus_ key. Never derived
from last-seen timing. `device_keys.tb_source_device_id` says which TB device to read
each key off, so one poll is one batched call per trigger device.

The trigger also reports activeTs_/InactiveTs_ per sensor — the exact moment of the
last transition. When they're configured, an event starts at that moment rather than
at poll time, so a 5–15 min poll interval no longer rounds off downtime windows.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
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
# "NO DATA" is what the trigger reports for a sensor sending nothing at all; the rule
# chain grades it CRITICAL, same as INACTIVE.
ATTENTION_STATES = {"INACTIVE", "OFFLINE", "DOWN", "UNKNOWN", "NO DATA"}

# A clipped day runs 00:00:00 -> 23:59:59, so full coverage is 86399s, not 86400.
DAY_SECONDS = 86399


def normalize_status(value: str | None) -> str:
    """Raw telemetry value -> the status vocabulary status_events stores.

    `deviceStatus_` keys already speak it (ACTIVE / STATIC / NO DATA). Sensors without
    one fall back to the `active_` boolean, which arrives as the string "true"/"false".
    """
    s = (value or "UNKNOWN").strip().upper()
    if s == "TRUE":
        return "ACTIVE"
    if s == "FALSE":
        return "INACTIVE"
    return s


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
        out[device_id] = normalize_status(latest.get((device_id, key)))
    return out


def _status_devices() -> list[dict]:
    """One row per device: its status key, the transition-ts keys, and where to read them.

    tb_source_device_id points at the site's trigger device for trigger-sourced keys and
    is NULL for a device that reports its own status, in which case the device's own
    tb_device_id is used.
    """
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.id AS device_id,
                   COALESCE(k.tb_source_device_id, d.tb_device_id) AS source_id,
                   k.key_name AS status_key,
                   (SELECT key_name FROM device_keys
                     WHERE device_id = d.id AND role = 'active_ts') AS active_ts_key,
                   (SELECT key_name FROM device_keys
                     WHERE device_id = d.id AND role = 'inactive_ts') AS inactive_ts_key
            FROM devices d
            JOIN device_keys k ON k.device_id = d.id AND k.role = 'status'
            WHERE COALESCE(k.tb_source_device_id, d.tb_device_id) IS NOT NULL
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _transition_ts(dev: dict, status: str, data: dict, now: str) -> str:
    """When the change actually happened, per the trigger's activeTs_/InactiveTs_ key.

    Falls back to poll time when the key isn't configured, is missing, or reports a
    moment in the future — the report must never open an event that hasn't happened.
    """
    key = dev["active_ts_key"] if status in ACTIVE_STATES else dev["inactive_ts_key"]
    points = data.get(key) or [] if key else []
    if not points:
        return now
    try:
        ms = int(float(points[0].get("value")))
    except (TypeError, ValueError):
        return now
    if ms <= 0:
        return now
    moment = datetime.fromtimestamp(ms / 1000, TZ)
    return moment.isoformat() if moment < datetime.fromisoformat(now) else now


def _open_event(conn, device_id: int):
    return conn.execute(
        "SELECT id, status, start_ts FROM status_events "
        "WHERE device_id = ? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1",
        (device_id,),
    ).fetchone()


def record_status(device_id: int, status: str, source: str = "poll", ts: str | None = None) -> bool:
    """Insert a new event if status changed. Returns True if an event was written."""
    status = normalize_status(status)
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


async def _poll_source(source_id: str, devices: list[dict]) -> int:
    """Poll every device whose keys live on one TB device. Returns events written."""
    keys = []
    for d in devices:
        keys += [k for k in (d["status_key"], d["active_ts_key"], d["inactive_ts_key"]) if k]
    try:
        data = await tb_client.latest_timeseries(source_id, sorted(set(keys)))
    except ThingsBoardError:
        return 0

    now = _now_iso()
    changes = 0
    for d in devices:
        points = data.get(d["status_key"]) or []
        if not points:
            continue
        status = normalize_status(str(points[0].get("value")))
        if record_status(d["device_id"], status, source="poll",
                         ts=_transition_ts(d, status, data, now)):
            changes += 1
    return changes


async def poll_all_statuses(trigger: str = "scheduled") -> dict:
    from . import ops

    devices = _status_devices()
    if not devices:
        ops.record("poll", "success", trigger, "0 devices with a status key and a TB source")
        return {"polled": 0, "changes": 0}

    by_source: dict[str, list[dict]] = {}
    for d in devices:
        by_source.setdefault(d["source_id"], []).append(d)
    try:
        results = await asyncio.gather(
            *(_poll_source(src, devs) for src, devs in by_source.items())
        )
    except Exception as e:  # noqa: BLE001
        ops.record("poll", "failed", trigger, str(e))
        raise
    changes = sum(results)
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


def _role_value(conn, device_id: int, role: str, date: str) -> str | None:
    """Latest captured value of a device's key in `role`, on `date`."""
    row = conn.execute(
        """
        SELECT s.value FROM snapshots s
        JOIN device_keys k ON k.device_id = s.device_id AND k.key_name = s.key_name
        WHERE s.device_id = ? AND k.role = ? AND s.capture_date = ? AND s.value IS NOT NULL
        ORDER BY s.capture_ts DESC LIMIT 1
        """,
        (device_id, role, date),
    ).fetchone()
    return row["value"] if row else None


def _total_use(conn, device_id: int, date: str) -> tuple[float, float] | None:
    """(inactive_ms, active_ms) from forTotalUse_<sensor> — cumulative, not per-day."""
    raw = _role_value(conn, device_id, "total_use", date)
    if not raw:
        return None
    try:
        pair = json.loads(raw)
        return float(pair[0]), float(pair[1])
    except (ValueError, TypeError, IndexError):
        return None


def counter_hours(device_id: int, date: str) -> dict | None:
    """Active / affected hours for one day, from ThingsBoard's own running counters.

    `forTotalUse_<sensor>` is [inactive_ms, active_ms] accumulated since the counters
    last reset — 4455 hours on one NUMed sensor — so it can't go straight into a daily
    column. Differencing it against the previous day's capture gives exactly the hours
    that elapsed in between, using ThingsBoard's accounting rather than re-deriving it
    from our poll samples.

    Returns None when there's no previous day to difference against (the first day a
    site is configured), or when a counter reset makes the difference negative — the
    caller falls back to the status_events math.
    """
    prev_date = (datetime.fromisoformat(date) - timedelta(days=1)).strftime("%Y-%m-%d")
    with read_conn() as conn:
        cur = _total_use(conn, device_id, date)
        prev = _total_use(conn, device_id, prev_date)
    if not cur or not prev:
        return None
    d_inactive, d_active = cur[0] - prev[0], cur[1] - prev[1]
    if d_inactive < 0 or d_active < 0:
        return None                      # counters were reset between the two captures

    active_h, affected_h = d_active / 3_600_000, d_inactive / 3_600_000
    # The baseline is whatever value the key last carried before the previous day ended.
    # If that key hadn't been written for a while, the difference spans more than one
    # day and the hours are not this day's. Past a day's slack, don't guess — fall back.
    if active_h + affected_h > DAY_SECONDS / 3600 + 2:
        return None
    return {
        # A day holds 24 hours; a slightly-early baseline can still push one side just
        # over, so clamp rather than print an impossible 25.0.
        "active_hours": round(min(active_h, 24.0), 1),
        "affected_hours": round(min(affected_h, 24.0), 1),
        "source": "counter",
    }


def day_summary(device_id: int, date: str) -> dict:
    events = downtime_for_date(device_id, date)
    covered = sum(e["seconds_in_day"] for e in events)
    active = sum(e["seconds_in_day"] for e in events if e["is_active"])
    affected = sum(e["seconds_in_day"] for e in events if not e["is_active"])
    occurrences = sum(1 for e in events if not e["is_active"])

    hours = {
        "active_hours": round(active / 3600, 1),
        "affected_hours": round(affected / 3600, 1),
        "source": "events",
    }
    # ThingsBoard already counts active/inactive milliseconds per sensor. Prefer that
    # over our own poll-derived math: it doesn't depend on how long we've been polling.
    from_counter = counter_hours(device_id, date)
    if from_counter:
        hours = from_counter

    # ISSUE OCC. is the trigger's own daily fault count ("<sensor> 1D"), which resets at
    # midnight and matches the Fault Counter device's per-device breakdown. Our own
    # count of inactive events is the fallback.
    with read_conn() as conn:
        daily = _role_value(conn, device_id, "daily_issues", date)
    try:
        occurrences = int(float(daily)) if daily is not None else occurrences
    except (TypeError, ValueError):
        pass

    # Percentage of the DAY, not of the covered window. Dividing by `covered` would
    # let a device with only 4h of events read 100% active.
    active_pct = round(100 * hours["active_hours"] * 3600 / DAY_SECONDS, 1)
    return {
        **hours,
        "covered_hours": round(covered / 3600, 1),
        "active_pct": min(active_pct, 100.0),
        "issue_occurrences": occurrences,
        "events": events,
    }
