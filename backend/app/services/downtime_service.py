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
            # The trigger's activeTs_/InactiveTs_ can point at a moment BEFORE the open
            # event began — routinely so right after a reconcile rewrote the day. Taking
            # it verbatim would write an event that overlaps the previous row and
            # double-counts those seconds; the change is dated no earlier than the
            # window it is closing.
            if datetime.fromisoformat(now) < start:
                now = current["start_ts"]
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
    samples = []
    for d in devices:
        points = data.get(d["status_key"]) or []
        if not points:
            continue
        status = normalize_status(str(points[0].get("value")))
        samples.append((now, d["device_id"], status, "poll"))
        if record_status(d["device_id"], status, source="poll",
                         ts=_transition_ts(d, status, data, now)):
            changes += 1
    if samples:
        with get_conn() as conn:
            conn.executemany(
                "INSERT INTO status_samples (sample_ts, device_id, status, source) "
                "VALUES (?, ?, ?, ?)",
                samples,
            )
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


def _debounce(events: list[dict], min_seconds: int) -> list[dict]:
    """Absorb sub-threshold windows into the window they interrupted.

    A sensor that reads STATIC for 38 seconds and comes back is not a downtime event;
    before the reconcile pass we simply never saw those, because the 5-minute poll
    sampled straight past them. Now that we read every transition ThingsBoard recorded,
    they have to be collapsed or one day prints hundreds of rows.

    Merging (rather than dropping) keeps the day continuous: the blip's seconds go to
    the window before it, and the two halves of that window become one row.
    """
    if min_seconds <= 0:
        return events
    def absorb(prev: dict, e: dict) -> None:
        prev["end"] = e["end"]
        prev["seconds_in_day"] += e["seconds_in_day"]
        prev["carried_out"] = e["carried_out"]
        prev["ongoing"] = e["ongoing"]
        prev["raw_end"] = e["raw_end"]

    out: list[dict] = []
    folded = False           # the window just dropped left a hole to close
    for e in events:
        prev = out[-1] if out else None
        # Only a window that picks up exactly where the last one ended can be merged
        # into it. Two INACTIVE windows hours apart are two outages, not one.
        contiguous = bool(prev) and prev["end"] == e["start"]
        if contiguous and e["seconds_in_day"] < min_seconds:
            absorb(prev, e)          # the blip itself
            folded = True
            continue
        if contiguous and folded and e["status"] == prev["status"]:
            absorb(prev, e)          # the far half of the window it interrupted
            folded = False
            continue
        if e["seconds_in_day"] < min_seconds and not e["ongoing"]:
            folded = True            # too short to report, nothing to fold it into
            continue
        # Same status, back to back, with nothing dropped between them: two separate
        # events. Two silences either side of a single reading are two faults, which is
        # how ThingsBoard counts them too.
        out.append(dict(e))
        folded = False
    return out


def downtime_for_date(device_id: int, date: str) -> list[dict]:
    """Status events overlapping a calendar day (local tz), clipped to the day.

    Windows shorter than `min_event_seconds` are debounced away — see `_debounce`.
    """
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
    # An event that is still open has not run to the end of the day yet — on today it
    # ends "now". Counting it to 23:59:59 would credit hours that haven't happened.
    open_end = min(day_end, max(datetime.now(TZ), day_start))

    out = []
    for r in rows:
        raw_start = datetime.fromisoformat(r["start_ts"])
        raw_end = datetime.fromisoformat(r["end_ts"]) if r["end_ts"] else None
        s = max(raw_start, day_start)
        e = max(min(raw_end or open_end, day_end), s)
        out.append(
            {
                "status": r["status"],
                "start": s.isoformat(),
                "end": e.isoformat(),
                "seconds_in_day": int((e - s).total_seconds()),
                "is_active": r["status"] in ACTIVE_STATES,
                # A window can start before the day and/or run past it. Callers show
                # the clipped times — these flags say the window continues beyond the
                # edge, so a drill-down never prints another day's date under the
                # date picker.
                "carried_in": raw_start < day_start,
                "carried_out": raw_end is None or raw_end > day_end,
                "ongoing": raw_end is None,
                "raw_start": raw_start.isoformat(),
                "raw_end": raw_end.isoformat() if raw_end else None,
            }
        )
    return _debounce(out, settings.min_event_seconds)


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


def report_denominator_seconds(date: str, trigger: str = "scheduled") -> int:
    """Return a full-day or latest-manual-capture denominator for active percent."""
    if trigger != "manual":
        return DAY_SECONDS
    with read_conn() as conn:
        row = conn.execute(
            "SELECT capture_ts, trigger FROM snapshots WHERE capture_date = ? "
            "ORDER BY capture_ts DESC LIMIT 1",
            (date,),
        ).fetchone()
    if not row or row["trigger"] != "manual":
        return DAY_SECONDS
    start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=TZ)
    captured = datetime.fromisoformat(row["capture_ts"])
    end = min(max(captured, start), start.replace(hour=23, minute=59, second=59))
    return max(int((end - start).total_seconds()), 1)


def _incidents(events: list[dict]) -> list[dict]:
    """Fold a day's events into outages: each run of non-ACTIVE windows is one incident.

    This is the unit both ISSUE OCC. and the report's STATUS SUMMARY count, so the
    number printed for a device always equals the rows listed under it. A run only
    ends at an ACTIVE window — a STATIC flag that flows straight into NO DATA is one
    outage, not two.
    """
    out: list[dict] = []
    current: dict | None = None
    for e in events:
        if e["is_active"]:
            current = None
            continue
        if current is None:
            current = {
                "statuses": [e["status"]],
                "start": e["start"], "raw_start": e.get("raw_start", e["start"]),
                "carried_in": e.get("carried_in", False), "seconds_in_day": 0,
            }
            out.append(current)
        elif e["status"] not in current["statuses"]:
            current["statuses"].append(e["status"])
        current["seconds_in_day"] += e["seconds_in_day"]
        current.update(end=e["end"], raw_end=e.get("raw_end", e["end"]),
                       carried_out=e.get("carried_out", False),
                       ongoing=e.get("ongoing", False))
    return out


def status_interval_rows(group_id: int, date: str, limit: int | None = None) -> list[dict]:
    """One row per outage on `date`, for every device in the group.

    Built from the same debounced, day-clipped events as ISSUE OCC. and the hours, so
    the report's STATUS SUMMARY, the site-detail list and the device table agree. An
    outage carried in from an earlier day keeps its original start (`raw_start`) but
    only counts the hours that fall on `date`.
    """
    with read_conn() as conn:
        devices = conn.execute(
            "SELECT id, name FROM devices WHERE group_id = ? ORDER BY sort_order, name",
            (group_id,),
        ).fetchall()
    out: list[dict] = []
    for d in devices:
        for inc in _incidents(downtime_for_date(d["id"], date)):
            status = inc["statuses"][-1]
            out.append({
                "device_id": d["id"],
                "device": d["name"],
                "status": " / ".join(inc["statuses"]),
                "severity": severity(status),
                "start": inc["start"],
                "raw_start": inc["raw_start"],
                "carried_in": inc["carried_in"],
                # None while it's still down at the end of the day (or now, on today).
                "recovered_at": None if inc["carried_out"] else inc["end"],
                "ongoing": inc["ongoing"],
                "hours_today": round(inc["seconds_in_day"] / 3600, 2),
            })
            if limit is not None and len(out) >= limit:
                return out
    return out


def status_summary_rows(group_id: int, date: str) -> list[dict]:
    """The report's STATUS SUMMARY: one row per device that went down on `date`.

    One row per outage was tried first (2026-10-12) and ran to 110 pages on a full day —
    KPJAP sensors flap 70-80 times a day. So each device gets one line: when it first
    went down, whether it came back, how many outages (= its ISSUE OCC.) and the hours
    down today. Worst first. The per-outage list stays in the site-detail drill-down.
    """
    by_device: dict[int, list[dict]] = {}
    for r in status_interval_rows(group_id, date):
        by_device.setdefault(r["device_id"], []).append(r)
    out = []
    for incidents in by_device.values():
        first, last = incidents[0], incidents[-1]
        out.append({
            "device_id": first["device_id"],
            "device": first["device"],
            "status": last["status"],
            "severity": last["severity"],
            "first_start": first["start"],
            "first_raw_start": first["raw_start"],
            "carried_in": first["carried_in"],
            "recovered_at": last["recovered_at"],
            "ongoing": last["ongoing"],
            "outages": len(incidents),
            "hours_today": round(sum(i["hours_today"] for i in incidents), 2),
        })
    return sorted(out, key=lambda r: (-r["hours_today"], -r["outages"], r["device"]))


def status_change_rows(group_id: int, date: str, per_device: int | None = None) -> list[dict]:
    """The report's STATUS SUMMARY: every non-ACTIVE window, grouped under its device.

    One row per status change — INACTIVE, then STALLED, then STATIC each get a line —
    with the device name printed on its first row only. Devices worst first (hours down
    today). A full day holds ~2,500 windows (KPJAP sensors flip ~45 times each), so the
    PDF passes `per_device`: the longest N windows are kept, shown in time order, and a
    `kind: "more"` row says how many were left out. The website passes None for all.
    """
    with read_conn() as conn:
        devices = conn.execute(
            "SELECT id, name FROM devices WHERE group_id = ? ORDER BY sort_order, name",
            (group_id,),
        ).fetchall()
    blocks = []
    for d in devices:
        down = [e for e in downtime_for_date(d["id"], date) if not e["is_active"]]
        if down:
            blocks.append((d, down, sum(e["seconds_in_day"] for e in down)))
    blocks.sort(key=lambda b: (-b[2], -len(b[1]), b[0]["name"]))

    out: list[dict] = []
    for d, down, _ in blocks:
        shown = down
        if per_device is not None and len(down) > per_device:
            keep = sorted(down, key=lambda e: -e["seconds_in_day"])[:per_device]
            shown = [e for e in down if any(e is k for k in keep)]
        for i, e in enumerate(shown):
            out.append({
                "kind": "change",
                "device_id": d["id"],
                "device": d["name"],
                "show_device": i == 0,
                "status": e["status"],
                "severity": severity(e["status"]),
                "start": e["start"],
                "raw_start": e.get("raw_start", e["start"]),
                "carried_in": e.get("carried_in", False),
                # None while it's still down at the end of the day (or now, on today).
                "end": None if e.get("carried_out") else e["end"],
                "ongoing": e.get("ongoing", False),
                "hours": round(e["seconds_in_day"] / 3600, 2),
            })
        hidden = [e for e in down if not any(e is s for s in shown)]
        if hidden:
            out.append({
                "kind": "more", "device_id": d["id"], "device": d["name"],
                "show_device": False, "hidden": len(hidden),
                "hidden_hours": round(sum(e["seconds_in_day"] for e in hidden) / 3600, 2),
            })
    return out


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

    This is THE source for the report's ACTIVE HRS / AFFECTED HRS whenever a baseline
    exists, including when the day's difference is zero. A handful of NUMed sensors
    (UFM, both RTD channels, DPM CH1/CH2) sit frozen at [0, 0] and so print 0.0/0.0
    even while reporting INACTIVE — that is what the ThingsBoard dashboard shows for
    them too, and it points at their rule chain, not at this code. status_events is
    used only when there is no baseline to difference at all.

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


def day_summary(device_id: int, date: str, denominator_seconds: int | None = None) -> dict:
    events = downtime_for_date(device_id, date)
    covered = sum(e["seconds_in_day"] for e in events)
    active = sum(e["seconds_in_day"] for e in events if e["is_active"])
    affected = sum(e["seconds_in_day"] for e in events if not e["is_active"])
    downtime_events = [e for e in events if not e["is_active"]]
    # One per outage — exactly the rows status_interval_rows lists for this device.
    occurrences = len(_incidents(events))

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

    # ISSUE OCC. counts the day's downtime windows — STATIC, STALLED, NO DATA and
    # INACTIVE alike — exactly the non-active rows the drill-down and the report's
    # summary show, so the number always equals the list under it. This is safe to do
    # now that events are telemetry gaps: back when they were flag transitions, one
    # sensor held 33 windows on a day the trigger counted 2 faults, and the column
    # briefly fell back to the trigger's "<sensor> 1D" key. That key counts by the rule
    # chain's own definition and only updates at capture time, so it sat at 2 while the
    # drill-down plainly showed 4 windows — the number on screen must match the list.

    # Scheduled reports use the full day; an on-demand report uses the elapsed capture
    # window. Dividing by `covered` would let a device with only 4h of events read 100%.
    denominator = denominator_seconds or DAY_SECONDS
    window_hours = denominator / 3600
    if hours["source"] == "events":
        # Only recorded downtime counts as affected; time no event covers is treated
        # as active, the same assumption as a device with no downtime at all. Taking
        # active from ACTIVE events instead turned every uncovered stretch into
        # downtime: BTMC OT3 went quiet at 07:38 on 2026-10-13, but with no ACTIVE
        # event before it (the host was asleep, so nothing was polled) the dashboard
        # printed 11.0 affected hours for a 3.4-hour outage.
        raw_active_hours = window_hours - min(affected / 3600, window_hours)
    elif not downtime_events:
        # No non-ACTIVE observation means the device worked for the whole window.
        raw_active_hours = window_hours
    else:
        raw_active_hours = min(max(float(hours["active_hours"]), 0.0), window_hours)
    active_hours = round(raw_active_hours, 1)
    hours["active_hours"] = active_hours
    hours["affected_hours"] = round(max(window_hours - active_hours, 0.0), 1)
    hours["observation_hours"] = round(window_hours, 1)
    active_pct = round(100 * raw_active_hours / window_hours, 1)
    return {
        **hours,
        "covered_hours": round(covered / 3600, 1),
        "active_pct": min(active_pct, 100.0),
        "issue_occurrences": occurrences,
        "downtime_events": len(downtime_events),
        "downtime_hours": round(
            sum(e["seconds_in_day"] for e in downtime_events) / 3600, 2
        ),
        "longest_downtime_hours": round(
            max((e["seconds_in_day"] for e in downtime_events), default=0) / 3600, 2
        ),
        "events": events,
    }
