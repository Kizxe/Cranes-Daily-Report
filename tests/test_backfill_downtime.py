"""Rebuilding a day's downtime from ThingsBoard history (reconcile_service).

A downtime event is the sensor going quiet — the gap between two of its own readings —
which is what TB's Downtime Events widget shows. Devices with no TB device of their own
fall back to walking the trigger's status flag. `scripts/backfill_downtime.py` is a thin
CLI over the same code.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.app.db.database import get_conn, read_conn
from backend.app.services import downtime_service as dt, reconcile_service as rec
from tests.factories import device, make_site

TZ = ZoneInfo("Asia/Kuala_Lumpur")
TRIGGER_ID = "trigger-1111"


def _pt(iso: str, value):
    return {"ts": int(datetime.fromisoformat(iso).timestamp() * 1000), "value": value}


DAY_START = datetime.fromisoformat("2026-08-30T00:00:00+08:00")
DAY_END = datetime.fromisoformat("2026-08-30T23:59:59+08:00")
NOW = datetime.fromisoformat("2026-09-01T12:00:00+08:00")


# --- the transition walk -----------------------------------------------------

def test_one_transition_in_day_is_closed_by_the_next():
    points = [
        _pt("2026-08-29T22:00:00+08:00", "ACTIVE"),      # standing before the day
        _pt("2026-08-30T09:00:00+08:00", "INACTIVE"),
        _pt("2026-08-30T11:30:00+08:00", "ACTIVE"),
    ]
    evs = rec.events_from_points(points, DAY_START, DAY_END, NOW)
    # only segments that BEGIN during the day
    assert [e[0] for e in evs] == ["INACTIVE", "ACTIVE"]
    assert evs[0][1].startswith("2026-08-30T09:00:00")
    assert evs[0][2].startswith("2026-08-30T11:30:00")   # closed by the next point
    assert evs[1][2] is None                             # last segment still open at NOW


def test_repeated_same_value_is_not_a_transition():
    points = [
        _pt("2026-08-30T08:00:00+08:00", "ACTIVE"),
        _pt("2026-08-30T09:00:00+08:00", "ACTIVE"),
        _pt("2026-08-30T10:00:00+08:00", "true"),        # normalises to ACTIVE
        _pt("2026-08-30T11:00:00+08:00", "INACTIVE"),
    ]
    evs = rec.events_from_points(points, DAY_START, DAY_END, NOW)
    assert [e[0] for e in evs] == ["ACTIVE", "INACTIVE"]
    assert evs[0][1].startswith("2026-08-30T08:00:00")


def test_segment_starting_before_midnight_is_excluded():
    points = [
        _pt("2026-08-29T20:00:00+08:00", "INACTIVE"),
        _pt("2026-08-30T06:00:00+08:00", "ACTIVE"),
    ]
    evs = rec.events_from_points(points, DAY_START, DAY_END, NOW)
    assert [e[0] for e in evs] == ["ACTIVE"]


def test_no_points_yields_nothing():
    assert rec.events_from_points([], DAY_START, DAY_END, NOW) == []


# --- writing the day back ----------------------------------------------------

DATE = "2026-08-30"
KEY = "deviceStatus_D1"


@pytest.fixture
def dev():
    """One device whose status key lives on a trigger device, as the real config does."""
    ids = make_site("S", [device("D1")], date=DATE)
    with get_conn() as conn:
        conn.execute("UPDATE device_keys SET key_name = ?, tb_source_device_id = ?"
                     " WHERE device_id = ? AND role = 'status'", (KEY, TRIGGER_ID, ids["D1"]))
        conn.execute("DELETE FROM status_events WHERE device_id = ?", (ids["D1"],))
    return ids["D1"]


def _events(device_id: int) -> list[tuple]:
    with read_conn() as conn:
        return [(r["status"], r["start_ts"][11:19], (r["end_ts"] or "")[11:19], r["source"])
                for r in conn.execute(
                    "SELECT * FROM status_events WHERE device_id = ? ORDER BY start_ts",
                    (device_id,))]


def _history_returning(points: list[dict]):
    async def fake_history(device_id, keys, start_ms, end_ms):
        assert device_id == TRIGGER_ID, "history read from the wrong TB device"
        return {KEY: points}
    return fake_history


async def test_refresh_replaces_flaps_the_poll_never_sampled(dev, monkeypatch):
    """The whole point: two short drops between polls that status_events never held."""
    with get_conn() as conn:                       # what a 5-min poll had recorded
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, source)"
            " VALUES (?, 'ACTIVE', ?, 'poll')", (dev, f"{DATE}T00:00:00+08:00"))

    monkeypatch.setattr(rec.tb_client, "timeseries_history", _history_returning([
        _pt(f"{DATE}T00:00:00+08:00", "ACTIVE"),
        _pt(f"{DATE}T09:00:00+08:00", "INACTIVE"),
        _pt(f"{DATE}T09:02:00+08:00", "ACTIVE"),   # both invisible to a 5-min poll
        _pt(f"{DATE}T14:00:00+08:00", "INACTIVE"),
        _pt(f"{DATE}T14:03:00+08:00", "ACTIVE"),
    ]))
    summary = await rec.reconcile_day(DATE, refresh=True)

    assert summary["events"] == 5 and summary["devices"] == 1
    assert _events(dev) == [
        ("ACTIVE", "00:00:00", "09:00:00", "reconcile"),
        ("INACTIVE", "09:00:00", "09:02:00", "reconcile"),
        ("ACTIVE", "09:02:00", "14:00:00", "reconcile"),
        ("INACTIVE", "14:00:00", "14:03:00", "reconcile"),
        ("ACTIVE", "14:03:00", "", "reconcile"),
    ]
    # Two of them are downtime, and the drill-down now lists windows the poll never saw.
    summary = dt.day_summary(dev, DATE)
    assert sum(1 for e in summary["events"] if not e["is_active"]) == 2


async def test_refresh_leaves_other_days_alone_and_closes_the_carried_in_window(
        dev, monkeypatch):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, source)"
            " VALUES (?, 'ACTIVE', '2026-08-29T20:00:00+08:00', 'poll')", (dev,))
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, 'INACTIVE', '2026-08-31T08:00:00+08:00',"
            " '2026-08-31T09:00:00+08:00', 'poll')", (dev,))

    monkeypatch.setattr(rec.tb_client, "timeseries_history", _history_returning([
        _pt("2026-08-29T20:00:00+08:00", "ACTIVE"),
        _pt(f"{DATE}T06:00:00+08:00", "INACTIVE"),
        _pt(f"{DATE}T07:00:00+08:00", "ACTIVE"),
    ]))
    await rec.reconcile_day(DATE, refresh=True)

    with read_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM status_events WHERE device_id = ? ORDER BY start_ts", (dev,))]
    # The 29th's open window now ends where the history says the day's first change was.
    assert rows[0]["start_ts"].startswith("2026-08-29")
    assert rows[0]["end_ts"].startswith(f"{DATE}T06:00:00")
    assert rows[0]["duration_seconds"] == 10 * 3600
    assert [r["start_ts"][:10] for r in rows] == ["2026-08-29", DATE, DATE, "2026-08-31"]


async def test_a_device_with_no_history_keeps_the_events_it_has(dev, monkeypatch):
    """A TB read that comes back empty must never blank out a day of downtime."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, 'INACTIVE', ?, ?, 'poll')",
            (dev, f"{DATE}T09:00:00+08:00", f"{DATE}T10:00:00+08:00"))

    monkeypatch.setattr(rec.tb_client, "timeseries_history", _history_returning([]))
    summary = await rec.reconcile_day(DATE, refresh=True)

    assert (summary["devices"], summary["skipped"]) == (0, 1)
    assert _events(dev) == [("INACTIVE", "09:00:00", "10:00:00", "poll")]


async def test_gap_fill_skips_a_day_the_poll_already_covered(dev, monkeypatch):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, source)"
            " VALUES (?, 'ACTIVE', ?, 'poll')", (dev, f"{DATE}T00:00:00+08:00"))

    called = False

    async def fake_history(*a, **kw):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(rec.tb_client, "timeseries_history", fake_history)
    summary = await rec.reconcile_day(DATE, refresh=False)

    assert summary["events"] == 0 and not called, "gap-fill must not touch a covered day"
    assert _events(dev) == [("ACTIVE", "00:00:00", "", "poll")]


async def test_dry_run_writes_nothing(dev, monkeypatch):
    monkeypatch.setattr(rec.tb_client, "timeseries_history", _history_returning([
        _pt(f"{DATE}T09:00:00+08:00", "INACTIVE"),
        _pt(f"{DATE}T10:00:00+08:00", "ACTIVE"),
    ]))
    summary = await rec.reconcile_day(DATE, refresh=True, dry_run=True)

    assert summary["events"] == 2 and len(summary["preview"]) == 2
    assert _events(dev) == []


# --- the gap walk: a downtime event is the sensor going quiet -----------------

GAP = 600            # settings.downtime_gap_minutes, in seconds
PAST_NOW = datetime.fromisoformat("2026-08-31T12:00:00+08:00")


def _beat(when: datetime) -> dict:
    """One reading. Only its timestamp matters — the value just has to be present."""
    return {"ts": int(when.timestamp() * 1000), "value": "1"}


def _reporting(*spans: tuple[str, str], step: int = 120) -> list[dict]:
    """A sensor reporting every `step` seconds through each span. The holes BETWEEN the
    spans are the silences — that is what the walk has to find."""
    out = []
    for start, end in spans:
        t = datetime.fromisoformat(f"{DATE}T{start}+08:00")
        stop = datetime.fromisoformat(f"{DATE}T{end}+08:00")
        while t <= stop:
            out.append(_beat(t))
            t += timedelta(seconds=step)
    return out


def _walk(points, now=PAST_NOW):
    return [(st, s[11:19], e[11:19] if e else "open")
            for st, s, e in rec.gaps_from_points(points, DAY_START, DAY_END, now, GAP)]


def test_a_long_enough_silence_becomes_one_downtime_window():
    """Bell's Court L1 in miniature: quiet from 09:44:49 to 10:02:48 is the event, and
    the window runs from the last reading to the next — the span TB's widget prints."""
    assert _walk(_reporting(("00:00:00", "09:44:49"), ("10:02:48", "23:59:59"))) == [
        ("ACTIVE", "00:00:00", "09:44:00"),      # its last reading before the silence
        ("NO DATA", "09:44:00", "10:02:48"),     # ... and the first one after it
        ("ACTIVE", "10:02:48", "23:59:59"),
    ]


def test_a_short_silence_is_not_an_event():
    """9 minutes is a sensor being a sensor. The old flag walk called this downtime."""
    assert _walk(_reporting(("00:00:00", "09:00:00"), ("09:09:00", "23:59:59"))) == [
        ("ACTIVE", "00:00:00", "23:59:59"),
    ]


def test_the_day_is_covered_end_to_end():
    """Every second of the day belongs to exactly one window, so the hours add up."""
    evs = rec.gaps_from_points(
        _reporting(("00:00:00", "02:00:00"), ("02:30:00", "08:00:00"),
                   ("09:00:00", "23:59:59")),
        DAY_START, DAY_END, PAST_NOW, GAP)
    assert evs[0][1].startswith(f"{DATE}T00:00:00")
    assert evs[-1][2].startswith(f"{DATE}T23:59:59")
    for (_, _, end), (_, start, _) in zip(evs, evs[1:]):
        assert end == start, "a hole between two windows would lose that time"
    assert [e[0] for e in evs] == ["ACTIVE", "NO DATA", "ACTIVE", "NO DATA", "ACTIVE"]


def test_silence_carried_over_midnight_is_clipped_to_the_day():
    points = ([_beat(datetime.fromisoformat("2026-08-29T23:40:00+08:00"))]
              + _reporting(("00:20:00", "23:59:59")))
    assert _walk(points)[:2] == [
        ("NO DATA", "00:00:00", "00:20:00"),   # clipped to the day, not back to 23:40
        ("ACTIVE", "00:20:00", "23:59:59"),
    ]


def test_a_sensor_silent_all_day_is_one_window():
    points = [_beat(datetime.fromisoformat("2026-08-29T23:40:00+08:00"))]
    assert _walk(points) == [("NO DATA", "00:00:00", "23:59:59")]


def test_a_sensor_quiet_right_now_leaves_the_window_open():
    """Only today: the device has not come back yet, so there is no end to record."""
    now = datetime.fromisoformat(f"{DATE}T14:00:00+08:00")
    assert _walk(_reporting(("00:00:00", "13:00:00")), now=now) == [
        ("ACTIVE", "00:00:00", "13:00:00"),
        ("NO DATA", "13:00:00", "open"),
    ]


def test_a_reporting_sensor_on_a_live_day_has_no_open_downtime():
    now = datetime.fromisoformat(f"{DATE}T14:00:00+08:00")
    assert _walk(_reporting(("00:00:00", "13:59:00")), now=now) == [
        ("ACTIVE", "00:00:00", "open"),
    ]


def test_readings_only_after_the_day_mean_the_device_did_not_exist_yet():
    """Backfilling a day before the sensor was deployed: skip it, don't crash.

    The lookahead fetches history past day_end, so this shape reaches the walk — and
    it used to raise IndexError, which took the whole reconcile pass down through
    asyncio.gather.
    """
    later = [_beat(datetime.fromisoformat("2026-08-31T09:00:00+08:00"))]
    assert rec.gaps_from_points(later, DAY_START, DAY_END, PAST_NOW, GAP) == []


def test_no_readings_at_all_yields_nothing_rather_than_inventing_downtime():
    assert rec.gaps_from_points([], DAY_START, DAY_END, PAST_NOW, GAP) == []


# --- wiring: which walk a device gets ----------------------------------------

def _as_own_sensor(device_id: int, key: str = "Seq #") -> None:
    with get_conn() as conn:
        conn.execute("UPDATE devices SET tb_device_id = 'sensor-1' WHERE id = ?", (device_id,))
        conn.execute("INSERT INTO device_keys (device_id, key_name, role)"
                     " VALUES (?, ?, 'heartbeat')", (device_id, key))


async def test_a_device_with_its_own_tb_id_is_read_for_gaps(dev, monkeypatch):
    """The sensor's own history is what gets read, not the trigger's status key."""
    _as_own_sensor(dev)

    async def fake_history(device_id, keys, start_ms, end_ms):
        assert device_id == "sensor-1", "read the trigger instead of the sensor"
        assert keys == ["Seq #"]
        return {"Seq #": _reporting(("00:00:00", "09:00:00"), ("09:20:00", "23:59:59"))}

    monkeypatch.setattr(rec.tb_client, "timeseries_history", fake_history)
    await rec.reconcile_day(DATE, refresh=True)

    assert _events(dev) == [
        ("ACTIVE", "00:00:00", "09:00:00", "reconcile"),
        ("NO DATA", "09:00:00", "09:20:00", "reconcile"),
        ("ACTIVE", "09:20:00", "23:59:59", "reconcile"),
    ]


async def test_a_device_without_its_own_tb_id_still_walks_the_trigger_flag(dev, monkeypatch):
    """RTD CH1/CH2 and UFM have no telemetry of their own to find gaps in."""
    monkeypatch.setattr(rec.tb_client, "timeseries_history", _history_returning([
        _pt(f"{DATE}T00:00:00+08:00", "ACTIVE"),
        _pt(f"{DATE}T09:00:00+08:00", "INACTIVE"),
        _pt(f"{DATE}T10:00:00+08:00", "ACTIVE"),
    ]))
    await rec.reconcile_day(DATE, refresh=True)

    assert [e[0] for e in _events(dev)] == ["ACTIVE", "INACTIVE", "ACTIVE"]


async def test_the_heartbeat_key_is_resolved_once_and_kept(dev, monkeypatch):
    calls = []

    async def fake_keys(device_id):
        calls.append(device_id)
        return ["RSSI (dBm)", "Temp (C)", "Seq #"]

    monkeypatch.setattr(rec.tb_client, "timeseries_keys", fake_keys)
    assert await rec.heartbeat_key(dev, "sensor-1") == "Seq #"    # preference order wins
    assert await rec.heartbeat_key(dev, "sensor-1") == "Seq #"
    assert calls == ["sensor-1"], "the second call must come from the stored key"


async def test_hours_from_gap_events_add_up_to_the_day(dev, monkeypatch):
    _as_own_sensor(dev)
    with get_conn() as conn:
        # Drop the counter pair so day_summary falls back to the events themselves.
        conn.execute("DELETE FROM snapshots WHERE device_id = ?", (dev,))

    async def fake_history(device_id, keys, start_ms, end_ms):
        return {"Seq #": _reporting(("00:00:00", "06:00:00"), ("08:00:00", "23:59:59"))}

    monkeypatch.setattr(rec.tb_client, "timeseries_history", fake_history)
    await rec.reconcile_day(DATE, refresh=True)

    summ = dt.day_summary(dev, DATE)
    assert summ["source"] == "events"
    assert summ["affected_hours"] == 2.0
    assert summ["active_hours"] == 22.0
    assert round(summ["covered_hours"]) == 24
