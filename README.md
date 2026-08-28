# Cranes Daily Report

Localhost/LAN app that pulls device telemetry from ThingsBoard for 22
device/alarm/trigger groups, snapshots every key daily at 23:59 (plus on demand),
detects active↔inactive downtime windows, takes manual remarks + PIC follow-ups,
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
  db/                schema.sql (7 tables) + sqlite helpers
  services/
    thingsboard_client.py   async ThingsBoard PE REST client
    config_sync.py          device_groups.yaml -> DB
    snapshot_service.py     scheduled + manual snapshots
    downtime_service.py     status polling -> status_events, day summaries
    report_service.py       build context -> Jinja -> Playwright PDF
    pdf_import_service.py    archive + best-effort extract imported PDFs
    scheduler.py            23:59 nightly job + status poll interval
  api/               devices, captures, downtime, remarks, reports, imports
backend/config/device_groups.yaml   the 22 groups (generate, don't hand-type)
frontend/            index (dashboard) · site-detail · reports
templates/daily_report.html         report template (swap for approved v3)
scripts/discover_device_groups.py   walks ThingsBoard to build the yaml
```

## First run (local, no Docker)

```bash
cd "Cranes Daily Report"
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium          # one-time, for PDF rendering

cp .env.example .env                 # fill in THINGSBOARD_URL / USERNAME / PASSWORD

# Build order step 1 — generate the group config from ThingsBoard:
python -m scripts.discover_device_groups --write
#   then review backend/config/device_groups.yaml:
#   set each device's `role: status` key, site_label, system_type.

uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — dashboard. `POST /api/config/reload` re-reads the yaml.

## Docker

```bash
cp .env.example .env    # fill in
docker compose up --build
```
Bind mounts keep `backend/data/`, `reports/`, `uploads/`, `backend/config/` as
real editable files on the host. `TZ=Asia/Kuala_Lumpur` is set so the 23:59 job
fires at local time.

## Key endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | liveness + configured TB url |
| GET  | `/api/groups`, `/api/groups/{id}/devices` | configured groups/devices |
| POST | `/api/config/reload` | re-sync `device_groups.yaml` into the DB |
| GET  | `/api/devices/{id}/live` | pull current values straight from ThingsBoard |
| POST | `/api/captures/run?date=YYYY-MM-DD` | manual snapshot ("Import Data") |
| GET  | `/api/captures/{date}` | latest snapshot values for a date |
| POST | `/api/downtime/poll` | poll all device statuses now |
| GET  | `/api/downtime/groups/{id}?date=` | per-device day summary |
| GET/POST/PUT/DELETE | `/api/remarks`, `/api/followups` | remarks + PIC follow-ups |
| POST | `/api/reports/{date}/generate` | (re)generate that day's PDF |
| GET  | `/api/reports/{date}/preview` | render report HTML (no PDF) |
| GET  | `/api/reports/{date}/pdf` | download the PDF |
| POST | `/api/imports` (multipart `file`) | archive + extract an existing PDF |

## Missed 23:59 run (backup trigger)

The in-process APScheduler job has `misfire_grace_time=3600`. If the machine was
off at 23:59, add an OS-level cron / Task Scheduler entry as backup:

```
5 0 * * *  curl -X POST http://localhost:8000/api/captures/run \
        && curl -X POST http://localhost:8000/api/reports/$(date -d yesterday +\%F)/generate
```

## Retention

Decided 2026-08-28: keep `reports/` folders and raw snapshots **indefinitely** —
no prune job. `retention_days=0` in `config.py` is the off switch if that changes.

## Status / build order

Scaffolding for all of CLAUDE.md's build order is in place and imports cleanly.
Still needs live wiring:
1. Run the discover script against the real ThingsBoard PE instance and review
   the generated `device_groups.yaml` (esp. the `role: status` key per device).
2. Confirm the PE REST paths in `thingsboard_client.py` against the live instance.
3. Replace `templates/daily_report.html` with the approved v3 template
   (keep the context variable names — see the comment at the top of the file).
