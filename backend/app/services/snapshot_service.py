"""Capture the latest value of every configured key for every device.

Runs at 23:59 (scheduled) and on demand from the "Import Data" button (manual).
One row per (capture_ts, device, key) in `snapshots`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings
from ..db.database import get_conn, read_conn
from . import ops
from .thingsboard_client import ThingsBoardError, tb_client

TZ = ZoneInfo(settings.timezone)
log = logging.getLogger("cranes.snapshot")


def _now() -> datetime:
    return datetime.now(TZ)


def _fetch_plan() -> tuple[list[dict], int]:
    """[{tb_device_id, keys: [(device_id, key_name)]}], plus the count of devices in it.

    Keys are grouped by the TB device that actually reports them, not by our device
    row: a site's status keys all live on its trigger device, so one site collapses
    into a single batched read instead of one call per sensor.
    """
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.id AS device_id, k.key_name,
                   COALESCE(k.tb_source_device_id, d.tb_device_id) AS source_id
            FROM devices d
            JOIN device_keys k ON k.device_id = d.id
            JOIN device_groups g ON g.id = d.group_id
            WHERE COALESCE(k.tb_source_device_id, d.tb_device_id) IS NOT NULL
            ORDER BY g.sort_order, d.sort_order, k.key_name
            """
        ).fetchall()

    by_source: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append((r["device_id"], r["key_name"]))
    # Devices we can actually read, not every row in `devices` — a seeded or retired
    # device with no TB source must not inflate "captured N devices".
    device_count = len({r["device_id"] for r in rows})
    return (
        [{"tb_device_id": src, "keys": pairs} for src, pairs in by_source.items()],
        device_count,
    )


async def _fetch_source(source: dict) -> list[tuple]:
    """Return rows (device_id, key_name, value, value_ts) for one TB device."""
    names = [k for _, k in source["keys"]]
    try:
        data = await tb_client.latest_timeseries(source["tb_device_id"], names)
    except ThingsBoardError:
        return [(dev_id, key, None, None) for dev_id, key in source["keys"]]
    out = []
    for dev_id, key in source["keys"]:
        points = data.get(key) or []
        if points:
            p = points[0]
            out.append((dev_id, key, str(p.get("value")), _ms_to_iso(p.get("ts"))))
        else:
            out.append((dev_id, key, None, None))
    return out


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat()


async def capture_snapshot(trigger: str = "manual", capture_date: str | None = None) -> dict:
    sources, device_count = _fetch_plan()
    ts = _now()
    capture_ts = ts.isoformat()
    capture_date = capture_date or ts.strftime("%Y-%m-%d")

    try:
        results = await asyncio.gather(*(_fetch_source(s) for s in sources))
        rows = [r for sub in results for r in sub]
        with get_conn() as conn:
            conn.executemany(
                """
                INSERT INTO snapshots
                    (capture_ts, capture_date, trigger, device_id, key_name, value, value_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capture_ts, device_id, key_name) DO UPDATE SET
                    value = excluded.value, value_ts = excluded.value_ts
                """,
                [(capture_ts, capture_date, trigger, d, k, v, vts) for (d, k, v, vts) in rows],
            )
        got = sum(1 for (_, _, v, _) in rows if v is not None)
        ops.record("capture", "success", trigger,
                   f"{capture_date}: {got}/{len(rows)} values, {device_count} devices")
        log.info("snapshot %s captured %d/%d values for %d devices from %d TB device(s) "
                 "(trigger=%s)", capture_date, got, len(rows), device_count,
                 len(sources), trigger)
    except Exception as e:  # noqa: BLE001
        ops.record("capture", "failed", trigger, f"{capture_date}: {e}")
        log.exception("snapshot capture failed for %s", capture_date)
        raise

    return {
        "capture_ts": capture_ts,
        "capture_date": capture_date,
        "trigger": trigger,
        "devices": device_count,
        "values": len(rows),
    }


def latest_snapshot_values(capture_date: str) -> list[dict]:
    """Most recent capture for a date, one row per device/key, for the dashboard."""
    with read_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """
                SELECT s.device_id, d.name AS device, g.name AS "group",
                       s.key_name, s.value, s.value_ts, s.capture_ts, s.trigger
                FROM snapshots s
                JOIN devices d ON d.id = s.device_id
                JOIN device_groups g ON g.id = d.group_id
                WHERE s.capture_date = ?
                  AND s.capture_ts = (
                      SELECT MAX(capture_ts) FROM snapshots WHERE capture_date = ?
                  )
                ORDER BY g.sort_order, d.sort_order, s.key_name
                """,
                (capture_date, capture_date),
            ).fetchall()
        ]
