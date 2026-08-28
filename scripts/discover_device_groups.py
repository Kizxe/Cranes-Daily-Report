"""Build-order step 1: walk ThingsBoard and emit a site's block for device_groups.yaml.

    # see what a site would produce, without touching the config
    python -m scripts.discover_device_groups --type Numed --name NUMed

    # keep only real sensors (TB `label` is RHT / DPM / UFM / ...)
    python -m scripts.discover_device_groups --type Numed --name NUMed --labels RHT,DPM,UFM

    # merge it into backend/config/device_groups.yaml (other sites are preserved)
    python -m scripts.discover_device_groups --type Numed --name NUMed --labels RHT,DPM,UFM --write

    # what device types exist on the instance?
    python -m scripts.discover_device_groups --list-types

Sites are identified by the ThingsBoard device `type` field, NOT by entity group —
the entity groups on this instance ("I4TAP NG", "BSC RHT", "QL", ...) don't line up
with the reported sites. `label` carries the sensor kind (RHT/DPM/UFM), which becomes
device_type in the report's DEVICE TYPE BREAKDOWN.

Only ONE key per device is emitted: the status key. It is the sole key the report
reads (device_keys.role='status'); active/affected hours, issue counts and the
downtime-events table are all derived from its history by the poll loop.

ALWAYS review the output. The status key is a guess from name hints, and site_label /
system_type don't exist in ThingsBoard at all.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

import yaml

from backend.app.config import settings
from backend.app.services.thingsboard_client import tb_client

# Ordered best-first: the earliest match on a device wins.
STATUS_HINTS = ("status", "state", "device_status", "connectivity", "active")


def _uuid(entity: dict) -> str | None:
    ident = entity.get("id")
    return ident["id"] if isinstance(ident, dict) else ident


async def _all_devices() -> list[dict]:
    """Every tenant device. The instance has ~900, one page is enough."""
    data = await tb_client._request(
        "GET", "/tenant/devices",
        params={"pageSize": 5000, "page": 0, "sortProperty": "name", "sortOrder": "ASC"},
    )
    return data.get("data", []) if isinstance(data, dict) else data


async def list_types() -> None:
    devices = await _all_devices()
    counts = Counter((d.get("type") or "—") for d in devices)
    print(f"{len(devices)} devices across {len(counts)} types\n")
    for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        labels = sorted({(d.get("label") or "").strip()
                         for d in devices if (d.get("type") or "—") == t} - {""})
        hint = f"   labels: {', '.join(labels[:6])}" if labels else ""
        print(f"  {n:4d}  {t}{hint}")


async def build_site(tb_type: str, site_name: str, keep_labels: set[str] | None) -> dict:
    devices = [d for d in await _all_devices() if (d.get("type") or "") == tb_type]
    if keep_labels:
        devices = [d for d in devices
                   if (d.get("label") or "").strip().upper() in keep_labels]

    entries = []
    unresolved = []
    for d in sorted(devices, key=lambda x: (x.get("label") or "", x.get("name") or "")):
        did = _uuid(d)
        try:
            keys = await tb_client.timeseries_keys(did)
        except Exception as e:  # noqa: BLE001 — a dead device shouldn't stop the walk
            print(f"  ! {d.get('name')}: {e}", file=sys.stderr)
            keys = []

        lowered = {k.lower(): k for k in keys}
        status_key = next((lowered[h] for h in STATUS_HINTS if h in lowered), None)
        if status_key is None:
            status_key = "status"           # placeholder — must be fixed by hand
            unresolved.append(d.get("name"))

        entries.append({
            "name": d.get("name"),
            # TB `label` is the sensor kind; fall back to the type.
            "device_type": (d.get("label") or tb_type).strip() or tb_type,
            "tb_device_id": did,
            "keys": [{"key_name": status_key, "role": "status"}],
        })

    if unresolved:
        print(f"  ! no status-like key on {len(unresolved)} device(s), left as "
              f"'status': {', '.join(unresolved[:5])}", file=sys.stderr)

    return {
        "name": site_name,
        "kind": "device",
        "tb_group_id": None,
        "site_label": None,       # TODO — not in ThingsBoard, fill in by hand
        "system_type": None,      # TODO — e.g. "Chiller Optimization System (COpti)"
        "expected_report_hours": 24,
        "devices": entries,
    }


def merge_into_config(site: dict) -> None:
    """Replace this one site in device_groups.yaml, leaving every other site alone."""
    path = settings.device_groups_config
    doc = yaml.safe_load(path.read_text()) or {}
    doc.setdefault("status_key_default", "status")
    groups = doc.get("groups") or []

    for i, g in enumerate(groups):
        if g.get("name") == site["name"]:
            # Keep the hand-written fields a re-run can't rediscover.
            for field in ("site_label", "system_type", "expected_report_hours"):
                if g.get(field) is not None:
                    site[field] = g[field]
            groups[i] = site
            break
    else:
        groups.append(site)

    doc["groups"] = groups
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    print(f"wrote {path} ({len(groups)} site(s), "
          f"{sum(len(g.get('devices') or []) for g in groups)} devices)", file=sys.stderr)


async def _run(args) -> None:
    try:
        if args.list_types:
            await list_types()
            return
        site = await build_site(args.type, args.name or args.type,
                                {s.strip().upper() for s in args.labels.split(",")}
                                if args.labels else None)
        print(f"  {len(site['devices'])} device(s) for {site['name']}", file=sys.stderr)
        if args.write:
            merge_into_config(site)
        else:
            print(yaml.safe_dump({"groups": [site]}, sort_keys=False, allow_unicode=True))
    finally:
        await tb_client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-types", action="store_true",
                    help="show every device type on the instance, then exit")
    ap.add_argument("--type", help="ThingsBoard device type identifying the site, e.g. Numed")
    ap.add_argument("--name", help="site name for the report (defaults to --type)")
    ap.add_argument("--labels", help="comma-separated TB labels to keep, e.g. RHT,DPM,UFM")
    ap.add_argument("--write", action="store_true",
                    help="merge this site into device_groups.yaml (other sites preserved)")
    args = ap.parse_args()

    if not args.list_types and not args.type:
        ap.error("give --type <TB device type>, or --list-types to see what exists")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
