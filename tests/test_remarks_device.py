"""Per-device recommendations vs site-level remarks."""
import pytest

from backend.app.db.database import get_conn

DATE = "2026-08-16"


@pytest.fixture
def group_and_device():
    with get_conn() as conn:
        gid = conn.execute(
            "INSERT INTO device_groups (name, kind) VALUES ('G1', 'device')"
        ).lastrowid
        did = conn.execute(
            "INSERT INTO devices (group_id, name, device_type) VALUES (?, 'D1', 'RHT')",
            (gid,),
        ).lastrowid
        other_gid = conn.execute(
            "INSERT INTO device_groups (name, kind) VALUES ('G2', 'device')"
        ).lastrowid
    return {"group_id": gid, "device_id": did, "other_group_id": other_gid}


def test_device_remark_upserts_to_one_row(client, group_and_device):
    body = {"group_id": group_and_device["group_id"], "report_date": DATE,
            "device_id": group_and_device["device_id"], "body": "first"}
    assert client.post("/api/remarks", json=body).status_code == 201
    assert client.post("/api/remarks", json={**body, "body": "second"}).status_code == 201

    rows = client.get(f"/api/remarks?scope=device&report_date={DATE}").json()
    assert len(rows) == 1
    assert rows[0]["body"] == "second"
    assert rows[0]["device_name"] == "D1"


def test_site_remarks_still_allow_many_per_day(client, group_and_device):
    """The partial index must not constrain device_id IS NULL rows."""
    body = {"group_id": group_and_device["group_id"], "report_date": DATE, "body": "one"}
    assert client.post("/api/remarks", json=body).status_code == 201
    assert client.post("/api/remarks", json={**body, "body": "two"}).status_code == 201

    rows = client.get(f"/api/remarks?scope=site&report_date={DATE}").json()
    assert len(rows) == 2
    assert all(r["device_id"] is None for r in rows)


def test_scope_filter_separates_the_two_kinds(client, group_and_device):
    gid, did = group_and_device["group_id"], group_and_device["device_id"]
    client.post("/api/remarks", json={"group_id": gid, "report_date": DATE, "body": "site"})
    client.post("/api/remarks", json={"group_id": gid, "report_date": DATE,
                                      "device_id": did, "body": "device"})

    assert [r["body"] for r in client.get("/api/remarks?scope=site").json()] == ["site"]
    assert [r["body"] for r in client.get("/api/remarks?scope=device").json()] == ["device"]
    assert len(client.get("/api/remarks").json()) == 2       # unfiltered = both


def test_device_must_belong_to_the_named_group(client, group_and_device):
    r = client.post("/api/remarks", json={
        "group_id": group_and_device["other_group_id"], "report_date": DATE,
        "device_id": group_and_device["device_id"], "body": "wrong group",
    })
    assert r.status_code == 400


def test_unknown_device_is_404(client, group_and_device):
    r = client.post("/api/remarks", json={
        "group_id": group_and_device["group_id"], "report_date": DATE,
        "device_id": 9999, "body": "nope",
    })
    assert r.status_code == 404
