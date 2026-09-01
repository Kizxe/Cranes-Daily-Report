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


def test_short_blips_are_debounced_into_the_window_they_interrupted():
    """The reconcile pass sees every transition; 38-second STATIC blips are not events.

    NUMed on 2026-09-01: 491 non-active windows, median 38s. Absorbed into the active
    window around them, so ISSUE OCC. and the DOWNTIME EVENTS table describe real
    outages and still agree with each other.
    """
    did = _make_device()
    day = "2026-08-16"
    windows = [
        ("ACTIVE",   "00:00:00", "09:00:00"),
        ("STATIC",   "09:00:00", "09:00:38"),   # blip — absorbed
        ("ACTIVE",   "09:00:38", "14:00:00"),
        ("INACTIVE", "14:00:00", "15:30:00"),   # a real outage — kept
        ("ACTIVE",   "15:30:00", "23:59:59"),
    ]
    with get_conn() as conn:
        for status, start, end in windows:
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, ?, ?, ?, 'test')",
                (did, status, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))

    evs = downtime_service.downtime_for_date(did, day)
    assert [e["status"] for e in evs] == ["ACTIVE", "INACTIVE", "ACTIVE"]
    assert evs[0]["start"][11:19] == "00:00:00" and evs[0]["end"][11:19] == "14:00:00"
    assert downtime_service.day_summary(did, day)["issue_occurrences"] == 1


def test_two_silences_either_side_of_one_reading_stay_two_events():
    """Back-to-back gap windows are two faults — that is how ThingsBoard counts them.

    Bell's Court L1 on 2026-09-01: quiet 04:12:27-04:28:56, one reading, quiet again
    04:28:56-04:49:07. Debouncing used to glue those into a single 37-minute outage.
    """
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        for start, end in (("04:12:27", "04:28:56"), ("04:28:56", "04:49:07")):
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, 'NO DATA', ?, ?, 'test')",
                (did, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))

    evs = downtime_service.downtime_for_date(did, day)
    assert [(e["start"][11:19], e["end"][11:19]) for e in evs] == [
        ("04:12:27", "04:28:56"), ("04:28:56", "04:49:07")]


def test_two_outages_hours_apart_stay_two_events():
    """Same status, but not contiguous — debouncing must not glue them together."""
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        for start, end in (("02:00:00", "03:00:00"), ("20:00:00", "21:00:00")):
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, 'INACTIVE', ?, ?, 'test')",
                (did, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))
    assert downtime_service.day_summary(did, day)["issue_occurrences"] == 2


def test_issue_occ_counts_every_non_active_window_on_screen():
    """Anatomy Room, 2026-09-01: three NO DATA gaps plus a STATIC flag window. The
    drill-down showed 4 while ISSUE OCC. said 2 (a stale trigger 1D snapshot) — the
    number must be the count of the windows listed under it."""
    did = _make_device()
    day = "2026-08-16"
    windows = [("NO DATA", "04:27:32", "04:41:34"), ("NO DATA", "07:27:44", "07:38:36"),
               ("NO DATA", "10:23:48", "11:04:04"), ("STATIC", "14:08:01", "14:12:07")]
    with get_conn() as conn:
        # A stale 1D key claiming fewer faults must not override what is on screen.
        conn.execute("INSERT INTO device_keys (device_id, key_name, role)"
                     " VALUES (?, 'D 1D', 'daily_issues')", (did,))
        conn.execute("INSERT INTO snapshots (capture_ts, capture_date, trigger,"
                     " device_id, key_name, value) VALUES (?, ?, 'manual', ?, 'D 1D', '2')",
                     (f"{day}T11:29:00+08:00", day, did))
        for status, start, end in windows:
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, ?, ?, ?, 'test')",
                (did, status, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))
    assert downtime_service.day_summary(did, day)["issue_occurrences"] == 4


def test_a_backdated_transition_never_overlaps_the_open_event():
    """activeTs_/InactiveTs_ can point before the open window began — right after a
    reconcile rewrote the day, it usually does. Taken verbatim it wrote an event that
    overlapped the previous row and double-counted those seconds."""
    did = _make_device()
    downtime_service.record_status(did, "ACTIVE", source="test",
                                   ts="2026-08-16T10:00:00+08:00")
    downtime_service.record_status(did, "STATIC", source="test",
                                   ts="2026-08-16T09:00:00+08:00")   # earlier than open
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, start_ts, end_ts FROM status_events WHERE device_id = ?"
            " ORDER BY id", (did,)).fetchall()
    assert rows[0]["end_ts"] == rows[1]["start_ts"], "windows must stay contiguous"
    assert rows[1]["start_ts"] >= rows[0]["start_ts"], "no event may predate the one it closes"


def test_snapshot_plan_leaves_heartbeat_keys_alone():
    """Heartbeat keys are for the gap reconcile; snapshotting them costs one extra TB
    call per sensor per capture for a value no report number reads."""
    from backend.app.services import snapshot_service as snap
    did = _make_device()
    with get_conn() as conn:
        conn.execute("UPDATE devices SET tb_device_id = 'sensor-1' WHERE id = ?", (did,))
        conn.execute("INSERT INTO device_keys (device_id, key_name, role)"
                     " VALUES (?, 'Seq #', 'heartbeat')", (did,))
    sources, _ = snap._fetch_plan()
    keys = [k for src in sources for _, k in src["keys"]]
    assert "Seq #" not in keys


def test_config_sync_noop_on_empty_yaml():
    counts = config_sync.sync_from_yaml()
    assert counts == {"groups": 0, "devices": 0, "keys": 0}


# --- the 23:59 job must close out the right day ------------------------------
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from backend.app.services.scheduler import report_date_for  # noqa: E402

_TZ = ZoneInfo("Asia/Kuala_Lumpur")


def _at(text: str) -> str:
    return report_date_for(datetime.fromisoformat(text).replace(tzinfo=_TZ))


def test_nightly_run_closes_out_the_day_it_was_scheduled_for():
    """A late run must still report the day it belongs to, not the day it woke up on."""
    assert _at("2026-08-31T23:59:00") == "2026-08-31"     # on time
    assert _at("2026-08-31T23:59:30") == "2026-08-31"
    assert _at("2026-09-01T00:03:21") == "2026-08-31"     # the real misfire we hit
    assert _at("2026-09-01T00:58:00") == "2026-08-31"     # still inside the grace hour
    assert _at("2026-08-31T23:58:59") == "2026-08-30"     # a hair early belongs to the day before
