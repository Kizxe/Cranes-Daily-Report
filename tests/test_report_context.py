"""Two status-resolution defects the report rewrite fixed, guarded so they stay fixed."""
from backend.app.db.database import get_conn
from backend.app.services import downtime_service as dt

DATE = "2026-08-16"


def _group_with_devices(status_key: str = "status") -> dict:
    with get_conn() as conn:
        gid = conn.execute(
            "INSERT INTO device_groups (name, kind) VALUES ('G1', 'device')"
        ).lastrowid
        ids = {}
        for name in ("D1", "D2"):
            did = conn.execute(
                "INSERT INTO devices (group_id, name, device_type) VALUES (?, ?, 'RHT')",
                (gid, name),
            ).lastrowid
            conn.execute(
                "INSERT INTO device_keys (device_id, key_name, role) VALUES (?, ?, 'status')",
                (did, status_key),
            )
            ids[name] = did
    return {"group_id": gid, **ids}


def _snapshot(device_id: int, key: str, value: str, ts: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO snapshots (capture_ts, capture_date, trigger, device_id, key_name, value)"
            " VALUES (?, ?, 'manual', ?, ?, ?)",
            (ts, DATE, device_id, key, value),
        )


def test_status_resolved_from_device_keys_not_a_hardcoded_name():
    """A group whose status key isn't literally called "status" must still resolve."""
    g = _group_with_devices(status_key="deviceStatus")
    _snapshot(g["D1"], "deviceStatus", "ACTIVE", f"{DATE}T23:59:00+08:00")

    assert dt.current_status_map(DATE)[g["D1"]] == "ACTIVE"


def test_partial_later_capture_does_not_blank_other_devices():
    """MAX(capture_ts) must be per-device, not one global max for the date.

    D1 and D2 are captured together; then D2 alone is re-captured a few seconds later.
    D1 must keep its status rather than falling back to UNKNOWN.
    """
    g = _group_with_devices()
    _snapshot(g["D1"], "status", "ACTIVE", f"{DATE}T23:59:00+08:00")
    _snapshot(g["D2"], "status", "ACTIVE", f"{DATE}T23:59:00+08:00")
    _snapshot(g["D2"], "status", "INACTIVE", f"{DATE}T23:59:30+08:00")

    got = dt.current_status_map(DATE)
    assert got[g["D1"]] == "ACTIVE"
    assert got[g["D2"]] == "INACTIVE"


def test_devices_with_no_snapshot_read_unknown():
    g = _group_with_devices()
    assert dt.current_status_map(DATE)[g["D1"]] == "UNKNOWN"


def test_severity_mapping():
    assert dt.severity("ACTIVE") == "ok"
    assert dt.severity("STATIC") == "warn"       # a warning, not attention
    assert dt.severity("STALLED") == "warn"
    assert dt.severity("INACTIVE") == "bad"
    assert dt.severity("") == "bad"              # UNKNOWN


def test_active_pct_is_a_share_of_the_day_not_of_covered_time():
    """A device with only 6h of recorded events is 25% active, not 100%."""
    g = _group_with_devices()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, 'ACTIVE', ?, ?, 'manual')",
            (g["D1"], f"{DATE}T00:00:00+08:00", f"{DATE}T06:00:00+08:00"),
        )
    summ = dt.day_summary(g["D1"], DATE)
    assert summ["active_hours"] == 6.0
    assert summ["covered_hours"] == 6.0
    assert 24.9 < summ["active_pct"] < 25.1
