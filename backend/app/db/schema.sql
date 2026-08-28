-- Cranes Daily Report — SQLite schema (7 tables).
-- Single backend process is the only writer; WAL is set at connection time.
PRAGMA foreign_keys = ON;

-- 1. The 22-group config, mirrored from device_groups.yaml on startup ---------
CREATE TABLE IF NOT EXISTS device_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tb_group_id     TEXT UNIQUE,                 -- ThingsBoard entity-group UUID
    name            TEXT NOT NULL UNIQUE,        -- e.g. "Computime"
    kind            TEXT NOT NULL DEFAULT 'device',  -- device | alarm | trigger
    site_label      TEXT,                        -- "Factory · Penang, Malaysia"
    system_type     TEXT,                        -- "Chiller Optimization System (COpti)"
    expected_report_hours INTEGER DEFAULT 24,
    sort_order      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    tb_device_id    TEXT UNIQUE,                 -- ThingsBoard device UUID
    name            TEXT NOT NULL,               -- "CT_RHT_04"
    device_type     TEXT,                        -- "RHT" | "DPM" | "GW" ...
    label           TEXT,
    sort_order      INTEGER DEFAULT 0,
    UNIQUE (group_id, name)
);
CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(group_id);

CREATE TABLE IF NOT EXISTS device_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    key_name        TEXT NOT NULL,               -- telemetry/attribute key on TB
    role            TEXT NOT NULL DEFAULT 'metric',  -- status | metric | signal
    unit            TEXT,
    UNIQUE (device_id, key_name)
);
CREATE INDEX IF NOT EXISTS idx_device_keys_device ON device_keys(device_id);

-- 2. Daily (and on-demand) value snapshots -----------------------------------
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_ts      TEXT NOT NULL,               -- ISO8601, local tz
    capture_date    TEXT NOT NULL,               -- YYYY-MM-DD (report bucket)
    trigger         TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | manual
    device_id       INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    key_name        TEXT NOT NULL,
    value           TEXT,                        -- raw value as string
    value_ts        TEXT,                        -- TB's own timestamp for the value
    UNIQUE (capture_ts, device_id, key_name)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON snapshots(capture_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_device_date ON snapshots(device_id, capture_date);

-- 3. Downtime source of truth: one row per status change --------------------
CREATE TABLE IF NOT EXISTS status_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,               -- ACTIVE | INACTIVE | STATIC | STALLED ...
    start_ts        TEXT NOT NULL,               -- ISO8601 local
    end_ts          TEXT,                        -- NULL while ongoing
    duration_seconds INTEGER,                    -- filled when end_ts is set
    source          TEXT NOT NULL DEFAULT 'poll' -- poll | snapshot | manual
);
CREATE INDEX IF NOT EXISTS idx_status_events_device ON status_events(device_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_status_events_open ON status_events(device_id) WHERE end_ts IS NULL;

-- 4. Manual remarks (site-level) -------------------------------------------
CREATE TABLE IF NOT EXISTS remarks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    report_date     TEXT NOT NULL,               -- YYYY-MM-DD this remark belongs to
    body            TEXT NOT NULL,
    author          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_remarks_group_date ON remarks(group_id, report_date);

-- 5. PIC follow-up entries ------------------------------------------------
CREATE TABLE IF NOT EXISTS pic_followups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    report_date     TEXT NOT NULL,
    device_id       INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    issue           TEXT NOT NULL,               -- "CT_RHT_04 Inactive"
    remark          TEXT NOT NULL,
    assigned_pic    TEXT NOT NULL,
    date_assigned   TEXT NOT NULL,               -- YYYY-MM-DD
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pic_group_date ON pic_followups(group_id, report_date);

-- 6. reports table already covered by #7 numbering in CLAUDE.md; kept as-is --
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date     TEXT NOT NULL,
    doc_number      TEXT,                        -- "DDR-20260816"
    pdf_path        TEXT NOT NULL,
    snapshot_json_path TEXT,
    trigger         TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | manual
    status          TEXT NOT NULL DEFAULT 'generated',  -- generated | failed
    error           TEXT,
    generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (report_date, generated_at)
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date);

-- 7. imported PDFs (kept as-received) ------------------------------------
CREATE TABLE IF NOT EXISTS imported_pdfs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    original_name   TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    report_date     TEXT,                        -- parsed from the doc if possible
    doc_number      TEXT,
    extracted_json  TEXT,                        -- best-effort structured extract
    imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
