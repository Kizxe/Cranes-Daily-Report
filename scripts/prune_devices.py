"""Delete devices that are no longer in the site config.

    python -m scripts.prune_devices --site NUMed              # dry run, lists them
    python -m scripts.prune_devices --site NUMed --apply
    python -m scripts.prune_devices --name "Numed Setpoints" --apply

`config_sync` upserts and never deletes, so a device removed from
`backend/config/sites/<site>.yaml` keeps its DB row — and keeps appearing on the
dashboard and in the report. That is deliberate (an audit tool should not drop history
because a config line moved), which leaves this as the manual door.

Deleting a device takes its `device_keys`, `snapshots`, `status_events` and `remarks`
with it — the schema cascades. Back up first: `python -m scripts.backup`.
"""
from __future__ import annotations

import argparse

from backend.app.db.database import get_conn, read_conn
from backend.app.services import config_sync

CHILD_TABLES = ("device_keys", "snapshots", "status_events", "remarks")


def _configured_names(site: str | None) -> set[str]:
    """Every device name the YAML config still lists (optionally for one site)."""
    names = set()
    for g in config_sync.load_yaml().get("groups", []) or []:
        if site and g["name"].lower() != site.lower():
            continue
        names |= {d["name"] for d in (g.get("devices") or [])}
    return names


def _targets(site: str | None, names: list[str]) -> list[dict]:
    sql = ("SELECT d.id, d.name, g.name AS site FROM devices d "
           "JOIN device_groups g ON g.id = d.group_id")
    params: list = []
    if site:
        sql += " WHERE g.name = ? COLLATE NOCASE"
        params.append(site)
    with read_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY g.name, d.name", params)]

    if names:
        wanted = {n.lower() for n in names}
        rows = [r for r in rows if r["name"].lower() in wanted]
    else:
        configured = _configured_names(site)
        rows = [r for r in rows if r["name"] not in configured]

    with read_conn() as conn:
        for r in rows:
            r["counts"] = {
                t: conn.execute(f"SELECT COUNT(*) n FROM {t} WHERE device_id = ?",
                                (r["id"],)).fetchone()["n"]
                for t in CHILD_TABLES
            }
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", help="limit to one site")
    ap.add_argument("--name", action="append", default=[],
                    help="delete this device by name (repeatable). Without it, every "
                         "device the config no longer lists is a candidate.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    args = ap.parse_args()

    rows = _targets(args.site, args.name)
    if not rows:
        print("nothing to prune — every DB device is still in the config")
        return

    for r in rows:
        detail = ", ".join(f"{n} {t}" for t, n in r["counts"].items() if n)
        print(f"  {r['site']}/{r['name']} (id {r['id']}) — {detail or 'no rows'}")
    print(f"{len(rows)} device(s)")

    if not args.apply:
        print("dry run — nothing deleted. Re-run with --apply (back up first: "
              "python -m scripts.backup)")
        return

    with get_conn() as conn:
        conn.executemany("DELETE FROM devices WHERE id = ?", [(r["id"],) for r in rows])
    print(f"deleted {len(rows)} device(s) and everything that referenced them")


if __name__ == "__main__":
    main()
