"""Load seed/sample_report_20260816.json into the DB.

CLAUDE.md guardrail: build the DB schema + report template + Playwright pipeline
end-to-end against this before the ThingsBoard client exists. The sample is
report-shaped (it mirrors the approved mockups), so this loader fans it back out
into device_groups / devices / device_keys / snapshots / status_events / remarks
/ pic_followups such that report_service.build_context() reproduces it.

Idempotent: clears seed-tagged rows first, then reloads.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import settings
from ..db.database import get_conn
from . import ops

log = logging.getLogger("cranes.seed")
TZ = ZoneInfo(settings.timezone)

ACTIVE = "ACTIVE"
_COUNT_RE = re.compile(r"(\d+)\s+([A-Za-z]+)")


def _abbr(site: str) -> str:
    return "".join(w[0] for w in site.split() if w).upper() or "SITE"


def _parse_status_counts(text: str) -> list[tuple[int, str]]:
    return [(int(n), lbl.upper()) for n, lbl in _COUNT_RE.findall(text or "")]


def _synth_events(device_status: str, active_hrs: float, affected_hrs: float,
                  issue_occ: int, day: str) -> list[dict]:
    """Alternating ACTIVE / down segments that sum to the sample's hours."""
    start = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=TZ)
    day_end = datetime.fromisoformat(f"{day}T23:59:59").replace(tzinfo=TZ)
    down_label = device_status if device_status != ACTIVE else "INACTIVE"

    n_down = max(issue_occ, 1 if affected_hrs > 0 else 0)
    if n_down == 0:
        return [{"status": ACTIVE, "start": start, "end": day_end}]

    ends_down = device_status != ACTIVE
    down_dur = timedelta(hours=affected_hrs / n_down) if n_down else timedelta()
    n_active = n_down if ends_down else n_down + 1
    active_dur = timedelta(hours=active_hrs / n_active) if n_active else timedelta()

    events: list[dict] = []
    cursor = start
    for i in range(n_down):
        if active_dur:
            events.append({"status": ACTIVE, "start": cursor, "end": cursor + active_dur})
            cursor += active_dur
        last = i == n_down - 1
        end = None if (last and ends_down) else cursor + down_dur
        events.append({"status": down_label, "start": cursor, "end": end})
        cursor = (end or day_end)
    if not ends_down and active_dur:
        events.append({"status": ACTIVE, "start": cursor, "end": day_end})
    return events


def _upsert_group(conn, name, sort, *, site_label=None, system_type=None, hours=24) -> int:
    conn.execute(
        """INSERT INTO device_groups (name, kind, site_label, system_type,
               expected_report_hours, sort_order)
           VALUES (?, 'device', ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               site_label = COALESCE(excluded.site_label, device_groups.site_label),
               system_type = COALESCE(excluded.system_type, device_groups.system_type),
               expected_report_hours = excluded.expected_report_hours,
               sort_order = excluded.sort_order""",
        (name, site_label, system_type, hours, sort),
    )
    return conn.execute("SELECT id FROM device_groups WHERE name = ?", (name,)).fetchone()["id"]


def _upsert_device(conn, group_id, name, dtype, sort) -> int:
    conn.execute(
        """INSERT INTO devices (group_id, name, device_type, sort_order)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(group_id, name) DO UPDATE SET
               device_type = excluded.device_type, sort_order = excluded.sort_order""",
        (group_id, name, dtype, sort),
    )
    return conn.execute(
        "SELECT id FROM devices WHERE group_id = ? AND name = ?", (group_id, name)
    ).fetchone()["id"]


def _device_row(conn, device_id, day, status, active_hrs, affected_hrs, issue_occ,
                signal_health=None, recommendation=None) -> None:
    status = status.upper()
    for key, role in (("status", "status"), ("signal_health", "signal"),
                      ("recommendation", "metric")):
        conn.execute(
            "INSERT OR IGNORE INTO device_keys (device_id, key_name, role) VALUES (?, ?, ?)",
            (device_id, key, role),
        )
    cap_ts = f"{day}T23:59:00+08:00"
    for key, val in (("status", status), ("signal_health", signal_health),
                     ("recommendation", recommendation)):
        if val is None:
            continue
        conn.execute(
            """INSERT INTO snapshots (capture_ts, capture_date, trigger, device_id, key_name, value)
               VALUES (?, ?, 'seed', ?, ?, ?)
               ON CONFLICT(capture_ts, device_id, key_name) DO UPDATE SET value = excluded.value""",
            (cap_ts, day, device_id, key, str(val)),
        )
    for ev in _synth_events(status, active_hrs, affected_hrs, issue_occ, day):
        end = ev["end"]
        dur = int((end - ev["start"]).total_seconds()) if end else None
        conn.execute(
            """INSERT INTO status_events (device_id, status, start_ts, end_ts,
                   duration_seconds, source)
               VALUES (?, ?, ?, ?, ?, 'seed')""",
            (device_id, ev["status"], ev["start"].isoformat(),
             end.isoformat() if end else None, dur),
        )


def load_seed() -> dict:
    data = json.loads(settings.seed_file.read_text())
    day = data["report_date"]
    detail = data.get("site_detail_example", {})
    detail_site = detail.get("site")

    with get_conn() as conn:
        # wipe previous seed so reloads are clean
        conn.execute("DELETE FROM status_events WHERE source = 'seed'")
        conn.execute("DELETE FROM snapshots WHERE trigger = 'seed'")
        conn.execute("DELETE FROM remarks WHERE author = 'seed'")
        conn.execute("DELETE FROM pic_followups WHERE report_date = ? AND date_assigned >= ?",
                     (day, day))

        groups = 0
        devices = 0
        for gi, site in enumerate(data.get("sites_overview", [])):
            is_detail = site["site"] == detail_site
            gid = _upsert_group(
                conn, site["site"], gi,
                site_label=detail.get("location") if is_detail else None,
                system_type=detail.get("system_type") if is_detail else None,
                hours=detail.get("expected_reporting_hrs", 24) if is_detail else 24,
            )
            groups += 1

            if is_detail:
                for di, d in enumerate(detail.get("devices", [])):
                    did = _upsert_device(conn, gid, d["device"], d.get("type"), di)
                    _device_row(conn, did, day, d["status"], d.get("active_hrs", 24.0),
                                d.get("affected_hrs", 0.0), d.get("issue_occ", 0),
                                d.get("signal_health"), d.get("recommendation"))
                    devices += 1
            else:
                # fan the "10 Active / 1 Static / ..." string into placeholder devices
                counts = _parse_status_counts(site.get("current_status", ""))
                non_active = sum(n for n, lbl in counts if lbl != ACTIVE) or 1
                per_bad = site.get("no_data_hrs", 0.0) / non_active
                di = 0
                for n, label in counts:
                    for _ in range(n):
                        di += 1
                        name = f"{_abbr(site['site'])}-{di:02d}"
                        did = _upsert_device(conn, gid, name, "SENSOR", di)
                        if label == ACTIVE:
                            _device_row(conn, did, day, ACTIVE, 24.0, 0.0, 0)
                        else:
                            _device_row(conn, did, day, label,
                                        max(24.0 - per_bad, 0.0), per_bad, 1)
                        devices += 1

            conn.execute(
                "INSERT INTO remarks (group_id, report_date, body, author) VALUES (?, ?, ?, 'seed')",
                (gid, day, site.get("remark", "")),
            )

        # PIC follow-ups (detailed site only)
        detail_gid = conn.execute(
            "SELECT id FROM device_groups WHERE name = ?", (detail_site,)
        ).fetchone()
        fus = 0
        if detail_gid:
            for f in detail.get("pic_followups", []):
                dev_name = re.split(r"\s+[—-]\s+", f["issue"])[0].strip()
                drow = conn.execute(
                    "SELECT id FROM devices WHERE group_id = ? AND name = ?",
                    (detail_gid["id"], dev_name),
                ).fetchone()
                conn.execute(
                    """INSERT INTO pic_followups (group_id, report_date, device_id, issue,
                           remark, assigned_pic, date_assigned)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (detail_gid["id"], day, drow["id"] if drow else None, f["issue"],
                     f["remark"], f["assigned_pic"], f.get("target_date", day)),
                )
                fus += 1

    result = {"date": day, "groups": groups, "devices": devices, "followups": fus}
    ops.record("seed", "success", "manual", json.dumps(result))
    log.info("seed loaded: %s", result)
    return result
