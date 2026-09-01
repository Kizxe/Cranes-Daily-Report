# Cranes Daily Report — System Architecture

> Written for the next person to work on this. Read `CLAUDE.md` for the decisions
> and *why* they were made; this document covers *what exists and how it fits together*.
>
> Last verified against the code on **2026-08-31** (49 tests passing, 1 of 22 sites live).

---

## 1. What the system does

Every night at **23:59** it reads device telemetry out of ThingsBoard for a set of
monitored sites, works out how long each device was up and down that day, folds in the
engineer's written remarks, and renders a client-facing **PDF into `reports/YYYY-MM-DD/`**.

It runs on one PC on the local network. There is no cloud component of its own — it is a
single Python process with a single SQLite file, reachable at `http://<that-pc>:8000`
from any machine on the LAN.

```
ThingsBoard PE  ──HTTPS──▶  FastAPI process  ──▶  SQLite (cranes.db)
  (cloud)                    · scheduler                │
                             · REST API                 ▼
                             · static frontend    reports/2026-08-31/
                                                    ├── Cranes_Daily_Report_2026-08-31.pdf
                                                    ├── report.html
                                                    └── snapshot_2026-08-31.json
```

**Three ways work enters the system:** the 23:59 scheduled job, the "Import Data" button
(a manual capture), and a human typing remarks into the web page.

---

## 2. Stack

| Layer | Choice | Note |
|---|---|---|
| Web framework | FastAPI + Uvicorn | one process serves API *and* the static frontend |
| Database | SQLite, WAL mode | one file; the backend process is the only writer |
| Scheduler | APScheduler (in-process) | plus an OS cron entry as a backup trigger |
| Frontend | plain HTML/CSS/JS | no build step, no framework |
| PDF | Playwright (Chromium) | HTML template → PDF |
| PDF import | pdfplumber / pypdf | archive first, extract best-effort |
| Packaging | one Docker container | `mcr.microsoft.com/playwright/python` base |

Python 3.12. Timezone is **`Asia/Kuala_Lumpur`** everywhere — set explicitly, because a
container defaults to UTC and the 23:59 job would silently fire at the wrong hour.

---

## 3. Directory map

```
CDR/
├── CLAUDE.md                    Architecture decisions + build order. Read first.
├── README.md                    Runbook: how to start it and drive it day to day.
├── docs/ARCHITECTURE.md         This file.
│
├── backend/
│   ├── requirements.txt
│   ├── config/
│   │   ├── device_groups.yaml   Shared defaults only (status_key_default).
│   │   └── sites/
│   │       └── numed.yaml       ONE FILE PER SITE. Generated, not hand-typed.
│   ├── data/
│   │   └── cranes.db            The whole database. One file.
│   └── app/
│       ├── main.py              FastAPI entrypoint + lifespan (init db, sync config, start scheduler)
│       ├── config.py            All settings, from environment / .env
│       ├── clock.py             Local-time clock for every timestamp written to the DB
│       ├── logging_setup.py     stdout + logs/scheduler.log
│       ├── db/
│       │   ├── schema.sql       All 9 tables (CREATE TABLE IF NOT EXISTS)
│       │   └── database.py      Connection helper, WAL pragmas, additive migrations
│       ├── models/schemas.py    Pydantic request/response models
│       ├── api/                 HTTP layer — thin, delegates to services
│       │   ├── devices.py       groups, devices, live values, config reload
│       │   ├── captures.py      manual snapshot + reading a date's snapshot
│       │   ├── downtime.py      poll now, per-device and per-site day summaries
│       │   ├── remarks.py       site remarks + per-device recommendations
│       │   ├── reports.py       generate / preview / download a day's report
│       │   ├── imports.py       upload an existing PDF
│       │   └── status.py        "did last night work?" for the dashboard banner
│       └── services/            All the real logic lives here
│           ├── thingsboard_client.py   async ThingsBoard PE REST client
│           ├── config_sync.py          sites/*.yaml → DB
│           ├── snapshot_service.py     capture every configured key
│           ├── downtime_service.py     status polling, status_events, day summaries
│           ├── report_service.py       build context → Jinja → Playwright → PDF
│           ├── report_layout.py        page-fitting for the PDF template
│           ├── pdf_import_service.py   archive + extract imported PDFs
│           ├── scheduler.py            the 23:59 job and the poll interval
│           └── ops.py                  job_runs bookkeeping
│
├── frontend/                    Served as static files by the same process
│   ├── index.html               Dashboard: sites, last-run banner, Import Data button
│   ├── site-detail.html         One site: devices, statuses, remark forms
│   ├── reports.html             Report archive
│   └── assets/{app.js,style.css}
│
├── templates/daily_report.html  The approved report design. Jinja + print CSS.
│
├── scripts/                     Operator tools, all `python -m scripts.<name>`
│   ├── discover_device_groups.py  Walk a site's trigger device → sites/<site>.yaml
│   ├── inspect_db.py              Read-only look inside cranes.db, no SQL needed
│   ├── backfill_counters.py       Recover a past day's values from ThingsBoard history
│   ├── check_thingsboard.py       Layered connection check (DNS → login → keys)
│   └── backup.py                  Dated zip of cranes.db + reports/
│
├── tests/                       pytest; throwaway DB + empty config, never touches real data
├── reports/YYYY-MM-DD/          One folder per day: PDF + report.html + snapshot JSON
├── uploads/pdfs/                Imported PDFs, kept exactly as received
├── logs/  backups/              Bind-mounted, git-ignored
├── Dockerfile  docker-compose.yml
└── .claude/                     Project commands (/add-device-group, /regen-report) + debugger agent
```

---

## 4. The ThingsBoard data model — read this before touching anything

This is the part that surprises people, and getting it wrong wastes days.

**A device's status does not live on that device.** Each site has exactly one
**trigger device** (`NumedTrigger`, `ComputimeTrigger`, `BSC Trigger`, … 27 of them on
the instance). ThingsBoard's rule chain computes the status of *every sensor at that
site* and writes it back onto the trigger, as one key per sensor per family:

| Key on the trigger | Example value | What it is |
|---|---|---|
| `active_<sensor>` | `true` | up/down flag — **every** sensor has one, so this is the roster |
| `deviceStatus_<sensor>` | `ACTIVE` · `STATIC` · `NO DATA` | richer status; only ~2/3 of sensors have it |
| `deviceSeverity_<sensor>` | `OK` · `NON-CRITICAL` · `CRITICAL` | maps 1:1 to the report's pill colours |
| `activeTs_<sensor>` | `1788143952574` | epoch ms of the last → active transition |
| `InactiveTs_<sensor>` | `1788143894616` | epoch ms of the last → inactive transition |
| `forTotalUse_<sensor>` | `[51704534, 133102008]` | **`[inactive_ms, active_ms]`**, cumulative |
| `ActiveInActive_<sensor>` | `["A_1788…","N_14:21:44",51704534,133102008]` | current state + the same pair |
| `IssueOcc_<sensor>` | `419` | issue occurrences to date |
| `<sensor> 1D` | `10.0` | fault count for the current day, resets at midnight — this is ISSUE OCC. |

`NumedTrigger` alone carries **566 keys**. Comma-joining them all into one REST call
overflows the request line and ThingsBoard answers `400` with an HTML error page — the
client batches keys 30 at a time for this reason.

**Do not use the trigger's `Active Device` / `Inactive Device` counters.** The rule chain
computes them over its own subset (19 + 4 against a 34-sensor roster) and they drift
minute to minute. Counts in the report come from our own roster.

### The other device worth knowing about

`Fault Counter` (`71af5410-df0d-11f0-a0b2-1366f3bd8252`) holds per-site daily fault
totals for every site at once: `<Site>_fault_counter_all` / `_dpm` / `_rht` / `_rtd` /
`_ufm`, `<Site>_top_fault_device`, `<Site>_top_fault_category`, and a per-device
breakdown in `<Site>_device_fault_data`. Its per-device numbers match the `1D` keys
exactly. **It carries no timestamps**, so it cannot fill the DOWNTIME EVENTS table —
that needs `status_events`.

---

## 5. Database

Nine tables in `backend/app/db/schema.sql`. Everything is one SQLite file in WAL mode;
the FastAPI process is the only writer, so there is no lock contention to design around.

```
device_groups ──< devices ──< device_keys        the config, mirrored from sites/*.yaml
                     │
                     ├──< snapshots              one row per (capture, device, key)
                     ├──< status_events          one row per status change  ← downtime truth
                     └──< remarks                site remarks + device recommendations

reports          log of every generated PDF
job_runs         one row per job run — "did last night work?"
imported_pdfs    PDFs uploaded through the import page
```

**Key columns worth knowing:**

- `device_keys.role` — what a key *means*. Exactly one key per device is `status`.
  Current roles: `status`, `active_flag`, `active_ts`, `inactive_ts`, `total_use`,
  `uptime_split`, `daily_issues`, `issue_count`, `severity`, `heartbeat`.
  `heartbeat` is the odd one out: it names a key the sensor writes **itself**
  (`Seq #`, `Temp (C)`, ...), and gaps in it are the downtime events.
- `device_keys.tb_source_device_id` — **which ThingsBoard device actually reports this
  key.** Set to the site's trigger for trigger-sourced keys; `NULL` means "the device's
  own `tb_device_id`". This one column is what lets a whole site collapse into a single
  batched read.
- `remarks.device_id` — `NULL` = a site remark (many per day, the REMARK column on page
  1). Set = that device's engineer recommendation, at most one per day, enforced by a
  partial unique index created in `database.py::_migrate`.
- `status_events.end_ts` — `NULL` while the event is still open. Closing it fills
  `end_ts` and `duration_seconds`.

**Migrations** are additive and idempotent, in `database.py::_migrate`. `schema.sql` is
all `CREATE TABLE IF NOT EXISTS`, so it can never add a column to a table that already
exists on disk — that is what `_migrate` is for. It runs on every startup.

**Timestamps:** SQLite's `datetime('now')` default is **UTC**, which prints the 23:59 job
as 15:59. Every timestamp the app writes goes through `app/clock.py` instead. The column
defaults remain only as a backstop for hand-written SQL.

---

## 6. How data flows

```
  sites/*.yaml
       │  config_sync.sync_from_yaml()          on startup and POST /api/config/reload
       ▼
  device_groups / devices / device_keys
       │
       │  snapshot_service.capture_snapshot()   23:59 and on demand
       │    groups keys by tb_source_device_id → one batched read per trigger device
       ▼
  snapshots  (raw values, one row per key per capture)
       │
       │  downtime_service.poll_all_statuses()  every 5 min
       │    reads role='status', writes a row only on change,
       │    dated from activeTs_/InactiveTs_ rather than poll time
       │
       │  reconcile_service.reconcile_day(refresh=True)  hourly + before the report
       │    replaces today's rows with gaps in each sensor's OWN telemetry:
       │    silence >= downtime_gap_minutes, last reading -> next reading.
       │    This is what TB's widget shows and its fault counter counts; the
       │    STATIC/STALLED flags describe the aftermath, not the outage.
       │    Devices with no TB device of their own keep the flag walk.
       ▼
  status_events
       │
       │  downtime_service.day_summary()
       │    active/affected hours ← forTotalUse_ differenced day over day
       │    issue occurrences     ← "<sensor> 1D" (windows are the fallback)
       │    events list           ← status_events, clipped to the day and
       │                              debounced by min_event_seconds (120s)
       ▼
  report_service.generate_report()  →  Jinja → Playwright → reports/YYYY-MM-DD/
```

### Where each number on the report comes from

This is the table to keep open while working on the report.

| Report column | Source | Mechanism |
|---|---|---|
| **STATUS** pill | `deviceStatus_<sensor>`, else `active_<sensor>` | latest snapshot value for `role='status'`; `true`/`false` normalise to `ACTIVE`/`INACTIVE` |
| **ACTIVE HRS** | `forTotalUse_[1]` | today's capture minus yesterday's — the counter is cumulative |
| **AFFECTED HRS** | `forTotalUse_[0]` | same difference |
| **ISSUE OCC.** | `<sensor> 1D` | the trigger's daily fault count, resets at midnight. It will not tally with the DOWNTIME EVENTS rows below it, and should not: the rule chain raises STATIC after 15 min of no change and STALLED after 30, and those flags clear in about a minute, so a sensor can hold 33 windows on a day the trigger counts 2 faults. Counting windows ourselves was tried on 2026-09-01 and reverted. Falls back to our window count for a device with no 1D key. |
| **ENGINEER RECOMMENDATION** | `remarks` where `device_id` is set | active devices with none auto-fill `No action.` |
| **REMARK** (page 1) | `remarks` where `device_id IS NULL` | many per site per day |
| **DOWNTIME SUMMARY** | `status_events` | one row per device that dropped: how many windows, total hours down, its longest. Complete — every window is counted here even when it is not listed below |
| **LONGEST OUTAGES** | `status_events` | gaps in the sensor's own `heartbeat` key, found by the reconcile pass: a silence of `downtime_gap_minutes` (10) or more, from its last reading to its next. The same span TB's Downtime Events widget prints. Capped and ranked worst-first so a chatty sensor can't flood the PDF |
| **DEVICE TYPE BREAKDOWN** chip | derived | a device counts as attention if its status is `bad` **or** it went down `report_attention_issue_count` times today (default 5) — a device that flapped 10 times isn't Healthy just because it's up when the report runs |
| Site **HEALTHY / ATTENTION** | derived | a site escalates only on a `bad` status. `STATIC`/`STALLED` are warnings, not attention |

**Why the hours are a difference, not the raw counter.** `forTotalUse_` accumulates since
the counters last reset — one NUMed sensor reads 4455 hours. Differencing consecutive
daily captures yields exactly that day's hours, using ThingsBoard's own accounting rather
than re-deriving it from our poll samples. Three guards protect the result:

- a **negative** difference (the counter was reset) falls back to `status_events`;
- a difference spanning **more than a day plus slack** (a stale baseline) falls back too;
- a small overshoot **clamps to 24.0** rather than printing an impossible 25.0.

`day_summary()` reports which source it used as `source: "counter" | "events"`, so it is
never ambiguous. On the first day a site is configured there is no baseline — run
`python -m scripts.backfill_counters --date <yesterday>` to recover it from history.

---

## 7. Scheduled jobs

Both live in `services/scheduler.py`, started by the FastAPI lifespan.

| Job | When | What |
|---|---|---|
| `nightly` | 23:59 daily | capture the snapshot, then generate that day's report |
| `downtime` | every 5 min (+ one catch-up ~5 s after boot) | poll statuses, write `status_events` on change |

`nightly` has `misfire_grace_time=3600`, so a process that comes back within the hour
still runs it. If the machine was **off** at 23:59, an OS-level cron entry hitting
`POST /api/captures/run` then `POST /api/reports/{date}/generate` is the backup trigger
(see README).

`downtime` only records changes while the process is up, and it *samples* — a drop that
starts and ends inside one poll interval is never seen. Both holes are closed by
`reconcile_service`, which reads the status key's full ThingsBoard history and replaces a
day's rows with every transition TB recorded: hourly against today (`reconcile_minutes`),
once more at the top of the 23:59 job before the report is built, and on demand via
`POST /api/downtime/reconcile?date=` or
`python -m scripts.backfill_downtime --date <day> --refresh` (rows tagged
`source='reconcile'`). Without `--refresh` the script only fills days that have no events
at all (`source='backfill'`), which is the safe way to recover a stretch the process was
down for. A device whose history comes back empty is always left as-is. The report/site-detail then show the
real downtime for that past day. Every run writes a `job_runs` row, which is what the dashboard banner and
`python -m scripts.inspect_db` read.

---

## 8. HTTP API

All under `/api`. The frontend is served from `/` by the same process.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness + configured timezone and TB url |
| GET | `/api/status/last-run` | last capture / report / poll + next scheduled runs |
| GET | `/api/status/runs` | recent `job_runs` |
| GET | `/api/groups`, `/api/groups/{id}/devices` | configured sites and devices |
| POST | `/api/config/reload` | re-read `sites/*.yaml` into the DB |
| GET | `/api/devices/{id}/live` | pull current values straight from ThingsBoard |
| POST | `/api/captures/run` | manual snapshot ("Import Data") |
| GET | `/api/captures/{date}` | that date's latest snapshot values |
| POST | `/api/downtime/poll` | poll every status now |
| GET | `/api/downtime/groups/{id}?date=` | per-device day summary (incl. `source`) |
| GET | `/api/downtime/events/{device_id}` | full event list, uncapped |
| GET/POST/PUT/DELETE | `/api/remarks` | remarks; `?scope=site\|device` filters |
| POST | `/api/reports/{date}/generate` | (re)generate the PDF — 409 if no snapshot |
| GET | `/api/reports/{date}/preview` | render the HTML without making a PDF |
| GET | `/api/reports/{date}/pdf` | download |
| POST | `/api/imports` | upload an existing PDF (multipart `file`) |

---

## 9. Configuration

### Site files — one per site

`backend/config/sites/<site>.yaml`. **Generated, never hand-typed:**

```bash
python -m scripts.discover_device_groups --list-triggers
python -m scripts.discover_device_groups --trigger NumedTrigger --name NUMed --write
```

A run rewrites exactly one file, so it can never disturb the other 21 sites. It preserves
the four fields ThingsBoard has no idea about: `site_label`, `system_type`, `sort_order`,
`expected_report_hours`.

```yaml
name: NUMed
kind: device
site_label: null            # ← fill in by hand; prints on the site page
system_type: null           # ← fill in by hand
expected_report_hours: 24
trigger:
  device_name: NumedTrigger
  tb_device_id: 29a014c0-cf22-11f0-8150-1ffa4442f0c7
devices:
  - name: Numed RHT Wet Lab       # the sensor name = the suffix on every trigger key
    device_type: RHT              # drives the DEVICE TYPE BREAKDOWN
    tb_device_id: null            # usually null — the sensor isn't its own TB device
    keys:
      - {key_name: "deviceStatus_Numed RHT Wet Lab", role: status,       source: trigger}
      - {key_name: "forTotalUse_Numed RHT Wet Lab",  role: total_use,    source: trigger}
      - {key_name: "Numed RHT Wet Lab 1D",           role: daily_issues, source: trigger}
      # ... activeTs_, InactiveTs_, deviceSeverity_, IssueOcc_, ActiveInActive_
```

`source: trigger` means "read this key off the site's trigger device". `config_sync`
resolves it into `device_keys.tb_source_device_id`.

### Environment (`.env`)

`THINGSBOARD_URL`, `THINGSBOARD_USERNAME`, `THINGSBOARD_PASSWORD` are required. Everything
else in `config.py` has a working default: paths, `TIMEZONE`, `SNAPSHOT_HOUR`/`MINUTE`,
`DOWNTIME_POLL_MINUTES`, the report's event caps, `ENABLE_SCHEDULER`.

---

## 10. Tests

`pytest -q` — **49 tests**. They use a throwaway SQLite file *and their own empty config
directory*, so they never touch `cranes.db` or `backend/config/`. That isolation is set
up in `tests/conftest.py` before any backend import, because `config.Settings` reads the
environment at import time.

| File | Covers |
|---|---|
| `test_trigger_keys.py` | trigger-sourced keys, per-site config loading, counter-derived hours, local timestamps |
| `test_downtime.py` | status events, day summaries, config sync |
| `test_report_context.py`, `test_report_layout.py` | report context assembly and page fitting |
| `test_report_pipeline.py` | context -> HTML -> PDF, on sites built by `tests/factories.py` |
| `test_api.py`, `test_remarks_device.py`, `test_migration.py` | endpoints, remark rules, schema migration |

**Write a test alongside every new endpoint or service function.** Nobody else is
reviewing this code; the test suite is the review.

---

## 11. Operating it

```bash
# start
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

# see inside the database
python -m scripts.inspect_db                  # sites, captures, JOB RUNS, reports, remarks
python -m scripts.inspect_db --site NUMed     # devices: status, hours, issues, source
python -m scripts.inspect_db --device 327     # one device: keys, values, downtime

# drive the pipeline by hand
curl -X POST localhost:8000/api/captures/run
curl -X POST localhost:8000/api/downtime/poll
curl -X POST localhost:8000/api/reports/2026-08-31/generate

# offline, no ThingsBoard
# backup — one file and one folder, no replication
python -m scripts.backup
```

**Docker** uses bind mounts (not named volumes) for `reports/`, `uploads/`,
`backend/data/`, `backend/config/`, `logs/` and `backups/`, so they stay real editable
files on the host rather than being sealed inside the container. `TZ=Asia/Kuala_Lumpur`
is set explicitly and the app binds `0.0.0.0` so it is reachable across the LAN.

**Retention:** decided 2026-08-28 — keep `reports/` folders and raw snapshots
indefinitely. No prune job. `retention_days=0` in `config.py` is the switch if that changes.

---

## 12. Gotchas that have already bitten

1. **ThingsBoard `400` on a big key list.** 566 keys comma-joined overflow the request
   line. `latest_timeseries` batches 30 at a time — keep that if you touch the client.
2. **SQLite `datetime('now')` is UTC.** Use `app/clock.py` for anything written to the DB.
3. **Renaming the repo folder breaks the venv.** Console scripts hard-code the old
   absolute path; `uvicorn: no such file` is the symptom. Recreate the venv or sed the
   paths in `.venv/bin/*` and `.venv/pyvenv.cfg`.
4. **The report's row heights are estimated, not measured at render time.** `report_layout.py`
   mirrors the template CSS; if a column width changes there, change the matching
   `*_CHARS` constant too, or content silently runs past the footer.
5. **Frozen counters.** Five NUMed sensors have `forTotalUse_` stuck at `[0, 0]` and so
   print 0.0 active / 0.0 affected while reporting INACTIVE or NO DATA. The ThingsBoard
   dashboard shows the same zeros — the fix belongs in that rule chain, not here.
6. **The PDF summarises downtime, it does not list it.** A site averages ~100 windows a
   day, so DOWNTIME SUMMARY prints one row per device that dropped — count, total down,
   longest window, always complete — and LONGEST OUTAGES prints the
   `report_longest_events` (10) longest windows underneath. Nothing is lost: the summary
   counts every window and `/api/downtime/events/{id}` has them one by one.

---

## 13. Where the project stands

**Done:** ThingsBoard client, per-site config generation, capture, downtime detection,
remarks, report generation, PDF import, scheduler, containerisation, 49 tests.

**Live:** 1 site of 22 (**NUMed**, 33 devices), producing a 4-page report.

**Next:**

1. The remaining 21 sites — one `discover_device_groups` run each, then fill in
   `site_label` / `system_type` by hand.
2. Chase the five NUMed sensors whose `forTotalUse_` counters are frozen at `[0, 0]`.
3. `NumedmostInactiveDevice` and `NumedtimeInHours` on the Fault Counter device are empty
   (`None` / `0`) — they look purpose-built for the downtime summary.
4. Decide whether the site page should carry a fault-summary block from the Fault Counter
   device (totals by type, worst device).
