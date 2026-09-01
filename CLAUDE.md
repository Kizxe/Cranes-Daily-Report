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
  **ISSUE OCC. counts the day's downtime windows** (settled 2026-09-01 after going back and
  forth): STATIC, STALLED, NO DATA and INACTIVE alike — exactly the rows the drill-down and
  the report list, so the number always equals the list under it. Counting windows only
  became safe once events were telemetry gaps; the trigger's `<sensor> 1D` key was tried in
  between, but it counts by the rule chain's own definition and only refreshes at capture
  time, so it printed 2 while the drill-down plainly showed 4. `1D` is still captured for
  reference; expect it to differ (CPD logs 1D faults with no telemetry gaps at all).
- **The Fault Counter device** `71af5410-df0d-11f0-a0b2-1366f3bd8252` holds per-site daily fault
  totals (`Numed_fault_counter_all/dpm/rht/rtd/ufm`, `Numed_top_fault_device`, `_category`,
  `Numed_device_fault_data`). No timestamps, so it CANNOT fill the DOWNTIME EVENTS table —
  that still needs `status_events` from the poll loop.
- **Don't use the trigger's `Active Device` / `Inactive Device` counters.** The rule chain
  computes them over its own subset (19+4 against a 34-sensor roster on NUMed) and they drift
  minute to minute. Report counts are derived from our own roster instead.
- **Downtime polling**: poll each device's status key every 5–15 min, write a `status_events` row only on change. Not worried about ThingsBoard rate limits — ThingsBoard's rule chain already computes status server-side, so this is just reading a settled key. One batched read per *trigger* device covers a whole site, and an event is dated from `activeTs_`/`InactiveTs_` rather than poll time, so the poll interval no longer rounds off downtime windows. Default is now **5 min** (`downtime_poll_minutes`), and `scheduler.start()` fires one catch-up poll ~5 s after boot so a restart leaves no gap. The loop only runs while the process is up, so the container must run 24/7 (`restart: unless-stopped`); for any stretch it was down, `scripts/backfill_downtime.py --date <day>` rebuilds that day's `status_events` from the trigger's status-key history (mirror of `backfill_counters.py`; skips devices that already have events that day).
- **Reconcile from ThingsBoard history** (added 2026-09-01): the poll only *samples*, so a
  device that dropped and recovered inside one interval never reached `status_events` —
  measured on NUMed at 11:32 that day, the poll held 28 downtime windows while the triggers'
  own `1D` counters summed to 98. `reconcile_service.reconcile_day(date, refresh=True)` walks
  the status key's full TB history and replaces that day's rows with every transition TB
  recorded. Runs hourly on today (`reconcile_minutes`, 0 disables) and once more at the top of
  the 23:59 job before the report is built; `POST /api/downtime/reconcile?date=` and
  `scripts/backfill_downtime.py --date <day> --refresh` are the manual doors. Without
  `--refresh` the script keeps its old gap-fill behaviour (only days with no events at all).
  A device whose history comes back empty is left untouched — a TB blip must never blank out
  a day of downtime.
- **Debounce: `min_event_seconds` (default 120)**. Reading every transition means reading the
  chatter: 2026-09-01 on NUMed held 491 non-active windows, median 38s, nearly all STATIC/
  STALLED. `downtime_service._debounce` folds a sub-threshold window into the (contiguous)
  window it interrupted, so ISSUE OCC., the site-detail drill-down and the report's DOWNTIME
  EVENTS table all describe real outages and agree with each other — 107 occurrences that day
  against the triggers' 98. Two same-status windows that aren't contiguous are two outages and
  are never merged. Replaces the old `report_min_event_seconds` filter.
- **A downtime event is the sensor going quiet** (2026-09-01, verified against the live
  instance). Not the STATIC/STALLED flag: on `Numed RHT Bell's Court Level 1- B.1.30` the
  TB widget's window `9:50:49 -> 10:02:48` is the silence, while the STALLED flag ran
  `10:02:48 -> 10:09:06` — the flag *starts* where the outage ends, which is why the two
  never tallied. `reconcile_service.gaps_from_points` walks the sensor's OWN telemetry
  (`device_keys.role = 'heartbeat'`, e.g. `Seq #`, resolved once and stored) and calls a
  silence of `downtime_gap_minutes` (**10**) a `NO DATA` window running from its last
  reading to its next, with the ACTIVE stretches in between emitted too so the day stays
  covered. Site total that day: **103 windows against the triggers' `1D` total of 102**,
  where the old flag walk produced 116 that matched nothing. Sensors with no TB device of
  their own (`RTD CH1`, `RTD CH2`, `UFM`) have no telemetry to find gaps in and keep the
  flag walk (`events_from_points`). Two silences either side of a single reading are two
  events — TB counts them that way, and `_debounce` only merges across a window it
  actually dropped.
- **The report summarises downtime rather than listing it** (2026-09-01). ~100 windows a
  day per site made the events table useless and cost three extra pages: DOWNTIME SUMMARY
  is one row per device that dropped (count, total down, longest window — always complete,
  worst first), then LONGEST OUTAGES lists the `report_longest_events` (10) longest windows.
  The old `report_max_events_per_device` / `_per_site` caps are gone with it.
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
   excluded as another site's. **Trimmed to 28 on 2026-09-01**: `Numed`, `Numed Setpoints`,
   `Numed Flowmeter`, `Numed Recovery`, `Meatrol DPM` were system flags, not devices —
   their status keys had not been written in 4–225 days, and each was printing an
   all-day INACTIVE window in the report. Removed from `sites/numed.yaml` and from the DB
   with `python -m scripts.prune_devices --site NUMed --apply` (config_sync upserts and
   never deletes, so the YAML edit alone leaves the rows behind). **`RTD CH1`, `RTD CH2`
   and `UFM` stay** — they are real channels whose counters are frozen at [0,0] by the
   rule chain, which is a ThingsBoard-side problem the report should keep showing.
   **KPJAP done 2026-09-01** — 34 devices, hand-authored (not via the discover script)
   and fixed against the live instance rather than trusted as typed: a `- name:`
   indentation slip broke the YAML parse; every device used `role: active_flag` for its
   status key instead of `role: status`, which would have left every device on the site
   reading UNKNOWN forever (`active_` is the only status source here — no device has a
   `deviceStatus_` key, same fallback NUMed's status-less sensors use); `InactiveTs_<x>`
   existed live for 33/34 devices but was missing from the hand-typed file, so it was
   added. **`Old EGL 1F` has no matching key on `KPJAPTriggers` at all** (`active_`,
   `activeTs_`, `forTotalUse_` all absent) — left configured (reads UNKNOWN, harmless)
   rather than guessed at — **resolved**: confirmed with the user it was a typo/duplicate
   of `Old EGL GF` (the only one of the two that exists on `KPJAPTriggers`), removed,
   pruned from the DB. 33 devices. Every KPJAP device has `tb_device_id: null` (no
   sensor is its own TB device here either), so downtime for the whole site runs on the
   flag-walk fallback, not the gap-based one — expected, not a bug; matches how
   `RTD CH1/CH2`/`UFM` already work at NUMed.
   **BTMC done 2026-09-01** — 6 devices, generated via the discover script then hand-
   trimmed by the user (dropped a bare `BTMC` entry, same system-flag pattern as
   NUMed's; corrected OT1–OT5 from the script's `Other` guess to `RHT`). Unlike KPJAP,
   every device here has its own `tb_device_id`, so downtime runs the gap-based path.
   Full wiring trace: `docs/onboarding-btmc.md`. **Flagged, not fixed — BTMC's telemetry
   reads stale across the board** (every `active_` value 5.5–7 days old, every
   `forTotalUse_` counter frozen at `[0,0]`, checked device by device) — synced in
   anyway on the user's call since the site is believed to be mid-commissioning; expect
   ACTIVE / 0.0 hrs everywhere until ThingsBoard's rule chain for `BTMCTriggers` starts
   writing current values. ThingsBoard-side, not fixable here.
   19 sites to go.
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
