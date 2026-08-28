from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..db.database import read_conn
from ..services import config_sync
from ..services.thingsboard_client import ThingsBoardError, tb_client

router = APIRouter(tags=["devices"])


@router.get("/groups")
def list_groups() -> list[dict]:
    with read_conn() as conn:
        groups = [dict(r) for r in conn.execute(
            "SELECT * FROM device_groups ORDER BY sort_order, name"
        ).fetchall()]
        for g in groups:
            g["device_count"] = conn.execute(
                "SELECT COUNT(*) c FROM devices WHERE group_id = ?", (g["id"],)
            ).fetchone()["c"]
    return groups


@router.get("/groups/{group_id}/devices")
def group_devices(group_id: int) -> list[dict]:
    with read_conn() as conn:
        devices = [dict(r) for r in conn.execute(
            "SELECT * FROM devices WHERE group_id = ? ORDER BY sort_order, name", (group_id,)
        ).fetchall()]
        if not devices and not conn.execute(
            "SELECT 1 FROM device_groups WHERE id = ?", (group_id,)
        ).fetchone():
            raise HTTPException(404, "group not found")
        for d in devices:
            d["keys"] = [dict(k) for k in conn.execute(
                "SELECT key_name, role, unit FROM device_keys WHERE device_id = ?", (d["id"],)
            ).fetchall()]
    return devices


@router.post("/config/reload")
def reload_config() -> dict:
    """Re-read device_groups.yaml into the DB (upsert, no deletes)."""
    return config_sync.sync_from_yaml()


@router.get("/devices/{device_id}/live")
async def device_live(device_id: int) -> dict:
    """Pull the current values straight from ThingsBoard (dashboard 'prove the connection')."""
    with read_conn() as conn:
        dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if not dev:
            raise HTTPException(404, "device not found")
        keys = [r["key_name"] for r in conn.execute(
            "SELECT key_name FROM device_keys WHERE device_id = ?", (device_id,)
        ).fetchall()]
    if not dev["tb_device_id"]:
        raise HTTPException(409, "device has no tb_device_id — run the discover script")
    try:
        data = await tb_client.latest_timeseries(dev["tb_device_id"], keys)
    except ThingsBoardError as e:
        raise HTTPException(502, f"ThingsBoard: {e}") from e
    return {"device": dev["name"], "values": data}
