"""Look inside cranes.db without writing SQL.

    python -m scripts.inspect_db                     # overview: sites, last runs, health
    python -m scripts.inspect_db --site NUMed        # every device + today's status
    python -m scripts.inspect_db --device 12         # one device: keys, values, downtime
    python -m scripts.inspect_db --date 2026-08-30   # any of the above for another day
    python -m scripts.inspect_db --sql "SELECT ..."  # escape hatch, read-only

Read-only: it opens the same SQLite file the app uses and never writes. Safe to run
while the server is up (WAL allows concurrent readers).
"""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.config import settings
from backend.app.db.database import read_conn
from backend.app.services import downtime_service as dt

TZ = ZoneInfo(settings.timezone)


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "─" * 78)


def overview(date: str) -> None:
    with read_conn() as conn:
        print(f"db: {settings.db_path}  ({settings.db_path.stat().st_size / 1e6:.1f} MB)")

        _rule(f"SITES  ({date})")
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.site_label, COUNT(d.id) AS devices
            FROM device_groups g LEFT JOIN devices d ON d.group_id = g.id
            GROUP BY g.id ORDER BY g.sort_order, g.name
            """
        ).fetchall()
        status_of = dt.current_status_map(date)
        for r in rows:
            devs = [x["id"] for x in conn.execute(
                "SELECT id FROM devices WHERE group_id = ?", (r["id"],))]
            bad = sum(1 for i in devs if dt.severity(status_of.get(i, "UNKNOWN")) == "bad")
            warn = sum(1 for i in devs if dt.severity(status_of.get(i, "UNKNOWN")) == "warn")
            flag = "ATTENTION" if bad else "HEALTHY"
            print(f"  [{r['id']:>3}] {r['name']:<22} {r['devices']:>3} devices   "
                  f"{flag:<9} {bad} bad / {warn} warn   {r['site_label'] or ''}")

        _rule("CAPTURES")
        for r in conn.execute(
            """SELECT capture_date, capture_ts, trigger, COUNT(*) AS keys,
                      SUM(value IS NOT NULL) AS got
               FROM snapshots GROUP BY capture_ts ORDER BY capture_ts DESC LIMIT 8"""
        ):
            print(f"  {r['capture_date']}  {r['capture_ts'][11:19]}  {r['trigger']:<9} "
                  f"{r['got']}/{r['keys']} values")

        _rule("JOB RUNS  (did last night work?)")
        for r in conn.execute(
            "SELECT job, trigger, status, detail, ran_at FROM job_runs "
            "ORDER BY ran_at DESC LIMIT 10"
        ):
            mark = "ok " if r["status"] == "success" else "FAIL"
            print(f"  {mark} {r['ran_at']}  {r['job']:<8} {r['trigger']:<9} {r['detail'] or ''}")

        _rule("REPORTS")
        for r in conn.execute(
            "SELECT report_date, status, pdf_path, generated_at, error FROM reports "
            "ORDER BY generated_at DESC LIMIT 8"
        ):
            print(f"  {r['report_date']}  {r['status']:<9} {r['generated_at']}  "
                  f"{r['error'] or r['pdf_path']}")

        _rule("REMARKS")
        for r in conn.execute(
            """SELECT r.report_date, g.name AS site, d.name AS device, r.body
               FROM remarks r JOIN device_groups g ON g.id = r.group_id
               LEFT JOIN devices d ON d.id = r.device_id
               ORDER BY r.report_date DESC, r.id DESC LIMIT 10"""
        ):
            who = r["device"] or "(site remark)"
            print(f"  {r['report_date']}  {r['site']:<12} {who[:28]:<30} {r['body'][:40]}")

    print("\nnext: --site <name> for a device list, --device <id> for one device's day")


def site_detail(name: str, date: str) -> None:
    with read_conn() as conn:
        g = conn.execute(
            "SELECT * FROM device_groups WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if not g:
            names = [r["name"] for r in conn.execute("SELECT name FROM device_groups")]
            raise SystemExit(f"no site {name!r}. Have: {', '.join(names) or '(none)'}")
        devices = conn.execute(
            "SELECT id, name, device_type FROM devices WHERE group_id = ? ORDER BY sort_order",
            (g["id"],),
        ).fetchall()

    status_of = dt.current_status_map(date)
    _rule(f"{g['name']}  —  {len(devices)} devices  —  {date}")
    print(f"  {'id':>4}  {'type':<10} {'device':<50} {'status':<10} "
          f"{'active h':>8} {'affected':>8}  issues")
    for d in devices:
        s = dt.day_summary(d["id"], date)
        st = status_of.get(d["id"], "UNKNOWN")
        print(f"  {d['id']:>4}  {d['device_type'] or '':<10} {d['name'][:48]:<50} {st:<10} "
              f"{s['active_hours']:>8} {s['affected_hours']:>8}  {s['issue_occurrences']}")
    print("\n  status: what role='status' reported in the latest snapshot for that date.")
    print("  active/affected hours come from status_events, clipped to the day.")


def device_detail(device_id: int, date: str) -> None:
    with read_conn() as conn:
        d = conn.execute(
            """SELECT d.*, g.name AS site FROM devices d
               JOIN device_groups g ON g.id = d.group_id WHERE d.id = ?""",
            (device_id,),
        ).fetchone()
        if not d:
            raise SystemExit(f"no device with id {device_id}")
        keys = conn.execute(
            "SELECT key_name, role, tb_source_device_id FROM device_keys "
            "WHERE device_id = ? ORDER BY role", (device_id,)
        ).fetchall()
        values = conn.execute(
            """SELECT key_name, value, value_ts FROM snapshots
               WHERE device_id = ? AND capture_date = ?
                 AND capture_ts = (SELECT MAX(capture_ts) FROM snapshots
                                    WHERE device_id = ? AND capture_date = ?)
               ORDER BY key_name""",
            (device_id, date, device_id, date),
        ).fetchall()

    _rule(f"{d['site']} / {d['name']}   (device id {device_id}, type {d['device_type']})")
    print(f"  own tb_device_id: {d['tb_device_id'] or '— (status comes off the trigger)'}")

    _rule("CONFIGURED KEYS")
    for k in keys:
        src = k["tb_source_device_id"] or "(own device)"
        print(f"  {k['role']:<13} {k['key_name'][:52]:<54} from {src}")

    _rule(f"LATEST SNAPSHOT VALUES  ({date})")
    for v in values:
        print(f"  {v['key_name'][:54]:<56} = {str(v['value'])[:24]:<26} @ {v['value_ts'] or '—'}")
    if not values:
        print("  (nothing captured for this date — run a capture)")

    s = dt.day_summary(device_id, date)
    _rule(f"DOWNTIME  ({date})")
    print(f"  active {s['active_hours']}h · affected {s['affected_hours']}h · "
          f"covered {s['covered_hours']}h · {s['active_pct']}% of day · "
          f"{s['issue_occurrences']} issue(s)")
    for e in s["events"]:
        print(f"    {e['status']:<10} {e['start'][11:19]} -> {e['end'][11:19]}  "
              f"{e['seconds_in_day'] // 60:>5} min")
    if not s["events"]:
        print("    (no status_events overlap this day — run a poll)")


def run_sql(sql: str) -> None:
    if not sql.lstrip().lower().startswith(("select", "with", "pragma", "explain")):
        raise SystemExit("read-only: SELECT / WITH / PRAGMA / EXPLAIN only")
    with read_conn() as conn:
        rows = conn.execute(sql).fetchall()
    if not rows:
        print("(no rows)")
        return
    cols = rows[0].keys()
    print(" | ".join(cols))
    print("-" * 78)
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))
    print(f"\n{len(rows)} row(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", help="site name, e.g. NUMed")
    ap.add_argument("--device", type=int, help="device id (from --site)")
    ap.add_argument("--date", default=_today(), help="YYYY-MM-DD (default: today)")
    ap.add_argument("--sql", help="read-only query")
    args = ap.parse_args()

    if not settings.db_path.exists():
        raise SystemExit(f"no database at {settings.db_path} — start the app once first")
    if args.sql:
        run_sql(args.sql)
    elif args.device:
        device_detail(args.device, args.date)
    elif args.site:
        site_detail(args.site, args.date)
    else:
        overview(args.date)


if __name__ == "__main__":
    main()
