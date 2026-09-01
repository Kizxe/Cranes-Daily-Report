from datetime import datetime

from backend.app.db.database import get_conn
from tests.factories import device, make_site

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_groups_empty_by_default(client):
    assert client.get("/api/groups").json() == []


def test_capture_runs_with_no_devices(client):
    r = client.post("/api/captures/run")
    assert r.status_code == 200
    body = r.json()
    assert body["trigger"] == "manual"
    assert body["devices"] == 0


def test_generate_refuses_empty_report(client):
    r = client.post("/api/reports/2026-08-16/generate")
    assert r.status_code == 409


def test_last_run_reports_capture(client):
    client.post("/api/captures/run")
    s = client.get("/api/status/last-run").json()
    assert s["last_capture"]["status"] == "success"


def test_remark_crud(client):
    gid = make_site("S", [device("D1")])["group_id"]
    created = client.post("/api/remarks", json={
        "group_id": gid, "report_date": "2026-08-16", "body": "hello",
    })
    assert created.status_code == 201
    rid = created.json()["id"]
    upd = client.put(f"/api/remarks/{rid}", json={
        "group_id": gid, "report_date": "2026-08-16", "body": "changed",
    })
    assert upd.json()["body"] == "changed"
    assert client.delete(f"/api/remarks/{rid}").status_code == 204


def test_downtime_events_follow_the_date(client):
    did = make_site("S", [device("D1")])["D1"]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, ?, ?, ?, 'poll')",
            [
                (did, "INACTIVE", "2026-08-31T09:00:00+08:00", "2026-08-31T10:00:00+08:00"),
                (did, "INACTIVE", "2026-09-01T14:00:00+08:00", "2026-09-01T15:00:00+08:00"),
            ],
        )
    all_ev = client.get(f"/api/downtime/events/{did}").json()
    assert len(all_ev) == 2
    aug31 = client.get(f"/api/downtime/events/{did}?date=2026-08-31").json()
    assert [e["start_ts"] for e in aug31] == ["2026-08-31T09:00:00+08:00"]
    assert client.get(f"/api/downtime/events/{did}?date=2026-08-30").json() == []


def test_a_window_spanning_midnight_is_clipped_to_the_picked_day(client):
    """The drill-down must never print another day's timestamps under the date picker."""
    did = make_site("S", [device("D1")])["D1"]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO status_events (device_id, status, start_ts, end_ts, source)"
            " VALUES (?, 'INACTIVE', '2026-08-31T22:00:00+08:00', '2026-09-01T05:30:00+08:00',"
            " 'poll')",
            (did,),
        )
    [e] = client.get(f"/api/downtime/events/{did}?date=2026-09-01").json()
    assert e["start_ts"].startswith("2026-09-01T00:00:00")
    assert e["end_ts"].startswith("2026-09-01T05:30:00")
    assert e["carried_in"] is True and e["carried_out"] is False
    assert e["actual_start_ts"].startswith("2026-08-31T22:00:00")
    assert e["duration_seconds"] == 5 * 3600 + 1800   # only the part inside the day

    [e] = client.get(f"/api/downtime/events/{did}?date=2026-08-31").json()
    assert e["end_ts"].startswith("2026-08-31T23:59:59")
    assert e["carried_in"] is False and e["carried_out"] is True


def test_reconcile_endpoint_rebuilds_the_day_from_history(client, monkeypatch):
    """POST /api/downtime/reconcile is the manual door to the hourly reconcile pass."""
    from backend.app.services import reconcile_service as rec

    did = make_site("S", [device("D1")], date="2026-08-30")["D1"]
    with get_conn() as conn:
        conn.execute("UPDATE device_keys SET key_name = 'deviceStatus_D1',"
                     " tb_source_device_id = 'trigger-1' WHERE device_id = ?"
                     " AND role = 'status'", (did,))
        conn.execute("DELETE FROM status_events WHERE device_id = ?", (did,))

    def ms(iso):
        return int(datetime.fromisoformat(iso).timestamp() * 1000)

    async def fake_history(device_id, keys, start_ms, end_ms):
        return {"deviceStatus_D1": [
            {"ts": ms("2026-08-30T08:00:00+08:00"), "value": "ACTIVE"},
            {"ts": ms("2026-08-30T09:00:00+08:00"), "value": "INACTIVE"},
            {"ts": ms("2026-08-30T09:05:00+08:00"), "value": "ACTIVE"},
        ]}

    monkeypatch.setattr(rec.tb_client, "timeseries_history", fake_history)
    body = client.post("/api/downtime/reconcile?date=2026-08-30").json()
    assert (body["events"], body["devices"], body["refresh"]) == (3, 1, True)

    evs = client.get(f"/api/downtime/events/{did}?date=2026-08-30").json()
    assert [e["status"] for e in evs] == ["ACTIVE", "INACTIVE", "ACTIVE"]   # newest first
    assert [e["start_ts"][11:19] for e in evs] == ["09:05:00", "09:00:00", "08:00:00"]
    assert evs[1]["duration_seconds"] == 300

