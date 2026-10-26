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
    """Same status with an ACTIVE stretch between — debouncing must not glue them together."""
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        for status, start, end in (("INACTIVE", "02:00:00", "03:00:00"),
                                   ("ACTIVE", "03:00:00", "20:00:00"),
                                   ("INACTIVE", "20:00:00", "21:00:00")):
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, ?, ?, ?, 'test')",
                (did, status, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))
    assert downtime_service.day_summary(did, day)["issue_occurrences"] == 2


def test_non_active_windows_without_recovery_between_are_one_incident():
    """ISSUE OCC. counts ACTIVE -> non-ACTIVE runs: no ACTIVE between, one incident."""
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        for start, end in (("02:00:00", "03:00:00"), ("20:00:00", "21:00:00")):
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, 'INACTIVE', ?, ?, 'test')",
                (did, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))
    summ = downtime_service.day_summary(did, day)
    assert summ["issue_occurrences"] == 1
    assert summ["downtime_events"] == 2, "the windows themselves are still all listed"


def test_issue_occ_counts_every_outage_not_the_stale_1d_key():
    """Anatomy Room, 2026-09-01: three NO DATA gaps plus a STATIC flag window, each
    followed by a recovery. ISSUE OCC. said 2 (a stale trigger 1D snapshot) — it must
    count the outages, not the 1D key."""
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
        for i, (status, start, end) in enumerate(windows):
            recovered = windows[i + 1][1] if i + 1 < len(windows) else "23:00:00"
            for st, s, e in ((status, start, end), ("ACTIVE", end, recovered)):
                conn.execute(
                    "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                    " VALUES (?, ?, ?, ?, 'test')",
                    (did, st, f"{day}T{s}+08:00", f"{day}T{e}+08:00"))
    assert downtime_service.day_summary(did, day)["issue_occurrences"] == 4
    assert len(downtime_service.status_interval_rows(_group_of(did), day)) == 4, \
        "ISSUE OCC. must equal the STATUS SUMMARY rows listed for the device"


def _group_of(did):
    with get_conn() as conn:
        return conn.execute("SELECT group_id FROM devices WHERE id = ?",
                            (did,)).fetchone()["group_id"]


def test_a_carried_in_outage_keeps_its_real_start_but_counts_only_today():
    """GuardHouse printed ELAPSED 7632 hr and an undated '11:20' — weeks of outage
    passed off as today. The row now says when it began and counts today's hours."""
    from backend.app.services import report_service
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, 'INACTIVE', '2026-08-10T17:29:00+08:00', ?, 'test')",
            (did, f"{day}T03:00:00+08:00"))
    [row] = downtime_service.status_interval_rows(_group_of(did), day)
    assert row["carried_in"] and row["raw_start"].startswith("2026-08-10")
    assert row["hours_today"] == 3.0
    [labelled] = report_service._status_history(_group_of(did), day)
    assert labelled["started_label"] == "since 08-10 17:29"
    assert labelled["end_label"] == "03:00"


def test_status_summary_is_one_row_per_device_with_its_outage_count():
    """KPJAP sensors flap 70-80 times a day; a row per outage ran to 110 pages."""
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        for status, start, end in (("INACTIVE", "01:00:00", "01:30:00"),
                                   ("ACTIVE", "01:30:00", "05:00:00"),
                                   ("INACTIVE", "05:00:00", "06:00:00"),
                                   ("ACTIVE", "06:00:00", "20:00:00"),
                                   ("STATIC", "20:00:00", "21:00:00"),
                                   ("ACTIVE", "21:00:00", "23:00:00")):
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, ?, ?, ?, 'test')",
                (did, status, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))
    [row] = downtime_service.status_summary_rows(_group_of(did), day)
    assert row["outages"] == 3 == downtime_service.day_summary(did, day)["issue_occurrences"]
    assert row["first_start"][11:16] == "01:00" and row["recovered_at"][11:16] == "21:00"
    assert row["status"] == "STATIC" and row["hours_today"] == 2.5


def test_time_no_event_covers_is_not_counted_as_downtime():
    """BTMC OT3, 2026-10-13: it went quiet at 07:38, the host slept through the
    morning so no ACTIVE event was written before that, and the dashboard printed
    11.0 affected hours for what was a 3.4-hour outage."""
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, 'INACTIVE', ?, ?, 'poll')",
            (did, f"{day}T07:38:00+08:00", f"{day}T11:02:00+08:00"))
    s = downtime_service.day_summary(did, day, 11 * 3600)   # an 11:00 manual capture
    assert s["source"] == "events"
    assert s["affected_hours"] == 3.4
    assert s["active_hours"] == 7.6


def test_a_sub_threshold_blip_is_not_listed_or_counted():
    did = _make_device()
    day = "2026-08-16"
    with get_conn() as conn:
        for status, start, end in (("ACTIVE", "00:00:00", "10:00:00"),
                                   ("INACTIVE", "10:00:00", "10:01:00"),
                                   ("ACTIVE", "10:01:00", "12:00:00")):
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, ?, ?, ?, 'test')",
                (did, status, f"{day}T{start}+08:00", f"{day}T{end}+08:00"))
    assert downtime_service.status_interval_rows(_group_of(did), day) == []
    assert downtime_service.day_summary(did, day)["issue_occurrences"] == 0


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


def _windows(did, day, spans):
    """Insert back-to-back windows: [(status, 'HH:MM', 'HH:MM'), ...]."""
    with get_conn() as conn:
        for status, a, b in spans:
            conn.execute(
                "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
                " VALUES (?, ?, ?, ?, 'test')",
                (did, status, f"{day}T{a}:00+08:00", f"{day}T{b}:00+08:00"))


def test_status_summary_lists_every_change_with_the_device_named_once():
    """User's layout (2026-10-13): Hot Kitchen Freezer | INACTIVE / INACTIVE / STALLED / STATIC."""
    did = _make_device()
    day = "2026-08-16"
    _windows(did, day, [("INACTIVE", "01:00", "01:30"), ("ACTIVE", "01:30", "02:00"),
                        ("INACTIVE", "02:00", "02:20"), ("STALLED", "02:20", "02:40"),
                        ("STATIC", "02:40", "03:00"), ("ACTIVE", "03:00", "23:59")])
    rows = downtime_service.status_change_rows(_group_of(did), day)
    assert [r["status"] for r in rows] == ["INACTIVE", "INACTIVE", "STALLED", "STATIC"]
    assert [r["show_device"] for r in rows] == [True, False, False, False]
    assert rows[0]["end"].startswith(f"{day}T01:30")


def test_pdf_caps_each_device_to_its_longest_changes_in_time_order():
    did = _make_device()
    day = "2026-08-16"
    spans = []
    for h in range(6):                       # 6 outages, the 03:00 and 05:00 ones longest
        mins = 50 if h in (3, 5) else 10
        spans += [("INACTIVE", f"{h:02d}:00", f"{h:02d}:{mins}"),
                  ("ACTIVE", f"{h:02d}:{mins}", f"{h + 1:02d}:00")]
    _windows(did, day, spans)
    rows = downtime_service.status_change_rows(_group_of(did), day, per_device=2)
    changes = [r for r in rows if r["kind"] == "change"]
    assert [r["start"][11:16] for r in changes] == ["03:00", "05:00"]
    assert changes[0]["show_device"]
    [more] = [r for r in rows if r["kind"] == "more"]
    assert more["hidden"] == 4 and more["hidden_hours"] == round(40 / 60, 2)
