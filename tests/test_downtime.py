from backend.app.services import config_sync, downtime_service
from backend.app.db.database import get_conn


def _make_device():
    with get_conn() as conn:
        conn.execute("INSERT INTO device_groups (name) VALUES ('G')")
        gid = conn.execute("SELECT id FROM device_groups WHERE name='G'").fetchone()["id"]
        conn.execute("INSERT INTO devices (group_id, name) VALUES (?, 'D1')", (gid,))
        did = conn.execute("SELECT id FROM devices WHERE name='D1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO device_keys (device_id, key_name, role) VALUES (?, 'status', 'status')",
            (did,),
        )
    return did


def test_record_status_only_writes_on_change():
    did = _make_device()
    assert downtime_service.record_status(did, "ACTIVE") is True
    assert downtime_service.record_status(did, "ACTIVE") is False   # no change
    assert downtime_service.record_status(did, "INACTIVE") is True  # change

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, end_ts FROM status_events WHERE device_id=? ORDER BY start_ts", (did,)
        ).fetchall()
    assert [r["status"] for r in rows] == ["ACTIVE", "INACTIVE"]
    assert rows[0]["end_ts"] is not None   # first event closed when second opened
    assert rows[1]["end_ts"] is None       # latest event still open


def test_day_summary_counts_affected():
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, duration_seconds, source)"
            " VALUES (?, 'ACTIVE', ?, ?, ?, 'seed')",
            (did, f"{day}T00:00:00+08:00", f"{day}T20:00:00+08:00", 72000),
        )
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, duration_seconds, source)"
            " VALUES (?, 'INACTIVE', ?, ?, ?, 'seed')",
            (did, f"{day}T20:00:00+08:00", f"{day}T23:59:59+08:00", 14399),
        )
    s = downtime_service.day_summary(did, day)
    assert s["active_hours"] == 20.0
    assert s["issue_occurrences"] == 1
    assert 3.5 < s["affected_hours"] < 4.1


def test_config_sync_noop_on_empty_yaml():
    counts = config_sync.sync_from_yaml()
    assert counts == {"groups": 0, "devices": 0, "keys": 0}
