# Cranes Daily Report — Project Memory

Read this at the start of every session in this repo. It's the architecture plan agreed on before any code was written — follow it unless the user explicitly changes a decision below.

Full reference doc (diagrams, schema, API table, folder tree): https://claude.ai/code/artifact/e66b2af0-a138-43a6-b700-ae355671a0c7

## What this is
A localhost app that pulls device telemetry from ThingsBoard for 22 device/alarm/trigger groups, snapshots every key daily at 23:59 (plus on-demand via an "Import Data" button), detects active↔inactive downtime windows, takes a site remark plus a per-device engineer recommendation, and renders a daily PDF report (matching an already-approved design system: JetBrains Mono, Source Serif 4) saved into `reports/YYYY-MM-DD/` so it's just a folder someone can open.

> **Report colours come from the approved PDF, not from this file.** The sample uses
> navy `#0e1b36` (table headers only) and ink `#1a1a1a` — not the `#0E2340` this doc
> used to state. `templates/daily_report.html` carries the full measured palette; don't
> "correct" it back. The frontend keeps `#0E2340` — it's a separate surface.

Runs containerized on a local PC, reachable over the LAN (not just localhost on that machine).

## Stack (decided, don't relitigate without asking)
- Backend: Python, FastAPI + Uvicorn
- DB: SQLite, `PRAGMA journal_mode=WAL` — file-based is fine, single backend process is the only writer
- Scheduler: APScheduler in-process, cron/Task Scheduler entry as a backup trigger for the 23:59 job
- Frontend: plain HTML/CSS/JS, served as static files by the same FastAPI app
- PDF render: Playwright (Chromium) — HTML template → PDF
- PDF import: pdfplumber / pypdf
- Containerized: single Docker container (`mcr.microsoft.com/playwright/python:...` base — ships Chromium deps preinstalled)

## Folder structure
```
backend/{app/{api,services,models,db}, config/device_groups.yaml, data/cranes.db}
frontend/{index.html, site-detail.html, reports.html, assets/}
templates/daily_report.html      # approved v3 report template
reports/YYYY-MM-DD/              # one folder per day — PDF + snapshot JSON
uploads/pdfs/                    # imported PDFs, kept as-received
```

## Data model (7 tables)
`device_groups` → `devices` → `device_keys` (the 22-group config) · `snapshots` (value per key per capture, tagged scheduled/manual) · `status_events` (start_ts/end_ts/duration per status change — downtime source of truth) · `remarks` + `pic_followups` · `reports` (log of generated PDFs).

`remarks.device_id` is NULL for a site remark (many per day, the REMARK column on page 1)
and set for a device's engineer recommendation (at most one per day — a partial unique
index in `db/database.py::_migrate` enforces it). Active devices store nothing; the
report auto-fills `No action.` for them.

**`pic_followups` is retired** (2026-08-28) — the table and the `/api/followups` routes
remain so existing rows survive, but nothing writes or renders them. The per-device
recommendation replaced it.

## Confirmed decisions
- **Status source**: every device's status comes from the sensor's own reported status telemetry key, uniformly across all 22 groups — never derived from last-seen/heartbeat timing.
- **Downtime polling**: poll each device's status key every 5–15 min, write a `status_events` row only on change. Not worried about ThingsBoard rate limits — ThingsBoard's rule chain already computes status server-side, so this is just reading a settled key.
- **Report timing**: generated right at 23:59 off that snapshot, no built-in wait for late remarks. A remark added after 23:59 gets in via manually regenerating that day's report (`POST /api/reports/{date}/generate`), not by delaying the scheduled run.
- **ThingsBoard instance**: ThingsBoard PE, cloud-hosted — not on the same PC as this app. So no `host.docker.internal` / local Docker bridge needed, just outbound HTTPS. PE's REST API closely matches CE's but isn't guaranteed identical — confirm the exact base URL / login flow when writing `thingsboard_client.py`.
- **Docker**: bind-mount (not named volumes) for `reports/`, `uploads/`, `backend/data/`, and `backend/config/` so they're real, editable files on the host, not sealed inside the container. Bind the app to `0.0.0.0` so it's reachable over the LAN. Set `TZ=Asia/Kuala_Lumpur` explicitly — containers default to UTC and the 23:59 job would silently fire at the wrong time otherwise. `restart: unless-stopped`.

## Deferred (not blocking — don't ask again unless the user brings it up)
- Exact ThingsBoard access model (shared vs per-group login, tenant admin vs customer-scoped user) — user said they'll figure this out later.

## Still open — ask the user
- Retention policy for old `reports/` folders and raw snapshots (prune after N days, or keep indefinitely?).

## Build order
1. Draft `backend/config/device_groups.yaml` for all 22 groups by walking the ThingsBoard API — don't hand-transcribe the 22 lists.
2. ThingsBoard client + manual capture endpoint + `snapshots` table + a dashboard page showing live pulled values. Prove the connection before anything else.
3. Downtime detection: polling loop + `status_events` + a way to view a device's downtime list for a date.
4. Site remark + per-device engineer recommendation forms.
5. Report generation: wire real data into the existing approved template, Playwright render, dated folder output, scheduler.
6. PDF import (archive first; extraction quality can improve later).
7. Reports archive page + handling for a missed 23:59 run.
8. Containerize (Dockerfile + compose already drafted in this repo — see `Dockerfile` / `docker-compose.yml`).

Work through these roughly in order — step 1 unblocks real testing of everything after it.

## Working solo — guardrails for you, Claude
There's no second engineer reviewing this, so hold yourself to the checks a reviewer would normally catch:
- **Build against `seed/sample_report_20260816.json` before the ThingsBoard client exists.** It's the same data already validated in the approved report mockups (sites overview + a full Computime device breakdown + PIC follow-ups). Use it to get the DB schema, report template wiring, and Playwright render pipeline all working end-to-end (steps 2–5) without needing live ThingsBoard credentials for every test run. Swap in the real client last, once the pipeline around it already works.
- **Write a test alongside every new endpoint or service function**, not after. Nobody else will notice a silent regression later — pytest in a `tests/` folder, run before considering a roadmap step "done."
- **Log every capture and report run to a file** (`logs/scheduler.log` or similar), not just stdout — the 23:59 job runs unattended overnight, and a silent failure with no one watching is the main real risk of this whole design. Surface "last capture: success/failed, <timestamp>" somewhere visible on the dashboard so a glance in the morning tells you whether last night worked.
- **Commit after every roadmap step**, not at the end of a session — small, working checkpoints are what let you (or a debugger agent later) roll back to a known-good point instead of untangling several days of changes at once.
- **Back up `backend/data/cranes.db` and `reports/` periodically** — it's one file and one folder on one machine, no replication. A dated zip copied somewhere off that PC is enough; doesn't need to be fancy.
