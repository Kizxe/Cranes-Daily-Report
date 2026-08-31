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
    is the path the report actually takes (downtime_service.counter_hours). Issue counts
    go in as the trigger's "<device> 1D" key. Both are exact, so a test asserting 13.2
    active hours gets 13.2 rather than something reconstructed from synthetic events.
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
            snap(date, f"{d['name']} 1D", str(d["issues"]))

            # One open event so the DOWNTIME EVENTS table has something to show for a
            # device that isn't active.
            if d["status"] != "ACTIVE":
                start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=TZ)
                conn.execute(
                    "INSERT INTO status_events (device_id, status, start_ts, source)"
                    " VALUES (?, ?, ?, 'test')",
                    (did, d["status"], start.isoformat()),
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
