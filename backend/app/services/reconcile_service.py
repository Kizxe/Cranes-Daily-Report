"""Rebuild `status_events` for a day from ThingsBoard's own key history.

Why this exists alongside the poll loop: `poll_all_statuses` samples every
`downtime_poll_minutes`, so a device that drops and recovers between two polls leaves
no trace at all. Measured on NUMed on 2026-09-01 at 11:32 — the poll had recorded 28
downtime windows for the day while the triggers' own `<sensor> 1D` fault counters
summed to 98. The transitions were not lost, they were simply never sampled:
ThingsBoard stores every value the status key ever took, with its timestamp.

So this walks that history instead of sampling it. Two modes:

- **gap-fill** (`refresh=False`) — only devices with no `status_events` row that day.
  What `scripts/backfill_downtime.py` has always done: recover a day the backend was
  down for, never touch a day it covered.
- **refresh** (`refresh=True`) — every device: the day's rows are replaced by what the
  history says. This is the one the scheduler runs against today, so ISSUE OCC. and
  the DOWNTIME EVENTS table converge on ThingsBoard's own account of the day rather
  than on our sampling of it.

Refresh only ever deletes rows for a device whose history actually came back. A failed
or empty ThingsBoard read leaves that device's existing events alone — a network blip
must not blank out a day of downtime.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..db.database import get_conn, read_conn
from . import ops
from .downtime_service import TZ, normalize_status
from .thingsboard_client import ThingsBoardError, tb_client

log = logging.getLogger("cranes.reconcile")


def plan(date: str, site: str | None = None, refresh: bool = False) -> list[dict]:
    """Devices to rebuild on `date`, grouped by the TB device that reports their status.

    Without `refresh`, a device that already has a status_events row starting that day
    is skipped — that day was covered by the live poll and is not a gap.
    """
    sql = """
        SELECT d.id AS device_id, d.name AS device_name,
               k.key_name AS status_key,
               COALESCE(k.tb_source_device_id, d.tb_device_id) AS source_id
        FROM devices d
        JOIN device_keys k ON k.device_id = d.id AND k.role = 'status'
        JOIN device_groups g ON g.id = d.group_id
        WHERE COALESCE(k.tb_source_device_id, d.tb_device_id) IS NOT NULL
    """
    params: list = []
    if not refresh:
        sql += """
          AND NOT EXISTS (
              SELECT 1 FROM status_events e
              WHERE e.device_id = d.id AND substr(e.start_ts, 1, 10) = ?
          )
        """
        params.append(date)
    if site:
        sql += " AND g.name = ? COLLATE NOCASE"
        params.append(site)
    with read_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append(dict(r))
    return [{"tb_device_id": k, "devices": v} for k, v in by_source.items()]


def events_from_points(points: list[dict], day_start: datetime, day_end: datetime,
                       now: datetime) -> list[tuple[str, str, str | None]]:
    """(status, start_ts, end_ts) per segment whose start falls inside the day.

    `points` is the full history ASC from before the day to now, so the transition
    that ends the last in-day segment is visible. A segment still open at the cutoff
    keeps end_ts = None (it is genuinely ongoing).
    """
    segments: list[tuple[str, datetime]] = []
    for p in points:
        val = p.get("value")
        if val is None:
            continue
        status = normalize_status(str(val))
        ts = datetime.fromtimestamp(p["ts"] / 1000, TZ)
        if segments and segments[-1][0] == status:
            continue                       # no change — extend current segment
        segments.append((status, ts))

    out: list[tuple[str, str, str | None]] = []
    for i, (status, start) in enumerate(segments):
        end = segments[i + 1][1] if i + 1 < len(segments) else None
        if start < day_start or start > day_end:
            continue                       # only segments that began during the day
        end_iso = end.isoformat() if end and end <= now else None
        out.append((status, start.isoformat(), end_iso))
    return out


async def _history(tb_device_id: str, keys: list[str], start_ms: int,
                   end_ms: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in range(0, len(keys), tb_client.KEY_BATCH):
        try:
            out.update(await tb_client.timeseries_history(
                tb_device_id, keys[i:i + tb_client.KEY_BATCH], start_ms, end_ms))
        except ThingsBoardError as e:
            log.warning("history read failed for %s: %s", tb_device_id, str(e)[:200])
    return out


def _duration(start_ts: str, end_ts: str | None) -> int | None:
    if not end_ts:
        return None
    delta = datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)
    return max(int(delta.total_seconds()), 0)


def _write_device(device_id: int, date: str, events: list[tuple[str, str, str | None]],
                  source: str) -> None:
    """Replace one device's rows for `date` with `events`, in a single transaction.

    Also re-closes the window that carried in from the previous day: it used to end at
    whatever transition the poll happened to catch, which is not where the history says
    the day's first change actually fell.
    """
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM status_events WHERE device_id = ? AND substr(start_ts, 1, 10) = ?",
            (device_id, date),
        )
        conn.executemany(
            """INSERT INTO status_events
                 (device_id, status, start_ts, end_ts, duration_seconds, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(device_id, st, s, e, _duration(s, e), source) for st, s, e in events],
        )
        if not events:
            return
        first_start = events[0][1]
        prev = conn.execute(
            "SELECT id, start_ts, end_ts FROM status_events "
            "WHERE device_id = ? AND substr(start_ts, 1, 10) < ? ORDER BY start_ts DESC LIMIT 1",
            (device_id, date),
        ).fetchone()
        if prev and (prev["end_ts"] is None or prev["end_ts"] > first_start):
            conn.execute(
                "UPDATE status_events SET end_ts = ?, duration_seconds = ? WHERE id = ?",
                (first_start, _duration(prev["start_ts"], first_start), prev["id"]),
            )


async def reconcile_day(date: str, *, site: str | None = None, refresh: bool = False,
                        lookback_days: int = 7, dry_run: bool = False,
                        trigger: str = "manual") -> dict:
    """Rebuild `date` from ThingsBoard history. Returns a summary dict."""
    day_start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=TZ)
    day_end = datetime.fromisoformat(f"{date}T23:59:59").replace(tzinfo=TZ)
    now = datetime.now(TZ)
    # Look back far enough to see the status standing at 00:00, forward to now so the
    # transition that closed the day's last segment is in the window.
    start_ms = int((day_start - timedelta(days=lookback_days)).timestamp() * 1000)
    end_ms = int(min(now, day_end + timedelta(days=lookback_days)).timestamp() * 1000)

    sources = plan(date, site, refresh)
    written = skipped = devices = 0
    preview: list[dict] = []

    for src in sources:
        keys = sorted({d["status_key"] for d in src["devices"]})
        data = await _history(src["tb_device_id"], keys, start_ms, end_ms)
        for d in src["devices"]:
            points = sorted(data.get(d["status_key"]) or [], key=lambda p: p["ts"])
            if not points:
                # No history came back — keep whatever we already hold for this device.
                skipped += 1
                continue
            events = events_from_points(points, day_start, day_end, now)
            devices += 1
            written += len(events)
            preview += [{"device": d["device_name"], "status": st, "start": s, "end": e}
                        for st, s, e in events]
            if not dry_run:
                _write_device(d["device_id"], date, events,
                              "reconcile" if refresh else "backfill")

    summary = {"date": date, "devices": devices, "events": written,
               "skipped": skipped, "refresh": refresh, "dry_run": dry_run}
    if not dry_run:
        ops.record("reconcile", "success", trigger,
                   f"{date}: {written} event(s) across {devices} device(s)"
                   + (f", {skipped} skipped (no history)" if skipped else ""))
    return summary | ({"preview": preview} if dry_run else {})


async def reconcile_today(trigger: str = "scheduled") -> dict:
    """The scheduled pass: refresh today from history so the day's events stay true."""
    date = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        return await reconcile_day(date, refresh=True, trigger=trigger)
    except Exception as e:  # noqa: BLE001
        ops.record("reconcile", "failed", trigger, f"{date}: {e}")
        raise


__all__ = ["events_from_points", "plan", "reconcile_day", "reconcile_today"]
