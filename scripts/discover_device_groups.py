"""Build-order step 1: walk the ThingsBoard API and emit device_groups.yaml.

    python -m scripts.discover_device_groups              # print to stdout
    python -m scripts.discover_device_groups --write      # overwrite the config file

Needs THINGSBOARD_* in .env. Review the output before trusting it — in
particular set each device's `role: status` key, plus site_label / system_type
(those aren't in ThingsBoard).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import yaml

from backend.app.config import settings
from backend.app.services.thingsboard_client import tb_client

# The 22 groups span device + alarm + trigger group types. Adjust if needed.
GROUP_TYPES = ["DEVICE"]

STATUS_HINTS = ("status", "state", "device_status", "connectivity")


async def build() -> dict:
    out_groups: list[dict] = []
    for gtype in GROUP_TYPES:
        groups = await tb_client.entity_groups(gtype)
        for g in groups:
            gid = g["id"]["id"] if isinstance(g.get("id"), dict) else g.get("id")
            name = g.get("name", "unnamed")
            devices = await tb_client.group_devices(gid)
            dev_entries = []
            for d in devices:
                did = d["id"]["id"] if isinstance(d.get("id"), dict) else d.get("id")
                try:
                    keys = await tb_client.timeseries_keys(did)
                except Exception:  # noqa: BLE001
                    keys = []
                status_key = next((k for k in keys if k.lower() in STATUS_HINTS), None)
                key_entries = []
                for k in keys:
                    role = "status" if k == status_key else "metric"
                    key_entries.append({"key_name": k, "role": role})
                if not status_key:
                    key_entries.insert(0, {"key_name": "status", "role": "status"})
                dev_entries.append({
                    "name": d.get("name"),
                    "device_type": d.get("type"),
                    "tb_device_id": did,
                    "keys": key_entries,
                })
            out_groups.append({
                "name": name,
                "kind": gtype.lower(),
                "tb_group_id": gid,
                "site_label": None,      # TODO fill in
                "system_type": None,     # TODO fill in
                "expected_report_hours": 24,
                "devices": dev_entries,
            })
    await tb_client.close()
    return {"status_key_default": "status", "groups": out_groups}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="overwrite backend/config/device_groups.yaml")
    args = ap.parse_args()

    data = asyncio.run(build())
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if args.write:
        settings.device_groups_config.write_text(text)
        print(f"wrote {settings.device_groups_config}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
