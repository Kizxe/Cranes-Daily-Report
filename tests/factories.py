"""Build a site directly in the test database.

The report pipeline used to be exercised through the sample-data loader. That loader
is gone — it kept landing in real reports — so tests construct exactly the site they
need here instead. Each test states its own devices, statuses and hours, which makes
the assertions readable without cross-referencing a fixture file.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.app.config import settings
from backend.app.db.database import get_conn

TZ = ZoneInfo(settings.timezone)
DATE = "2026-08-16"

HOUR_MS = 3_600_000


def device(name, *, type="RHT", status="ACTIVE", active_h=24.0, affected_h=0.0,
           issues=0, recommendation=None) -> dict:
    """One device spec. `recommendation` None means the report auto-fills 'No action.'"""
    return {"name": name, "type": type, "status": status, "active_h": active_h,
            "affected_h": affected_h, "issues": issues, "recommendation": recommendation}


def make_site(name="Test Site", devices=(), *, date=DATE, remark=None,
              site_label=None, system_type=None, sort_order=0) -> dict:
    """Insert a site and everything the report needs to render it. Returns {name: device_id}.

    Hours are written as forTotalUse_ counter pairs on `date` and the day before, which
    is the path the report actually takes (downtime_service.counter_hours), so a test
    asserting 13.2 active hours gets exactly 13.2. `issues` becomes that many downtime
    windows in status_events, because ISSUE OCC. is counted off the events themselves.
    """
    ids: dict[str, int] = {}
    prev = (datetime.fromisoformat(date) - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        gid = conn.execute(
            "INSERT INTO device_groups (name, kind, site_label, system_type, sort_order)"
            " VALUES (?, 'device', ?, ?, ?)",
            (name, site_label, system_type, sort_order),
        ).lastrowid

        for order, d in enumerate(devices):
            did = conn.execute(
                "INSERT INTO devices (group_id, name, device_type, sort_order) VALUES (?, ?, ?, ?)",
                (gid, d["name"], d["type"], order),
            ).lastrowid
            ids[d["name"]] = did

            for key, role in (("status", "status"),
                              (f"forTotalUse_{d['name']}", "total_use"),
                              (f"{d['name']} 1D", "daily_issues")):
                conn.execute(
                    "INSERT INTO device_keys (device_id, key_name, role) VALUES (?, ?, ?)",
                    (did, key, role),
                )

            def snap(day, key, value):
                conn.execute(
                    "INSERT INTO snapshots (capture_ts, capture_date, trigger, device_id,"
                    " key_name, value) VALUES (?, ?, 'scheduled', ?, ?, ?)",
                    (f"{day}T23:59:00+08:00", day, did, key, value),
                )

            snap(date, "status", d["status"])
            snap(prev, f"forTotalUse_{d['name']}", "[0, 0]")
            snap(date, f"forTotalUse_{d['name']}",
                 f"[{int(d['affected_h'] * HOUR_MS)}, {int(d['active_h'] * HOUR_MS)}]")
            # Still captured — it just no longer drives ISSUE OCC.
            snap(date, f"{d['name']} 1D", str(d["issues"]))

            def at(hhmm):
                return datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=TZ)

            # A device that is down right now holds one open window, and that outage is
            # itself one of the day's occurrences.
            down_now = d["status"] != "ACTIVE"
            for i in range(max(d["issues"] - (1 if down_now else 0), 0)):
                start = at("01:00:00") + timedelta(minutes=30 * i)
                end = start + timedelta(minutes=5)
                conn.execute(
                    "INSERT INTO status_events (device_id, status, start_ts, end_ts,"
                    " duration_seconds, source) VALUES (?, 'INACTIVE', ?, ?, 300, 'test')",
                    (did, start.isoformat(), end.isoformat()),
                )
            if down_now:
                conn.execute(
                    "INSERT INTO status_events (device_id, status, start_ts, source)"
                    " VALUES (?, ?, ?, 'test')",
                    (did, d["status"], at("12:00:00").isoformat()),
                )

            if d["recommendation"]:
                conn.execute(
                    "INSERT INTO remarks (group_id, device_id, report_date, body, author)"
                    " VALUES (?, ?, ?, ?, 'test')",
                    (gid, did, date, d["recommendation"]),
                )

        if remark:
            conn.execute(
                "INSERT INTO remarks (group_id, report_date, body, author)"
                " VALUES (?, ?, ?, 'test')",
                (gid, date, remark),
            )

    return {"group_id": gid, **ids}
