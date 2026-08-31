# Cranes Daily Report — Project Memory

Read this at the start of every session in this repo. It's the architecture plan agreed on before any code was written — follow it unless the user explicitly changes a decision below.

Full reference doc (diagrams, schema, API table, folder tree): https://claude.ai/code/artifact/e66b2af0-a138-43a6-b700-ae355671a0c7

How the built system actually fits together — directory map, the trigger-device data model,
where each report number comes from: `docs/ARCHITECTURE.md`
(shareable page: https://claude.ai/code/artifact/9cb3f082-1fa1-44e8-a76e-c5b32e9d30cb)

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
backend/{app/{api,services,models,db}, config/sites/<site>.yaml, config/device_groups.yaml, data/cranes.db}
frontend/{index.html, site-detail.html, reports.html, assets/}
templates/daily_report.html      # approved v3 report template
reports/YYYY-MM-DD/              # one folder per day — PDF + snapshot JSON
uploads/pdfs/                    # imported PDFs, kept as-received
```

## Data model (7 tables)
`device_groups` → `devices` → `device_keys` (the 22-group config; `device_keys.tb_source_device_id` = which TB device reports the key, i.e. the site's trigger device, NULL = the device's own `tb_device_id`) · `snapshots` (value per key per capture, tagged scheduled/manual) · `status_events` (start_ts/end_ts/duration per status change — downtime source of truth) · `remarks` · `reports` (log of generated PDFs) · `job_runs` · `imported_pdfs`.

`remarks.device_id` is NULL for a site remark (many per day, the REMARK column on page 1)
and set for a device's engineer recommendation (at most one per day — a partial unique
index in `db/database.py::_migrate` enforces it). Active devices store nothing; the
report auto-fills `No action.` for them.

**`pic_followups` was removed** (2026-08-31). It was retired on 2026-08-28 when the
per-device recommendation replaced it; the table was empty, so the dead routes, schemas
and table definition are gone. An existing database keeps its empty table — nothing
reads it.

## Confirmed decisions
- **Status source** (revised 2026-08-31 against the live instance — supersedes "the sensor's
  own status key"): status is **not** on the sensor's device. Each site has ONE **trigger
  device** (`NumedTrigger`, `ComputimeTrigger`, `BSC Trigger`, ... — 27 of them, `--list-triggers`)
  whose rule chain computes status for every sensor at that site and writes it back as one key
  per sensor per family:
  `active_<sensor>` (bool — the roster, every sensor has one) · `deviceStatus_<sensor>`
  (ACTIVE | STATIC | NO DATA | ... — only ~2/3 of sensors) · `deviceSeverity_<sensor>`
  (OK | NON-CRITICAL | CRITICAL, maps 1:1 to the report's pills) · `activeTs_` / `InactiveTs_<sensor>`
  (epoch ms of the last transition) · `IssueOcc_<sensor>` and `<sensor> 1D` (issue counts,
  to-date and today) · `ActiveInActive_<sensor>` (`["A_<ts>","N_hh:mm:ss", activeMs, inactiveMs]`).
  Still never derived from last-seen/heartbeat timing.
  `role: status` = `deviceStatus_` when the sensor has one, else `active_` normalised
  true/false -> ACTIVE/INACTIVE (`downtime_service.normalize_status`).
- **ACTIVE HRS / AFFECTED HRS come from `forTotalUse_<sensor>`** = `[inactive_ms, active_ms]`,
  the pair the ThingsBoard "Total Active / Total Inactive Time" table shows and the same one
  carried in `ActiveInActive_`. It is cumulative (4455h on one sensor), so `day_summary`
  differences it against the previous day's capture — TB's own accounting, scoped to the day.
  `status_events` is the fallback only when there is no baseline to difference.
  `scripts/backfill_counters.py --date <yesterday>` recovers a missing baseline from TB history.
  **ISSUE OCC. is `<sensor> 1D`**, the trigger's daily fault count (resets at midnight; matches
  the Fault Counter device's `<Site>_device_fault_data` breakdown device for device).
- **The Fault Counter device** `71af5410-df0d-11f0-a0b2-1366f3bd8252` holds per-site daily fault
  totals (`Numed_fault_counter_all/dpm/rht/rtd/ufm`, `Numed_top_fault_device`, `_category`,
  `Numed_device_fault_data`). No timestamps, so it CANNOT fill the DOWNTIME EVENTS table —
  that still needs `status_events` from the poll loop.
- **Don't use the trigger's `Active Device` / `Inactive Device` counters.** The rule chain
  computes them over its own subset (19+4 against a 34-sensor roster on NUMed) and they drift
  minute to minute. Report counts are derived from our own roster instead.
- **Downtime polling**: poll each device's status key every 5–15 min, write a `status_events` row only on change. Not worried about ThingsBoard rate limits — ThingsBoard's rule chain already computes status server-side, so this is just reading a settled key. One batched read per *trigger* device covers a whole site, and an event is dated from `activeTs_`/`InactiveTs_` rather than poll time, so the poll interval no longer rounds off downtime windows.
- **Report timing**: generated right at 23:59 off that snapshot, no built-in wait for late remarks. A remark added after 23:59 gets in via manually regenerating that day's report (`POST /api/reports/{date}/generate`), not by delaying the scheduled run.
- **ThingsBoard instance**: ThingsBoard PE, cloud-hosted — not on the same PC as this app. So no `host.docker.internal` / local Docker bridge needed, just outbound HTTPS. PE's REST API closely matches CE's but isn't guaranteed identical — confirm the exact base URL / login flow when writing `thingsboard_client.py`.
- **Docker**: bind-mount (not named volumes) for `reports/`, `uploads/`, `backend/data/`, and `backend/config/` so they're real, editable files on the host, not sealed inside the container. Bind the app to `0.0.0.0` so it's reachable over the LAN. Set `TZ=Asia/Kuala_Lumpur` explicitly — containers default to UTC and the 23:59 job would silently fire at the wrong time otherwise. `restart: unless-stopped`.

## Deferred (not blocking — don't ask again unless the user brings it up)
- Exact ThingsBoard access model (shared vs per-group login, tenant admin vs customer-scoped user) — user said they'll figure this out later.

## Still open — ask the user
- Retention policy for old `reports/` folders and raw snapshots (prune after N days, or keep indefinitely?).

## Build order
1. Draft the site config for all 22 groups by walking the ThingsBoard API — don't hand-transcribe the 22 lists. **One file per site**, `backend/config/sites/<site>.yaml`; `device_groups.yaml` now holds only shared defaults. One site per run, so a re-run can't disturb the other 21:
   `python -m scripts.discover_device_groups --trigger NumedTrigger --name NUMed --write`
   (`--list-triggers` first). **NUMed done 2026-08-31** — 33 sensors, `Robert Bosch Recovery`
   excluded as another site's. Still to review there: `Numed`, `Numed Setpoints`,
   `Numed Flowmeter`, `Numed Recovery`, `Meatrol DPM` look like system flags rather than
   devices — drop with `--exclude` if so. 21 sites to go.
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
- **The sample-data loader is gone** (removed 2026-08-31). It had served its purpose — the live pipeline works — and it kept being re-run by accident, landing six fake sites in real reports. Tests build the site they need with `tests/factories.py::make_site()` instead, which is clearer than cross-referencing a fixture file.
- **Write a test alongside every new endpoint or service function**, not after. Nobody else will notice a silent regression later — pytest in a `tests/` folder, run before considering a roadmap step "done."
- **Log every capture and report run to a file** (`logs/scheduler.log` or similar), not just stdout — the 23:59 job runs unattended overnight, and a silent failure with no one watching is the main real risk of this whole design. Surface "last capture: success/failed, <timestamp>" somewhere visible on the dashboard so a glance in the morning tells you whether last night worked.
- **Commit after every roadmap step**, not at the end of a session — small, working checkpoints are what let you (or a debugger agent later) roll back to a known-good point instead of untangling several days of changes at once.
- **Back up `backend/data/cranes.db` and `reports/` periodically** — it's one file and one folder on one machine, no replication. A dated zip copied somewhere off that PC is enough; doesn't need to be fancy.
