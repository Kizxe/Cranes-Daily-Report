"""Capture the latest value of every configured key for every device.

Runs at 23:59 (scheduled) and on demand from the "Import Data" button (manual).
One row per (capture_ts, device, key) in `snapshots`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import settings
from ..db.database import get_conn, read_conn
from .thingsboard_client import ThingsBoardError, tb_client

TZ = ZoneInfo(settings.timezone)


def _now() -> datetime:
    return datetime.now(TZ)


def _configured_devices() -> list[dict]:
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.id AS device_id, d.name, d.tb_device_id,
                   g.name AS group_name
            FROM devices d
            JOIN device_groups g ON g.id = d.group_id
            ORDER BY g.sort_order, d.sort_order
            """
        ).fetchall()
        keys = conn.execute(
            "SELECT device_id, key_name FROM device_keys"
        ).fetchall()
    by_device: dict[int, list[str]] = {}
    for k in keys:
        by_device.setdefault(k["device_id"], []).append(k["key_name"])
    return [
        {**dict(r), "keys": by_device.get(r["device_id"], [])}
        for r in rows
    ]


async def _fetch_one(dev: dict) -> list[tuple]:
    """Return rows (device_id, key_name, value, value_ts) for one device."""
    if not dev["tb_device_id"] or not dev["keys"]:
        return []
    try:
        data = await tb_client.latest_timeseries(dev["tb_device_id"], dev["keys"])
    except ThingsBoardError:
        return [(dev["device_id"], k, None, None) for k in dev["keys"]]
    out = []
    for key in dev["keys"]:
        points = data.get(key) or []
        if points:
            p = points[0]
            out.append((dev["device_id"], key, str(p.get("value")), _ms_to_iso(p.get("ts"))))
        else:
            out.append((dev["device_id"], key, None, None))
    return out


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, TZ).isoformat()


async def capture_snapshot(trigger: str = "manual", capture_date: str | None = None) -> dict:
    devices = _configured_devices()
    ts = _now()
    capture_ts = ts.isoformat()
    capture_date = capture_date or ts.strftime("%Y-%m-%d")

    results = await asyncio.gather(*(_fetch_one(d) for d in devices))
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
            [
                (capture_ts, capture_date, trigger, d, k, v, vts)
                for (d, k, v, vts) in rows
            ],
        )
    return {
        "capture_ts": capture_ts,
        "capture_date": capture_date,
        "trigger": trigger,
        "devices": len(devices),
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
