# Cranes Daily Report

Localhost/LAN app that pulls device telemetry from ThingsBoard for 22
device/alarm/trigger groups, snapshots every key daily at 23:59 (plus on demand),
detects active↔inactive downtime windows, takes a site remark + per-device recommendations,
and renders a daily PDF into `reports/YYYY-MM-DD/`.

**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** explains how the whole system fits together —
the ThingsBoard trigger-device model, the nine tables, and where every number on the PDF comes
from. Start there if you're new to the codebase. **[CLAUDE.md](CLAUDE.md)** records the decisions
and why they were made. This file is the runbook: how to actually drive it.

## Stack
FastAPI + Uvicorn · SQLite (WAL) · APScheduler · plain HTML/CSS/JS · Playwright
(HTML→PDF) · pdfplumber/pypdf (PDF import). One container, bind-mounted data.

## Layout
```
backend/app/
  main.py            FastAPI app + lifespan (init db, sync config, start scheduler)
  config.py          env-driven settings (.env)
  logging_setup.py   stdout + logs/scheduler.log
  db/                schema.sql (9 tables) + sqlite helpers
  services/
    thingsboard_client.py   async ThingsBoard PE REST client
    config_sync.py          sites/*.yaml -> DB
    snapshot_service.py     scheduled + manual snapshots
    downtime_service.py     status polling -> status_events, day summaries
    report_service.py       build context -> Jinja -> Playwright PDF
    pdf_import_service.py    archive + best-effort extract imported PDFs
    ops.py                  job_runs bookkeeping ("did last night work?")
    scheduler.py            23:59 nightly job + status poll interval
  api/               devices, captures, downtime, remarks, reports, imports, status
backend/config/sites/<site>.yaml     ONE FILE PER SITE (generate, don't hand-type)
backend/config/device_groups.yaml    shared defaults only
frontend/            index (dashboard) · site-detail · reports
templates/daily_report.html         report template (swap for approved v3)
tests/               pytest — run before calling a roadmap step done
scripts/
  discover_device_groups.py   walks a site's trigger device -> sites/<site>.yaml
  inspect_db.py               read-only look inside cranes.db
  backfill_counters.py        recover a past day's values from ThingsBoard history
  backfill_downtime.py        rebuild a day's status_events from TB history (--refresh)
  prune_devices.py            delete devices the site YAML no longer lists
  backup.py                   dated zip of cranes.db + reports/ into backups/
logs/  backups/                bind-mounted, git-ignored
.claude/commands/    /add-device-group, /regen-report
.claude/agents/      debugger
```

## Running it — the daily walkthrough

Everything below is run from the repo root with the venv active:

```bash
cd /Users/irfan/CDR
source .venv/bin/activate            # then plain `python`, `uvicorn`, `pytest` work
```

### 1. Start the app

```bash
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

On startup it creates the DB if missing, mirrors `backend/config/sites/*.yaml` into it,
and starts two scheduled jobs: the **23:59 nightly** (capture + report) and a **status
poll every 5 min** (plus a one-off catch-up poll ~5 s after boot, so a restart leaves
no blind spot). Watch for this line — it tells you the config actually loaded:

```
INFO cranes: config sync: {'groups': 1, 'devices': 33, 'keys': 248}
```

Open **http://localhost:8000** (or `http://<this-pc-ip>:8000` from another machine on
the LAN). Leave it running; open a second terminal for everything below.

### 1b. Stopping and restarting

If the app is running in a terminal you can see, **Ctrl-C** stops it. Otherwise:

```bash
pgrep -fl "uvicorn backend.app.main"     # is it running? prints the PID if so
pkill -f "uvicorn backend.app.main"      # stop it
```

To restart, stop it and start it again — there is no reload command. `--reload` only
picks up *Python* edits; a change to `.env` or to `config.py` defaults needs a full
restart, and so does anything that touches the scheduler.

```bash
pkill -f "uvicorn backend.app.main"; sleep 2
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

Run it detached, so it survives closing the terminal:

```bash
nohup uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 > logs/uvicorn.out 2>&1 &
tail -f logs/uvicorn.out                 # watch it boot; Ctrl-C just stops tailing
```

Confirm it came back up — all three should answer:

```bash
curl -s localhost:8000/api/health
curl -s localhost:8000/api/status/last-run | python -m json.tool | head -20
python -m scripts.inspect_db
```

**A restart is not free.** Downtime is only recorded while the process is up, so a
status change during the gap is missed. The boot catch-up poll narrows it to seconds,
and the hourly reconcile pass rebuilds today from ThingsBoard history anyway; for a
longer outage rebuild that day with `scripts/backfill_downtime.py`. On the
always-on PC, run it under `docker compose` with `restart: unless-stopped` rather than
by hand.

**Port already in use?** Something is still bound to 8000:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN       # what's holding it
```

### 2. Add a site

One file per site in `backend/config/sites/`. Generate it, don't hand-type it:

```bash
python -m scripts.discover_device_groups --list-triggers          # 27 sites available
python -m scripts.discover_device_groups --trigger ComputimeTrigger --name Computime --write
```

That writes `backend/config/sites/computime.yaml` and touches no other site. Then open
it and fill in the two things ThingsBoard doesn't know — `site_label` and `system_type`
(they print on the site's report page) — plus `sort_order` if you want it higher in the
list. A later re-run preserves all three.

Drop sensors that aren't really devices, and re-run:

```bash
python -m scripts.discover_device_groups --trigger ComputimeTrigger --name Computime \
    --exclude "Some Flag,Another Flag" --write
```

Load the change without restarting:

```bash
curl -X POST localhost:8000/api/config/reload
```

### 3. Pull data and check it

```bash
curl -X POST localhost:8000/api/captures/run     # snapshot every key now ("Import Data")
curl -X POST localhost:8000/api/downtime/poll    # read statuses, write status_events
```

The nightly job does both automatically; these are for when you want to see it now.

### 4. Look inside the database

`cranes.db` is one SQLite file. Read it without SQL:

```bash
python -m scripts.inspect_db                    # sites, captures, job runs, reports, remarks
python -m scripts.inspect_db --site NUMed       # every device: status, active/affected hrs
python -m scripts.inspect_db --device 295       # one device: its keys, values, downtime events
python -m scripts.inspect_db --date 2026-08-30  # any of the above, for another day
python -m scripts.inspect_db --sql "SELECT name, device_type FROM devices LIMIT 5"
```

It is read-only and safe to run while the server is up. Start with the plain command:
the **JOB RUNS** block is the "did last night actually work?" answer, and **CAPTURES**
shows how many values came back (`215/215` = every key answered).

For raw SQL: `sqlite3 backend/data/cranes.db` (`.tables`, `.schema devices`, `.quit`).
The 7 tables are documented in `backend/app/db/schema.sql`.

### 5. Write the remarks

The report has two kinds, both enterable from the site page in the browser:

- **Site remark** — the REMARK column on page 1. Many per day.
- **Engineer recommendation** — one per device per day. Devices left blank print
  `No action.` automatically, so you only fill in the ones with a problem.

```bash
curl -X POST localhost:8000/api/remarks -H 'content-type: application/json' \
  -d '{"group_id":43,"report_date":"2026-08-31","body":"Check the Level 2 gateway."}'
```

Add `"device_id": 295` to make it that device's recommendation instead.

### 6. Generate and read the report

```bash
curl -X POST localhost:8000/api/reports/2026-08-31/generate
open reports/2026-08-31/Cranes_Daily_Report_2026-08-31.pdf
```

Each day gets a folder: the PDF, the `report.html` it was rendered from, and
`snapshot_YYYY-MM-DD.json` (the raw values it was built from — the audit trail).

To iterate on layout without re-rendering a PDF, open
`http://localhost:8000/api/reports/2026-08-31/preview` in the browser.

A remark added after 23:59 doesn't need a new capture — just regenerate that day:
`curl -X POST localhost:8000/api/reports/2026-08-31/generate` (or `/regen-report`).

### 7. Every morning

```bash
python -m scripts.inspect_db     # JOB RUNS: capture + report both 'ok' for last night?
```

Or glance at the dashboard banner, which shows the same thing. If the machine was off
at 23:59, see *Missed 23:59 run* below.

## Tests

```bash
pytest -q          # 49 tests
```

Run before calling a roadmap step done. Tests use a throwaway DB and their own empty
config, so they never touch `cranes.db` or `backend/config/`.

## First-time setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium          # one-time, for PDF rendering
cp .env.example .env                 # THINGSBOARD_URL / USERNAME / PASSWORD
```

> If you move or rename the repo folder, the venv's scripts keep pointing at the old
> path and `uvicorn: no such file` appears. Recreate it, or `sed -i '' 's|<old>|<new>|g'`
> across `.venv/bin/*` and `.venv/pyvenv.cfg`.

## Docker

```bash
cp .env.example .env    # fill in
docker compose up --build
```
Bind mounts keep `backend/data/`, `reports/`, `uploads/`, `backend/config/`,
`logs/`, `backups/` as real editable files on the host. `TZ=Asia/Kuala_Lumpur`
is set so the 23:59 job fires at local time.

**Run it 24/7.** Downtime is recorded only while the process is up — the poll loop
runs every 5 min, but nothing catches a status change that happens while the
container is stopped. `docker compose` sets `restart: unless-stopped`; keep it on the
always-on PC. For any stretch it *was* down, rebuild that day afterwards:

```bash
python -m scripts.backfill_downtime --date 2026-08-30 --dry-run   # then drop --dry-run
python -m scripts.backfill_downtime --date 2026-08-30 --site NUMed
```

It reads each device's status-key history from ThingsBoard and writes one
`status_events` row per transition, tagged `source='backfill'`. It skips any device
that already has events that day, so a real poll-recorded day is never overwritten
and re-running is safe.

**The poll misses short flaps — that is what the reconcile pass is for.** Polling every
5 min only samples; a device that dropped and recovered in between left no trace. Every
hour (and once at the top of the 23:59 job) the backend replaces today's events with
every transition ThingsBoard's history holds, so ISSUE OCC. and the DOWNTIME EVENTS
table match what the instance actually recorded. Set `RECONCILE_MINUTES=0` to turn it
off; run it by hand for any day with:

```bash
curl -s -X POST "localhost:8000/api/downtime/reconcile?date=2026-09-01"
python -m scripts.backfill_downtime --date 2026-09-01 --refresh --dry-run
```

Sensors chatter, so windows shorter than `MIN_EVENT_SECONDS` (default 120) are folded
into the window they interrupted — NUMed sees ~490 raw transitions a day with a median
length of 38s, and roughly 105 real windows once debounced. Regenerate the report afterwards
(`POST /api/reports/{date}/generate`).

## Key endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/health` | liveness + configured TB url |
| GET  | `/api/status/last-run` | last capture/report/poll + next scheduled runs (dashboard banner) |
| GET  | `/api/status/runs` | recent `job_runs` rows |
| GET  | `/api/groups`, `/api/groups/{id}/devices` | configured groups/devices |
| POST | `/api/config/reload` | re-sync `backend/config/sites/*.yaml` into the DB |
| GET  | `/api/devices/{id}/live` | pull current values straight from ThingsBoard |
| POST | `/api/captures/run?date=YYYY-MM-DD` | manual snapshot ("Import Data") |
| GET  | `/api/captures/{date}` | latest snapshot values for a date |
| POST | `/api/downtime/poll` | poll all device statuses now |
| GET  | `/api/downtime/groups/{id}?date=` | per-device day summary (`source`: counter \| events) |
| GET/POST/PUT/DELETE | `/api/remarks` | site remarks (`device_id` null) + per-device recommendations (`device_id` set; POST upserts). `?scope=site\|device` filters |
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

1. 21 of the 22 sites — NUMed is done. One `discover_device_groups` run each.
2. Five NUMed sensors (DPM CH1/CH2, RTD CH1/CH2, UFM) have `forTotalUse_` frozen at
   `[0, 0]` on NumedTrigger, so they print 0.0 active / 0.0 affected even while
   reporting INACTIVE or NO DATA. The ThingsBoard dashboard shows the same zeros —
   the fix belongs in that rule chain, not here.
3. `NumedmostInactiveDevice` and `NumedtimeInHours` on the Fault Counter device are
   empty (`None` / `0`); they look purpose-built for the downtime summary.
