# Cranes Daily Report

Localhost/LAN app that pulls device telemetry from ThingsBoard for 22
device/alarm/trigger groups, snapshots every key daily at 23:59 (plus on demand),
detects active↔inactive downtime windows, takes a site remark + per-device recommendations,
and renders a daily PDF into `reports/YYYY-MM-DD/`.

Architecture, schema and decisions live in **[CLAUDE.md](CLAUDE.md)** — read that first.

## Stack
FastAPI + Uvicorn · SQLite (WAL) · APScheduler · plain HTML/CSS/JS · Playwright
(HTML→PDF) · pdfplumber/pypdf (PDF import). One container, bind-mounted data.

## Layout
```
backend/app/
  main.py            FastAPI app + lifespan (init db, sync config, start scheduler)
  config.py          env-driven settings (.env)
  logging_setup.py   stdout + logs/scheduler.log
  db/                schema.sql (7 data tables + job_runs/imported_pdfs) + sqlite helpers
  services/
    thingsboard_client.py   async ThingsBoard PE REST client
    config_sync.py          device_groups.yaml -> DB
    seed_service.py         seed/sample_report_*.json -> DB (offline pipeline testing)
    snapshot_service.py     scheduled + manual snapshots
    downtime_service.py     status polling -> status_events, day summaries
    report_service.py       build context -> Jinja -> Playwright PDF
    pdf_import_service.py    archive + best-effort extract imported PDFs
    ops.py                  job_runs bookkeeping ("did last night work?")
    scheduler.py            23:59 nightly job + status poll interval
  api/               devices, captures, downtime, remarks, reports, imports, seed, status
backend/config/device_groups.yaml   the 22 groups (generate, don't hand-type)
seed/sample_report_20260816.json    approved-mockup data for offline dev
frontend/            index (dashboard) · site-detail · reports
templates/daily_report.html         report template (swap for approved v3)
tests/               pytest — run before calling a roadmap step done
scripts/
  discover_device_groups.py   walks ThingsBoard to build the yaml
  load_seed.py                load sample data (+ --report to render its PDF)
  backup.py                   dated zip of cranes.db + reports/ into backups/
logs/  backups/                bind-mounted, git-ignored
.claude/commands/    /add-device-group, /regen-report
.claude/agents/      debugger
```

## First run — offline (no ThingsBoard yet)

Per CLAUDE.md's guardrails, get the pipeline working against the sample data first.

```bash
cd "Cranes Daily Report"
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium          # one-time, for PDF rendering

python -m scripts.load_seed --report  # loads 6 sites / 42 devices, renders the PDF
python -m pytest -q                    # 15 tests

uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — the dashboard has a **Load sample data** button and a
banner showing last capture / last report / next nightly run. The generated PDF is
in `reports/2026-08-16/`.

## Going live with ThingsBoard

```bash
cp .env.example .env                  # fill in THINGSBOARD_URL / USERNAME / PASSWORD
python -m scripts.discover_device_groups --write
#   then review backend/config/device_groups.yaml:
#   set each device's `role: status` key, plus site_label / system_type.
#   restart, or: curl -X POST localhost:8000/api/config/reload
```

Confirm the PE REST paths in `thingsboard_client.py` against the live instance
(PE ≈ CE but not guaranteed identical).

## Docker

```bash
cp .env.example .env    # fill in
docker compose up --build
```
Bind mounts keep `backend/data/`, `reports/`, `uploads/`, `backend/config/`,
`logs/`, `backups/` as real editable files on the host. `TZ=Asia/Kuala_Lumpur`
is set so the 23:59 job fires at local time.

## Key endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | liveness + configured TB url |
| GET  | `/api/status/last-run` | last capture/report/poll + next scheduled runs (dashboard banner) |
| GET  | `/api/status/runs` | recent `job_runs` rows |
| GET  | `/api/groups`, `/api/groups/{id}/devices` | configured groups/devices |
| POST | `/api/config/reload` | re-sync `device_groups.yaml` into the DB |
| POST | `/api/seed/load` | load the sample data (offline dev) |
| GET  | `/api/devices/{id}/live` | pull current values straight from ThingsBoard |
| POST | `/api/captures/run?date=YYYY-MM-DD` | manual snapshot ("Import Data") |
| GET  | `/api/captures/{date}` | latest snapshot values for a date |
| POST | `/api/downtime/poll` | poll all device statuses now |
| GET  | `/api/downtime/groups/{id}?date=` | per-device day summary |
| GET/POST/PUT/DELETE | `/api/remarks` | site remarks (`device_id` null) + per-device recommendations (`device_id` set; POST upserts). `?scope=site\|device` filters |
| GET/POST/DELETE | `/api/followups` | retired — kept so existing PIC follow-up rows stay readable; nothing writes or renders them |
| POST | `/api/reports/{date}/generate` | (re)generate that day's PDF (409 if no snapshot; `?allow_empty=true` to override) |
| GET  | `/api/reports/{date}/preview` | render report HTML (no PDF) |
| GET  | `/api/reports/{date}/pdf` | download the PDF |
| POST | `/api/imports` (multipart `file`) | archive + extract an existing PDF |

Output per day: `reports/YYYY-MM-DD/Cranes_Daily_Report_YYYY-MM-DD.pdf`,
`report.html`, `snapshot_YYYY-MM-DD.json`.

## Missed 23:59 run (backup trigger)

The in-process APScheduler job has `misfire_grace_time=3600`. If the machine was
off at 23:59, add an OS-level cron / Task Scheduler entry as backup:

```
5 0 * * *  curl -X POST http://localhost:8000/api/captures/run \
        && curl -X POST "http://localhost:8000/api/reports/$(date -v-1d +\%F)/generate"
```

## Backups & retention

- `python -m scripts.backup` → dated zip of `cranes.db` + `reports/` in `backups/`.
  Copy it off the machine (cron it if you like). One file, one folder, no replication.
- Retention decided 2026-08-28: keep `reports/` folders and raw snapshots
  **indefinitely** — no prune job. `retention_days=0` in `config.py` is the switch.

## Still needs live wiring

1. Run `scripts/discover_device_groups.py` against the real ThingsBoard PE
   instance; review the generated yaml (esp. the `role: status` key per device).
2. Confirm the PE REST paths in `thingsboard_client.py`.
3. Replace `templates/daily_report.html` with the approved v3 template
   (keep the context variable names — see the comment at the top of the file).
