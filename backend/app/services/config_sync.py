"""Load device_groups.yaml and mirror it into device_groups / devices / device_keys.

The YAML file is the source of truth for config. This runs on startup and via
POST /api/config/reload. It upserts (never deletes) so a stale DB row from a
removed device stays until cleaned manually — safer for an audit tool.
"""
from __future__ import annotations

import yaml

from ..config import settings
from ..db.database import get_conn


def load_yaml() -> dict:
    if not settings.device_groups_config.exists():
        return {"groups": [], "status_key_default": "status"}
    return yaml.safe_load(settings.device_groups_config.read_text()) or {}


def _source_device(key: dict, trigger_id: str | None) -> str | None:
    """Which TB device reports this key. NULL = the device's own tb_device_id.

    `source: trigger` means the key lives on the site's trigger device — that is where
    the rule chain writes active_/deviceStatus_/... for every sensor at the site. An
    explicit `tb_source_device_id` on the key wins over both.
    """
    if key.get("tb_source_device_id"):
        return key["tb_source_device_id"]
    if key.get("source") == "trigger":
        return trigger_id
    return None


def sync_from_yaml() -> dict:
    cfg = load_yaml()
    groups = cfg.get("groups", []) or []
    status_default = cfg.get("status_key_default", "status")

    counts = {"groups": 0, "devices": 0, "keys": 0}
    with get_conn() as conn:
        for sort_g, g in enumerate(groups):
            conn.execute(
                """
                INSERT INTO device_groups
                    (tb_group_id, name, kind, site_label, system_type,
                     expected_report_hours, sort_order)
                VALUES (:tb_group_id, :name, :kind, :site_label, :system_type,
                        :hours, :sort_order)
                ON CONFLICT(name) DO UPDATE SET
                    tb_group_id = excluded.tb_group_id,
                    kind = excluded.kind,
                    site_label = excluded.site_label,
                    system_type = excluded.system_type,
                    expected_report_hours = excluded.expected_report_hours,
                    sort_order = excluded.sort_order
                """,
                {
                    "tb_group_id": g.get("tb_group_id"),
                    "name": g["name"],
                    "kind": g.get("kind", "device"),
                    "site_label": g.get("site_label"),
                    "system_type": g.get("system_type"),
                    "hours": g.get("expected_report_hours", 24),
                    "sort_order": sort_g,
                },
            )
            group_id = conn.execute(
                "SELECT id FROM device_groups WHERE name = ?", (g["name"],)
            ).fetchone()["id"]
            counts["groups"] += 1
            trigger_id = (g.get("trigger") or {}).get("tb_device_id")

            for sort_d, d in enumerate(g.get("devices", []) or []):
                conn.execute(
                    """
                    INSERT INTO devices
                        (group_id, tb_device_id, name, device_type, label, sort_order)
                    VALUES (:group_id, :tb_device_id, :name, :device_type, :label, :sort_order)
                    ON CONFLICT(group_id, name) DO UPDATE SET
                        tb_device_id = excluded.tb_device_id,
                        device_type = excluded.device_type,
                        label = excluded.label,
                        sort_order = excluded.sort_order
                    """,
                    {
                        "group_id": group_id,
                        "tb_device_id": d.get("tb_device_id"),
                        "name": d["name"],
                        "device_type": d.get("device_type"),
                        "label": d.get("label"),
                        "sort_order": sort_d,
                    },
                )
                device_id = conn.execute(
                    "SELECT id FROM devices WHERE group_id = ? AND name = ?",
                    (group_id, d["name"]),
                ).fetchone()["id"]
                counts["devices"] += 1

                keys = d.get("keys") or [{"key_name": status_default, "role": "status"}]
                has_status = any(k.get("role") == "status" for k in keys)
                if not has_status:
                    keys = [{"key_name": status_default, "role": "status"}, *keys]

                for k in keys:
                    conn.execute(
                        """
                        INSERT INTO device_keys
                            (device_id, key_name, role, unit, tb_source_device_id)
                        VALUES (:device_id, :key_name, :role, :unit, :source)
                        ON CONFLICT(device_id, key_name) DO UPDATE SET
                            role = excluded.role,
                            unit = excluded.unit,
                            tb_source_device_id = excluded.tb_source_device_id
                        """,
                        {
                            "device_id": device_id,
                            "key_name": k["key_name"],
                            "role": k.get("role", "metric"),
                            "unit": k.get("unit"),
                            "source": _source_device(k, trigger_id),
                        },
                    )
                    counts["keys"] += 1
    return counts
