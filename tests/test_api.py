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
    client.post("/api/seed/load")
    gid = client.get("/api/groups").json()[0]["id"]
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


def test_followup_create(client):
    client.post("/api/seed/load")
    gid = client.get("/api/groups").json()[0]["id"]
    r = client.post("/api/followups", json={
        "group_id": gid, "report_date": "2026-08-16",
        "issue": "X down", "remark": "check it", "assigned_pic": "Eng",
        "date_assigned": "2026-08-18",
    })
    assert r.status_code == 201
    assert r.json()["issue"] == "X down"
