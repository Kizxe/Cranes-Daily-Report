"""Build-order step 1: read a site's trigger device into its own config file.

Config is one file per site — `backend/config/sites/<site>.yaml`. This script writes
exactly one of them per run, so re-running a site can never disturb the other 21.

    # what trigger devices exist?
    python -m scripts.discover_device_groups --list-triggers

    # see what a site would produce, without touching the config
    python -m scripts.discover_device_groups --trigger NumedTrigger --name NUMed

    # write backend/config/sites/numed.yaml (every other site's file is untouched)
    python -m scripts.discover_device_groups --trigger NumedTrigger --name NUMed --write

    # drop sensors that aren't really devices at this site
    python -m scripts.discover_device_groups --trigger NumedTrigger --name NUMed \
        --exclude "Numed,Numed Setpoints,Robert Bosch Recovery"

Where status actually comes from
--------------------------------
NOT from the sensor's own device. Each site has ONE trigger device (`NumedTrigger`,
`ComputimeTrigger`, ...) whose rule chain computes status for every sensor at the site
and writes it back as one key per sensor per family:

    active_<sensor>          true / false            (the roster — every sensor has one)
    deviceStatus_<sensor>    ACTIVE | STATIC | NO DATA | ...
    deviceSeverity_<sensor>  OK | NON-CRITICAL | CRITICAL
    activeTs_<sensor>        epoch ms of the last ->active transition
    InactiveTs_<sensor>      epoch ms of the last ->inactive transition
    IssueOcc_<sensor>        issue occurrences to date
    <sensor> 1D              issues on the current day
    ActiveInActive_<sensor>  ["A_<ts>","N_hh:mm:ss", activeMs, inactiveMs]

So the sensor roster is derived from `active_*` and every emitted key carries
`source: trigger`, meaning "read this off the trigger device, not off the sensor".

The site's own `Active Device` / `Inactive Device` counters are deliberately NOT used:
the rule chain computes them over its own subset of sensors and they don't add up to
the roster here. Counts in the report are derived from the roster instead.

ALWAYS review the output — site_label / system_type don't exist in ThingsBoard at all.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys

import yaml

from backend.app.config import settings
from backend.app.services.thingsboard_client import tb_client

# Per-sensor key families on the trigger device -> the role stored in device_keys.
# `status` is resolved separately (deviceStatus_ when the sensor has one, else active_).
FAMILIES: list[tuple[str, str]] = [
    ("active_", "active_flag"),
    ("deviceStatus_", "device_status"),
    ("deviceSeverity_", "severity"),
    ("activeTs_", "active_ts"),
    ("InactiveTs_", "inactive_ts"),
    ("IssueOcc_", "issue_count"),
    ("ActiveInActive_", "uptime_split"),
]

# Sensor kind, read out of the sensor name. First match wins, so RHT beats the
# bare-word fallbacks. Becomes device_type in the report's DEVICE TYPE BREAKDOWN.
TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"\bRHT\b", "RHT"),
    (r"\bDPM\b|_DPM_", "DPM"),
    (r"\bRTD\b", "RTD"),
    (r"\bUFM\b", "UFM"),
    (r"\bFlowmeter\b", "Flowmeter"),
    (r"\bRecovery\b", "Recovery"),
    (r"\bSetpoints?\b", "Setpoints"),
]


# Re-emitted on top of every --write; yaml.safe_dump can't carry comments, so without
# this the file loses its explanation the first time the script rewrites it.
HEADER = """\
# ─────────────────────────────────────────────────────────────────────────────
# {site} — one site of the 22. GENERATED, do not hand-transcribe:
#
#     python -m scripts.discover_device_groups --trigger {trigger} --name {site} --write
#
# A re-run rewrites only this file and preserves site_label / system_type /
# sort_order / expected_report_hours, which don't exist in ThingsBoard and are
# filled in by hand. Every other site lives in its own file beside this one.
#
# Status does NOT come from the sensor's own device. This site's trigger device
# ({trigger}) computes status for every sensor and writes it back as one key per
# sensor: active_<sensor> (bool), deviceStatus_<sensor> (ACTIVE/STATIC/NO DATA),
# deviceSeverity_<sensor>, activeTs_/InactiveTs_<sensor> (transition epoch ms),
# IssueOcc_<sensor>, "<sensor> 1D", ActiveInActive_<sensor>. `source: trigger` on a
# key means "read it off that device"; config_sync stores it as tb_source_device_id.
#
# Exactly one key per device carries role: status — deviceStatus_ when the sensor
# has one, else the active_ boolean (normalised true/false -> ACTIVE/INACTIVE).
#
# Edit this file, then restart or:  curl -X POST localhost:8000/api/config/reload
# ─────────────────────────────────────────────────────────────────────────────

"""

# Fields a re-run must not clobber — they're hand-filled, ThingsBoard has no idea.
PRESERVED = ("site_label", "system_type", "expected_report_hours", "sort_order")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "site"


def _uuid(entity: dict) -> str | None:
    ident = entity.get("id")
    return ident["id"] if isinstance(ident, dict) else ident


def device_type(sensor: str) -> str:
    for pattern, kind in TYPE_PATTERNS:
        if re.search(pattern, sensor, re.IGNORECASE):
            return kind
    return "Other"


async def _all_devices() -> list[dict]:
    """Every tenant device. The instance has ~900, one page is enough."""
    data = await tb_client._request(
        "GET", "/tenant/devices",
        params={"pageSize": 5000, "page": 0, "sortProperty": "name", "sortOrder": "ASC"},
    )
    return data.get("data", []) if isinstance(data, dict) else data


async def list_triggers() -> None:
    devices = [d for d in await _all_devices() if "trigger" in (d.get("name") or "").lower()]
    print(f"{len(devices)} device(s) with 'trigger' in the name:\n")
    for d in sorted(devices, key=lambda x: x.get("name") or ""):
        print(f"  {d.get('name'):34} type={d.get('type') or '—'}")


async def build_site(trigger_name: str, site_name: str, exclude: set[str]) -> dict:
    devices = await _all_devices()
    trigger = next((d for d in devices if (d.get("name") or "") == trigger_name), None)
    if trigger is None:
        raise SystemExit(f"no device named {trigger_name!r} — try --list-triggers")
    trigger_id = _uuid(trigger)

    keys = set(await tb_client.timeseries_keys(trigger_id))
    sensors = sorted(k[len("active_"):] for k in keys if k.startswith("active_"))
    if not sensors:
        raise SystemExit(f"{trigger_name} has no active_* keys — is it really a trigger device?")

    # A sensor may also exist as its own TB device; keep the link when it does.
    own_id = {(d.get("name") or ""): _uuid(d) for d in devices}

    entries = []
    no_status = []
    for sensor in sensors:
        if sensor in exclude:
            continue
        dev_keys = []
        for prefix, role in FAMILIES:
            if prefix + sensor in keys:
                dev_keys.append({"key_name": prefix + sensor, "role": role, "source": "trigger"})
        if f"{sensor} 1D" in keys:
            dev_keys.append({"key_name": f"{sensor} 1D", "role": "daily_issues",
                             "source": "trigger"})

        # deviceStatus_ carries STATIC / NO DATA, which the report's pills need; only
        # ~2/3 of sensors have one, so the rest fall back to the active_ boolean.
        status_key = (f"deviceStatus_{sensor}" if f"deviceStatus_{sensor}" in keys
                      else f"active_{sensor}")
        if not status_key.startswith("deviceStatus_"):
            no_status.append(sensor)
        for k in dev_keys:
            if k["key_name"] == status_key:
                k["role"] = "status"
                break

        entries.append({
            "name": sensor,
            "device_type": device_type(sensor),
            "tb_device_id": own_id.get(sensor),   # usually None — sensor isn't its own device
            "keys": dev_keys,
        })

    if no_status:
        print(f"  ! {len(no_status)} sensor(s) have no deviceStatus_ key, falling back to the "
              f"active_ boolean: {', '.join(no_status[:6])}"
              + (" ..." if len(no_status) > 6 else ""), file=sys.stderr)

    return {
        "name": site_name,
        "kind": "device",
        "tb_group_id": None,
        "site_label": None,       # TODO — not in ThingsBoard, fill in by hand
        "system_type": None,      # TODO — e.g. "Chiller Optimization System (COpti)"
        "expected_report_hours": 24,
        "trigger": {"device_name": trigger_name, "tb_device_id": trigger_id},
        "devices": entries,
    }


def write_site_file(site: dict) -> pathlib.Path:
    """Write backend/config/sites/<slug>.yaml. No other site's file is touched."""
    path = settings.sites_dir / f"{slugify(site['name'])}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = yaml.safe_load(path.read_text()) or {}
        for field in PRESERVED:
            if existing.get(field) is not None:
                site[field] = existing[field]

    header = HEADER.format(site=site["name"], trigger=site["trigger"]["device_name"])
    path.write_text(header + yaml.safe_dump(site, sort_keys=False, allow_unicode=True))
    print(f"wrote {path} ({len(site.get('devices') or [])} devices)", file=sys.stderr)
    return path


async def _run(args) -> None:
    try:
        if args.list_triggers:
            await list_triggers()
            return
        exclude = {s.strip() for s in args.exclude.split(",") if s.strip()} if args.exclude else set()
        site = await build_site(args.trigger, args.name or args.trigger, exclude)
        print(f"  {len(site['devices'])} sensor(s) for {site['name']}", file=sys.stderr)
        if args.write:
            write_site_file(site)
        else:
            print(yaml.safe_dump(site, sort_keys=False, allow_unicode=True))
    finally:
        await tb_client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-triggers", action="store_true",
                    help="show every trigger-looking device on the instance, then exit")
    ap.add_argument("--trigger", help="trigger device name for the site, e.g. NumedTrigger")
    ap.add_argument("--name", help="site name for the report (defaults to --trigger)")
    ap.add_argument("--exclude", help="comma-separated sensor names to leave out")
    ap.add_argument("--write", action="store_true",
                    help="write backend/config/sites/<site>.yaml (other sites untouched)")
    args = ap.parse_args()

    if not args.list_triggers and not args.trigger:
        ap.error("give --trigger <trigger device name>, or --list-triggers to see what exists")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
