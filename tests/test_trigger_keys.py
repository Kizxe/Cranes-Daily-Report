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


def test_forget_seed_removes_only_the_sample_sites(site):
    """--forget must clear the samples and leave configured sites alone."""
    from backend.app.services import seed_service

    seed_service.load_seed()
    with read_conn() as conn:
        before = {r["name"] for r in conn.execute("SELECT name FROM device_groups")}
    assert "NUMed" in before and len(before) > 1

    removed = seed_service.forget_seed()
    with read_conn() as conn:
        after = {r["name"] for r in conn.execute("SELECT name FROM device_groups")}
        orphans = conn.execute(
            "SELECT COUNT(*) n FROM devices WHERE group_id NOT IN "
            "(SELECT id FROM device_groups)"
        ).fetchone()["n"]
    assert after == {"NUMed"}, "a configured site was deleted with the seed"
    assert removed["count"] == len(before) - 1
    assert orphans == 0, "deleting a group left its devices behind"
