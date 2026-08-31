"""Check the ThingsBoard connection, end to end, one layer at a time.

    python -m scripts.check_thingsboard              # config + auth + a read
    python -m scripts.check_thingsboard --config     # also validate device_groups.yaml
    python -m scripts.check_thingsboard --device <uuid>   # dump one device's keys

Run this FIRST whenever something looks wrong. It separates "the credentials are
bad" from "the URL is wrong" from "the YAML points at a device that no longer
exists", which otherwise all surface as the same empty report.

Every step prints the exact failure and stops, so the first red line is the cause.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from backend.app.config import settings
from backend.app.db.database import read_conn
from backend.app.services.thingsboard_client import ThingsBoardError, tb_client

OK, BAD, WARN = "  ✓", "  ✗", "  !"


def _mask(secret: str) -> str:
    return f"{secret[:2]}{'*' * 6}" if secret else "(empty)"


async def check_config() -> bool:
    print("1. Configuration (.env)")
    print(f"{OK} url      {settings.thingsboard_url}")
    print(f"{OK} api base {settings.tb_base}")
    if not settings.thingsboard_username or not settings.thingsboard_password:
        print(f"{BAD} credentials missing — copy .env.example to .env and fill them in")
        return False
    print(f"{OK} user     {settings.thingsboard_username}")
    print(f"{OK} password {_mask(settings.thingsboard_password)}")
    return True


async def check_auth() -> bool:
    print("\n2. Authentication  (POST /api/auth/login)")
    try:
        await tb_client._ensure_token()
    except ThingsBoardError as e:
        print(f"{BAD} login failed: {e}")
        print("     401 -> wrong username/password. Connection refused -> wrong URL or port.")
        return False
    except Exception as e:  # noqa: BLE001 — DNS, TLS, refused connections
        print(f"{BAD} could not reach the server: {e!r}")
        print(f"     Is {settings.thingsboard_url} right, and reachable from this machine?")
        return False

    print(f"{OK} got a JWT ({len(tb_client._token)} chars)")
    try:
        me = await tb_client._request("GET", "/auth/user")
        print(f"{OK} authenticated as {me.get('email')}  ({', '.join(me.get('authority', ''))[:40]})"
              if isinstance(me.get("authority"), list)
              else f"{OK} authenticated as {me.get('email')}  ({me.get('authority')})")
    except Exception as e:  # noqa: BLE001
        print(f"{WARN} token works but /auth/user failed: {e}")
    return True


async def check_read() -> bool:
    print("\n3. Reading data  (GET /api/tenant/devices)")
    try:
        data = await tb_client._request(
            "GET", "/tenant/devices",
            params={"pageSize": 5000, "page": 0, "sortProperty": "name", "sortOrder": "ASC"},
        )
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} could not list devices: {e}")
        return False
    devices = data.get("data", [])
    print(f"{OK} {len(devices)} devices visible to this account")
    if not devices:
        print(f"{WARN} none — is this account scoped to a customer with no devices?")
        return True

    # Read telemetry off the first device that actually has some.
    for d in devices[:25]:
        did = d["id"]["id"]
        keys = await tb_client.timeseries_keys(did)
        if keys:
            vals = await tb_client.latest_timeseries(did, keys[:3])
            print(f"{OK} telemetry read from {d.get('name')!r}:")
            for k, points in vals.items():
                if points:
                    ts = datetime.fromtimestamp(points[0]["ts"] / 1000, timezone.utc)
                    age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                    print(f"       {k} = {str(points[0]['value'])[:40]}  ({age:.1f}h old)")
            return True
    print(f"{WARN} no telemetry on the first 25 devices")
    return True


async def check_configured_devices() -> bool:
    """Validate device_groups.yaml against the live instance — the check that matters
    once you start adding sites. A stale UUID or a renamed key silently produces an
    empty report otherwise."""
    print("\n4. Configured devices  (device_groups.yaml -> ThingsBoard)")
    with read_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT g.name AS site, d.name AS device, d.tb_device_id, k.key_name
            FROM devices d
            JOIN device_groups g ON g.id = d.group_id
            LEFT JOIN device_keys k ON k.device_id = d.id AND k.role = 'status'
            ORDER BY g.sort_order, d.sort_order
            """
        )]
    if not rows:
        print(f"{WARN} no devices configured yet — run scripts.discover_device_groups")
        return True

    missing_id = [r for r in rows if not r["tb_device_id"]]
    if missing_id:
        print(f"{WARN} {len(missing_id)} device(s) have no tb_device_id, skipping them")

    live = [r for r in rows if r["tb_device_id"]]
    print(f"     checking {len(live)} device(s) with a ThingsBoard id...")
    bad = 0
    for r in live:
        try:
            keys = set(await tb_client.timeseries_keys(r["tb_device_id"]))
        except Exception as e:  # noqa: BLE001
            print(f"{BAD} {r['site']}/{r['device']}: id not found on the server ({e})")
            bad += 1
            continue
        if r["key_name"] not in keys:
            print(f"{BAD} {r['site']}/{r['device']}: status key {r['key_name']!r} "
                  f"not reported. Has: {', '.join(sorted(keys)[:6])}")
            bad += 1
    if bad:
        print(f"{BAD} {bad} of {len(live)} device(s) would produce no status")
        return False
    print(f"{OK} all {len(live)} device(s) resolve and report their status key")
    return True


async def dump_device(device_id: str) -> None:
    print(f"\nDevice {device_id}")
    dev = await tb_client._request("GET", f"/device/{device_id}")
    print(f"  name  {dev.get('name')}\n  type  {dev.get('type')}\n  label {dev.get('label')}")
    keys = await tb_client.timeseries_keys(device_id)
    print(f"  {len(keys)} timeseries key(s)")
    vals = await tb_client.latest_timeseries(device_id, keys) if keys else {}
    for k in sorted(vals):
        pts = vals[k]
        if pts:
            print(f"    {k} = {str(pts[0]['value'])[:60]}")


async def main(args) -> int:
    try:
        if args.device:
            await tb_client._ensure_token()
            await dump_device(args.device)
            return 0
        if not await check_config():
            return 1
        if not await check_auth():
            return 1
        ok = await check_read()
        if args.config:
            ok = await check_configured_devices() and ok
        print("\nAll good." if ok else "\nSomething above needs fixing.")
        return 0 if ok else 1
    finally:
        await tb_client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="store_true",
                    help="also validate device_groups.yaml against the live instance")
    ap.add_argument("--device", help="dump one device's keys and latest values")
    sys.exit(asyncio.run(main(ap.parse_args())))
