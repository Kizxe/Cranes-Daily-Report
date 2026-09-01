"""Status keys that live on a site's trigger device, not on the sensor itself.

Covers the three places that had to learn about it: config_sync storing
tb_source_device_id, the snapshot fetch plan grouping by source device, and the
poll loop batching per source + dating events from activeTs_/InactiveTs_.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml

from backend.app.db.database import get_conn, read_conn
from backend.app.services import config_sync, downtime_service as dt, snapshot_service as snap

TRIGGER_ID = "trigger-uuid-0001"

SITE = {
    "status_key_default": "status",
    "groups": [
        {
            "name": "NUMed",
            "kind": "device",
            "trigger": {"device_name": "NumedTrigger", "tb_device_id": TRIGGER_ID},
            "devices": [
                {
                    "name": "Numed RHT Wet Lab",
                    "device_type": "RHT",
                    "tb_device_id": None,
                    "keys": [
                        {"key_name": "deviceStatus_Numed RHT Wet Lab", "role": "status",
                         "source": "trigger"},
                        {"key_name": "activeTs_Numed RHT Wet Lab", "role": "active_ts",
                         "source": "trigger"},
                        {"key_name": "InactiveTs_Numed RHT Wet Lab", "role": "inactive_ts",
                         "source": "trigger"},
                    ],
                },
                {
                    # No deviceStatus_ key: status falls back to the active_ boolean.
                    "name": "Numed_DPM_4",
                    "device_type": "DPM",
                    "tb_device_id": None,
                    "keys": [
                        {"key_name": "active_Numed_DPM_4", "role": "status",
                         "source": "trigger"},
                        {"key_name": "InactiveTs_Numed_DPM_4", "role": "inactive_ts",
                         "source": "trigger"},
                    ],
                },
            ],
        }
    ],
}


@pytest.fixture
def site(tmp_path, monkeypatch):
    """The site as its own sites/<name>.yaml, which is how config ships."""
    sites = tmp_path / "sites"
    sites.mkdir()
    (sites / "numed.yaml").write_text(yaml.safe_dump(SITE["groups"][0]))
    monkeypatch.setattr(config_sync.settings, "sites_dir", sites)
    config_sync.sync_from_yaml()
    with read_conn() as conn:
        return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM devices")}


def test_trigger_keys_record_their_source_device(site):
    with read_conn() as conn:
        rows = conn.execute(
            "SELECT key_name, tb_source_device_id FROM device_keys"
        ).fetchall()
    assert rows, "config_sync stored no keys"
    # Every key came from the trigger, and none was attributed to the sensor itself.
    assert {r["tb_source_device_id"] for r in rows} == {TRIGGER_ID}


def test_fetch_plan_collapses_the_site_into_one_read(site):
    sources, device_count = snap._fetch_plan()
    assert device_count == 2
    assert len(sources) == 1, "keys on one trigger device must batch into one call"
    assert sources[0]["tb_device_id"] == TRIGGER_ID
    assert ("deviceStatus_Numed RHT Wet Lab" in [k for _, k in sources[0]["keys"]])


def test_active_boolean_normalises_to_the_status_vocabulary():
    assert dt.normalize_status("true") == "ACTIVE"
    assert dt.normalize_status("false") == "INACTIVE"
    assert dt.normalize_status("ACTIVE") == "ACTIVE"
    assert dt.normalize_status(None) == "UNKNOWN"
    # The trigger's own grading and ours must agree on what escalates a site.
    assert dt.severity("NO DATA") == "bad"
    assert dt.severity("STATIC") == "warn"



async def test_poll_dates_the_event_from_the_trigger_timestamp(site, monkeypatch):
    """An event starts when the trigger says it did, not when the poll happened."""
    went_down = datetime.now(dt.TZ).replace(microsecond=0) - timedelta(hours=3)
    ms = int(went_down.timestamp() * 1000)

    async def fake_latest(device_id, keys):
        assert device_id == TRIGGER_ID, "polled the wrong TB device"
        assert len(keys) == 5, "poll did not batch the site's keys into one call"
        return {
            "deviceStatus_Numed RHT Wet Lab": [{"ts": 1, "value": "ACTIVE"}],
            "activeTs_Numed RHT Wet Lab": [{"ts": 1, "value": str(ms)}],
            "active_Numed_DPM_4": [{"ts": 1, "value": "false"}],
            "InactiveTs_Numed_DPM_4": [{"ts": 1, "value": str(ms)}],
        }

    monkeypatch.setattr(dt.tb_client, "latest_timeseries", fake_latest)
    result = await dt.poll_all_statuses(trigger="manual")
    assert result == {"polled": 2, "changes": 2}

    with read_conn() as conn:
        events = {
            r["device_id"]: r
            for r in conn.execute("SELECT device_id, status, start_ts FROM status_events")
        }
    dpm = events[site["Numed_DPM_4"]]
    assert dpm["status"] == "INACTIVE", "active_=false must store as INACTIVE"
    assert dpm["start_ts"] == went_down.isoformat(), "event not dated from InactiveTs_"
    assert events[site["Numed RHT Wet Lab"]]["status"] == "ACTIVE"



async def test_future_transition_timestamp_falls_back_to_poll_time(site, monkeypatch):
    """The trigger sometimes carries a ts ahead of now; never open an event in the future."""
    ahead = int((datetime.now(dt.TZ) + timedelta(days=2)).timestamp() * 1000)

    async def fake_latest(device_id, keys):
        return {
            "active_Numed_DPM_4": [{"ts": 1, "value": "false"}],
            "InactiveTs_Numed_DPM_4": [{"ts": 1, "value": str(ahead)}],
        }

    monkeypatch.setattr(dt.tb_client, "latest_timeseries", fake_latest)
    await dt.poll_all_statuses(trigger="manual")
    with read_conn() as conn:
        row = conn.execute(
            "SELECT start_ts FROM status_events WHERE device_id = ?",
            (site["Numed_DPM_4"],),
        ).fetchone()
    assert datetime.fromisoformat(row["start_ts"]) <= datetime.now(dt.TZ)


def test_each_site_loads_from_its_own_file(tmp_path, monkeypatch):
    """One file per site, ordered by sort_order then file name; no cross-talk."""
    sites = tmp_path / "sites"
    sites.mkdir()
    (sites / "numed.yaml").write_text(yaml.safe_dump(SITE["groups"][0]))
    (sites / "computime.yaml").write_text(yaml.safe_dump({
        "name": "Computime",
        "sort_order": 0,
        "trigger": {"device_name": "ComputimeTrigger", "tb_device_id": "trigger-uuid-0002"},
        "devices": [{"name": "CT_RHT_01", "device_type": "RHT", "keys": [
            {"key_name": "deviceStatus_CT_RHT_01", "role": "status", "source": "trigger"}]}],
    }))
    (sites / "_draft.yaml").write_text(yaml.safe_dump({"name": "NotReady", "devices": []}))
    monkeypatch.setattr(config_sync.settings, "sites_dir", sites)

    groups = config_sync.load_yaml()["groups"]
    assert [g["name"] for g in groups] == ["Computime", "NUMed"], "sort_order must win"
    # A leading underscore parks a site file without loading it.
    assert "NotReady" not in [g["name"] for g in groups]

    config_sync.sync_from_yaml()
    with read_conn() as conn:
        rows = conn.execute(
            """SELECT g.name AS site, COUNT(d.id) AS n FROM device_groups g
               LEFT JOIN devices d ON d.group_id = g.id GROUP BY g.id ORDER BY g.sort_order"""
        ).fetchall()
    assert [(r["site"], r["n"]) for r in rows] == [("Computime", 1), ("NUMed", 2)]


def test_written_timestamps_are_local_not_utc(client):
    """SQLite's datetime('now') default is UTC — 8h off here. Nothing may rely on it."""
    from backend.app.services import ops

    ops.record("capture", "success", "manual", "test")
    stamps = [ops.last_run("capture")["ran_at"]]

    with get_conn() as conn:
        gid = conn.execute(
            "INSERT INTO device_groups (name) VALUES ('TZ Site') RETURNING id"
        ).fetchone()["id"]
    r = client.post("/api/remarks", json={"group_id": gid, "report_date": "2026-08-31",
                                         "body": "check the gateway"})
    assert r.status_code == 201
    stamps += [r.json()["created_at"], r.json()["updated_at"]]

    for raw in stamps:
        ts = datetime.fromisoformat(raw)
        assert ts.utcoffset() is not None, f"{raw} lost its timezone"
        assert abs((datetime.now(dt.TZ) - ts).total_seconds()) < 60, f"{raw} is not local time"


def _capture(device_id: int, date: str, key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (capture_ts, capture_date, trigger, device_id, key_name, value)"
            " VALUES (?, ?, 'scheduled', ?, ?, ?)",
            (f"{date}T23:59:00+08:00", date, device_id, key, value),
        )


def test_day_hours_come_from_the_trigger_counters(site):
    """ACTIVE/AFFECTED HRS = one day's slice of ThingsBoard's own running counters."""
    did = site["Numed RHT Wet Lab"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role, tb_source_device_id)"
            " VALUES (?, 'forTotalUse_Numed RHT Wet Lab', 'total_use', ?)", (did, TRIGGER_ID))
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role, tb_source_device_id)"
            " VALUES (?, 'Numed RHT Wet Lab 1D', 'daily_issues', ?)", (did, TRIGGER_ID))

    # Cumulative totals: 100h inactive / 200h active yesterday, +2h / +22h since.
    h = 3_600_000
    _capture(did, "2026-08-30", "forTotalUse_Numed RHT Wet Lab", f"[{100*h}, {200*h}]")
    _capture(did, "2026-08-31", "forTotalUse_Numed RHT Wet Lab", f"[{102*h}, {222*h}]")
    _capture(did, "2026-08-31", "Numed RHT Wet Lab 1D", "7.0")

    # Two downtime windows on the day, while the trigger's own 1D key claims 7 faults.
    # The windows win: they are what the drill-down and the report print, and the 1D
    # snapshot only updates at capture time — the number must match the list under it.
    for start, end in (("2026-08-31T02:00:00+08:00", "2026-08-31T03:00:00+08:00"),
                       ("2026-08-31T20:00:00+08:00", "2026-08-31T21:00:00+08:00")):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, 'INACTIVE', ?, ?, 'test')", (did, start, end))

    s = dt.day_summary(did, "2026-08-31")
    assert s["source"] == "counter"
    assert s["active_hours"] == 22.0, "active hours must be the day's slice, not the total"
    assert s["affected_hours"] == 2.0
    assert s["issue_occurrences"] == 2, "ISSUE OCC. counts the windows on screen, not 1D"


def test_day_hours_fall_back_when_there_is_no_previous_day(site):
    """First day a site is configured: nothing to difference, so use status_events."""
    did = site["Numed RHT Wet Lab"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role, tb_source_device_id)"
            " VALUES (?, 'forTotalUse_Numed RHT Wet Lab', 'total_use', ?)", (did, TRIGGER_ID))
    _capture(did, "2026-08-31", "forTotalUse_Numed RHT Wet Lab", f"[{102*3_600_000}, 0]")

    assert dt.counter_hours(did, "2026-08-31") is None
    assert dt.day_summary(did, "2026-08-31")["source"] == "events"


def test_counter_reset_does_not_produce_negative_hours(site):
    """The counters reset sometimes; a negative difference must not reach the report."""
    did = site["Numed RHT Wet Lab"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role, tb_source_device_id)"
            " VALUES (?, 'forTotalUse_Numed RHT Wet Lab', 'total_use', ?)", (did, TRIGGER_ID))
    _capture(did, "2026-08-30", "forTotalUse_Numed RHT Wet Lab", "[9999999999, 9999999999]")
    _capture(did, "2026-08-31", "forTotalUse_Numed RHT Wet Lab", "[0, 0]")

    assert dt.counter_hours(did, "2026-08-31") is None
    s = dt.day_summary(did, "2026-08-31")
    assert s["active_hours"] >= 0 and s["affected_hours"] >= 0


def test_stale_baseline_is_rejected_rather_than_printing_impossible_hours(site):
    """A baseline older than one day would make ACTIVE HRS exceed 24 — don't use it."""
    did = site["Numed RHT Wet Lab"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role, tb_source_device_id)"
            " VALUES (?, 'forTotalUse_Numed RHT Wet Lab', 'total_use', ?)", (did, TRIGGER_ID))
    h = 3_600_000
    _capture(did, "2026-08-30", "forTotalUse_Numed RHT Wet Lab", "[0, 0]")
    _capture(did, "2026-08-31", "forTotalUse_Numed RHT Wet Lab", f"[{10*h}, {70*h}]")
    assert dt.counter_hours(did, "2026-08-31") is None, "80h in a day must be rejected"

    # Just over a day is a slightly-early baseline, not a broken one: clamp, don't drop.
    with get_conn() as conn:
        conn.execute("DELETE FROM snapshots WHERE capture_date = '2026-08-31'")
    _capture(did, "2026-08-31", "forTotalUse_Numed RHT Wet Lab", f"[0, {25*h}]")
    assert dt.counter_hours(did, "2026-08-31")["active_hours"] == 24.0


def test_frozen_counters_still_come_from_the_counter(site):
    """A [0,0] counter reports 0.0/0.0 — the counter is the source whenever it exists.

    UFM and both RTD channels are frozen at [0,0] on NumedTrigger while reporting
    INACTIVE / NO DATA. The ThingsBoard dashboard shows 0 for them as well, so the
    report follows the counter rather than substituting its own number.
    """
    did = site["Numed_DPM_4"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role, tb_source_device_id)"
            " VALUES (?, 'forTotalUse_Numed_DPM_4', 'total_use', ?)", (did, TRIGGER_ID))
    _capture(did, "2026-08-30", "forTotalUse_Numed_DPM_4", "[0, 0]")
    _capture(did, "2026-08-31", "forTotalUse_Numed_DPM_4", "[0, 0]")
    dt.record_status(did, "NO DATA", source="test", ts="2026-08-31T00:00:00+08:00")

    s = dt.day_summary(did, "2026-08-31")
    assert s["source"] == "counter"
    assert (s["active_hours"], s["affected_hours"]) == (0.0, 0.0)
