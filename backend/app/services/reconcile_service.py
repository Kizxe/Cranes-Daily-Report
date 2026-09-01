"""Rebuild `status_events` for a day from ThingsBoard's own history.

A downtime event is **the sensor going quiet**: the span between its last reading and
its next one, once that span passes `downtime_gap_minutes`. That is what ThingsBoard's
Downtime Events widget shows and what its fault counter counts. It is NOT the same as
how long the trigger's STATIC/STALLED flag stayed up afterwards — verified on
`Numed RHT Bell's Court Level 1- B.1.30`, 2026-09-01: TB's window 9:50:49 -> 10:02:48
is the silence, while the STALLED flag ran 10:02:48 -> 10:09:06, i.e. it *starts* where
TB's window ends. Counting flag windows gave 116 events that matched nothing; counting
gaps gives 99 against the triggers' own 1D total of 102.

Devices with no `tb_device_id` of their own (`RTD CH1`, `RTD CH2`, `UFM` — channels the
trigger reports on behalf of) have no telemetry to find gaps in, so they keep the older
walk over the trigger's status key.

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

import asyncio
import logging
from datetime import datetime, timedelta

from ..config import settings
from ..db.database import get_conn, read_conn
from . import ops
from .downtime_service import TZ, normalize_status
from .thingsboard_client import ThingsBoardError, tb_client

log = logging.getLogger("cranes.reconcile")

# How many devices to read from ThingsBoard at once.
_READ_CHUNK = 8


def plan(date: str, site: str | None = None, refresh: bool = False) -> list[dict]:
    """Devices to rebuild on `date`, grouped by the TB device that reports their status.

    Without `refresh`, a device that already has a status_events row starting that day
    is skipped — that day was covered by the live poll and is not a gap.
    """
    sql = """
        SELECT d.id AS device_id, d.name AS device_name,
               k.key_name AS status_key,
               d.tb_device_id AS own_device_id,
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


# What to watch for a heartbeat, best first. Any key the device writes on its own
# schedule works — "Seq #" is a counter every RHT bumps each reading, so it is the most
# reliable evidence that the sensor was alive at that moment.
HEARTBEAT_PREFERENCE = ("Seq #", "Temp (C)", "Corrected Temp (C)", "Humidity (%RH)",
                        "Flow Rate", "Power (W)", "Total Energy (kWh)")


async def heartbeat_key(device_id: int, tb_device_id: str) -> str | None:
    """Which of the device's OWN keys proves it was reporting. Resolved once, then kept.

    A `role='heartbeat'` row is the config; the site YAML carries one for sites
    discovered from now on. When it is missing we ask ThingsBoard for the device's keys,
    pick one, and store it — so an existing site starts working without re-running
    discovery, and later passes cost no extra call.
    """
    with read_conn() as conn:
        row = conn.execute(
            "SELECT key_name FROM device_keys WHERE device_id = ? AND role = 'heartbeat'",
            (device_id,),
        ).fetchone()
    if row:
        return row["key_name"]

    try:
        keys = await tb_client.timeseries_keys(tb_device_id)
    except ThingsBoardError as e:
        log.warning("no key list for %s: %s", tb_device_id, str(e)[:200])
        return None
    if not keys:
        return None

    pick = next((k for k in HEARTBEAT_PREFERENCE if k in keys), keys[0])
    with get_conn() as conn:
        # The key may already be configured on the device under another role (a site
        # YAML that lists the sensor's own metrics). Claim it for heartbeat unless the
        # role is one something else resolves by — otherwise the pick is never stored
        # and every pass pays a timeseries_keys call for this device again.
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role) VALUES (?, ?, 'heartbeat') "
            "ON CONFLICT(device_id, key_name) DO UPDATE SET role = 'heartbeat' "
            "WHERE device_keys.role NOT IN "
            "('status', 'active_ts', 'inactive_ts', 'total_use', 'daily_issues')",
            (device_id, pick),
        )
    return pick


def gaps_from_points(points: list[dict], day_start: datetime, day_end: datetime,
                     now: datetime, gap_seconds: int) -> list[tuple[str, str, str | None]]:
    """The day as alternating ACTIVE / NO DATA segments, from when the sensor reported.

    A silence of `gap_seconds` or longer is a downtime window running from the last
    reading before it to the first reading after — the same span TB's widget prints,
    minus its ~5 min detection lag, because the device stopped at its last reading and
    not when the rule chain noticed.

    The ACTIVE segments between gaps are emitted too, so the day stays fully covered and
    `day_summary`'s events-based hours and active_pct still mean something.
    """
    stamps = sorted(datetime.fromtimestamp(p["ts"] / 1000, TZ)
                    for p in points if p.get("value") is not None)
    cap = min(now, day_end)
    if not stamps or cap <= day_start:
        return []

    before = [t for t in stamps if t < day_start]
    inside = [t for t in stamps if day_start <= t <= cap]
    # The last reading before midnight is what makes a gap straddling 00:00 visible.
    seq = ([before[-1]] if before else []) + inside
    if not seq:
        # Every reading is AFTER this day (the lookahead fetches past day_end): the
        # device had not started reporting yet. Skip it — inventing a 24h outage for a
        # pre-deployment day would be a lie, and the IndexError this used to raise took
        # the whole reconcile pass down with it.
        return []

    segments: list[tuple[str, datetime, datetime | None]] = []

    def add(status: str, start: datetime, end: datetime | None) -> None:
        if end is None or end > start:
            segments.append((status, start, end))

    cursor = day_start
    # Nothing at all before the day and nothing for a while after midnight: the sensor
    # was quiet through that stretch too, even though there is no earlier reading to
    # measure from.
    if not before and inside and (inside[0] - day_start).total_seconds() >= gap_seconds:
        add("NO DATA", day_start, inside[0])
        cursor = inside[0]

    for a, b in zip(seq, seq[1:]):
        if (b - a).total_seconds() < gap_seconds:
            continue
        gap_start, gap_end = max(a, day_start), min(b, cap)
        if gap_end <= cursor:
            continue
        if gap_start > cursor:
            add("ACTIVE", cursor, gap_start)
        add("NO DATA", max(gap_start, cursor), gap_end)
        cursor = gap_end

    # The tail: either the sensor is still reporting, or it has been quiet since its
    # last reading. Only today leaves a window open — a past day is closed at its end.
    open_end = None if cap == now else day_end
    last = max(seq[-1], day_start)
    if (cap - last).total_seconds() >= gap_seconds:
        if last > cursor:
            add("ACTIVE", cursor, last)
        add("NO DATA", max(last, cursor), open_end)
    else:
        add("ACTIVE", cursor, open_end)

    return [(st, s.isoformat(), e.isoformat() if e else None) for st, s, e in segments]


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
        if prev and (prev["end_ts"] is None
                     or datetime.fromisoformat(prev["end_ts"])
                     > datetime.fromisoformat(first_start)):
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

    gap_seconds = settings.downtime_gap_minutes * 60
    sources = plan(date, site, refresh)
    # A device with a TB device of its own has telemetry to find gaps in. One without
    # (RTD CH1/CH2, UFM) is only ever described by the trigger, so it keeps the flag
    # walk — that is the one case events_from_points is still used for.
    by_gap = [d for src in sources for d in src["devices"] if d["own_device_id"]]
    by_flag = [{"tb_device_id": src["tb_device_id"],
                "devices": [d for d in src["devices"] if not d["own_device_id"]]}
               for src in sources]

    written = skipped = devices = 0
    preview: list[dict] = []
    results: list[tuple[dict, list]] = []

    async def read_gaps(d: dict) -> tuple[dict, list]:
        key = await heartbeat_key(d["device_id"], d["own_device_id"])
        if not key:
            return d, []
        data = await _history(d["own_device_id"], [key], start_ms, end_ms)
        points = data.get(key) or []
        if not points:
            return d, []
        return d, gaps_from_points(points, day_start, day_end, now, gap_seconds)

    # One history call per device now, not one per trigger — issued in chunks so a site
    # of 33 sensors is a handful of round trips rather than 33.
    for i in range(0, len(by_gap), _READ_CHUNK):
        results += await asyncio.gather(*(read_gaps(d) for d in by_gap[i:i + _READ_CHUNK]))

    for src in by_flag:
        if not src["devices"]:
            continue
        keys = sorted({d["status_key"] for d in src["devices"]})
        data = await _history(src["tb_device_id"], keys, start_ms, end_ms)
        for d in src["devices"]:
            points = sorted(data.get(d["status_key"]) or [], key=lambda p: p["ts"])
            results.append((d, events_from_points(points, day_start, day_end, now)
                            if points else []))

    for d, events in results:
        if not events:
            # Nothing came back — keep whatever we already hold for this device rather
            # than blanking out a day of downtime over a failed read.
            skipped += 1
            continue
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


__all__ = ["events_from_points", "gaps_from_points", "heartbeat_key", "plan",
           "reconcile_day", "reconcile_today"]
