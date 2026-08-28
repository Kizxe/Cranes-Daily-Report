---
description: Add a new device group to device_groups.yaml the safe way — validates before touching the file
argument-hint: [group name]
---

Walk me through adding a new device group called "$ARGUMENTS" to backend/config/device_groups.yaml.

Ask me for: the ThingsBoard device IDs involved, each device's name and type, and which key on each device is the status key. Per CLAUDE.md, status always comes from the device's own reported status telemetry key — never derived from last-seen time.

The file's schema (see `backend/config/device_groups.yaml` and `backend/app/services/config_sync.py`): each group has `name`, `kind`, `tb_group_id`, `site_label`, `system_type`, `expected_report_hours`, and `devices[]`. Each device has `name`, `device_type`, `tb_device_id`, and `keys[]` — each key is `{ key_name, role, unit }` where `role` is one of `status` | `signal` | `metric`. Exactly one key per device must be `role: status`.

Before writing anything:
1. Check every tb_device_id looks like a real ThingsBoard UUID and isn't already used by another group in the file.
2. Confirm each device has exactly one key with `role: status`.
3. Show me the YAML block you're about to add and wait for me to confirm before writing it.

After writing, validate the whole file still parses (`python -c "import yaml,sys; yaml.safe_load(open('backend/config/device_groups.yaml'))"`) and, if the app is running, `POST /api/config/reload` to mirror it into the DB — so one typo doesn't take down the whole app.
