"""Rebuilding a day's downtime from ThingsBoard history (reconcile_service).

The poll loop samples; this walks TB's own key history, so it sees the flaps that fell
between two polls. `scripts/backfill_downtime.py` is a thin CLI over the same code.
"""
from __future__ import annotations

from datetime import datetime
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
    # ISSUE OCC. follows, because it counts the day's downtime windows.
    assert dt.day_summary(dev, DATE)["issue_occurrences"] == 2


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
