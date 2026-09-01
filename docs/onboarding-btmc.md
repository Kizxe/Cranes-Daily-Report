# Onboarding BTMC — where it's wired, step by step

This is a concrete trace of adding the third site, kept as a worked example alongside
the checklist in `README.md` (`### 2. Add a site`). Same sequence works for any site;
this file just shows exactly which file/table/endpoint each step touched, with the
real values from doing it.

## 0. The file

`backend/config/sites/btmc.yaml` — generated, not hand-typed:

```bash
python -m scripts.discover_device_groups --trigger BTMCTriggers --name BTMC --write
```

verified first against the live instance (the UUID the user supplied,
`82b83090-d0e6-11f0-8150-1ffa4442f0c7`, matched a device literally named
`BTMCTriggers` — confirmed via `GET /tenant/devices?textSearch=BTMCTriggers` before
trusting it). The discover script came out clean on its own — every device got exactly
one `role: status` key (`active_<sensor>`, since none of them have a `deviceStatus_`
key), `InactiveTs_` included, and a `role: heartbeat` key already resolved
(`reconcile_service.HEARTBEAT_PREFERENCE`, e.g. `Seq #`) — none of the bugs KPJAP's
hand-typed file had.

**Then hand-edited** to drop the bare `BTMC` entry (flagged as a likely system flag,
same pattern as NUMed's `Numed`/`Numed Setpoints` — confirmed unwanted, removed) and to
correct device types on OT1–OT5 from the script's generic `Other` guess to `RHT`. Down
to 6 devices. The heartbeat keys got removed by hand along with it — not a problem, see
step 4.

## 1. `config/sites/*.yaml` → the database

```bash
curl -X POST localhost:8000/api/config/reload
```

Runs `config_sync.sync_from_yaml()` — upserts every group/device/key the YAML lists
into `device_groups` / `devices` / `device_keys`. Result: `{"groups": 3, "devices": 67,
"keys": 382}` — BTMC's 6 joined KPJAP's 33 and NUMed's 33. One new `device_groups` row:

```
id=83  name=BTMC  sort_order=0  created_at=2026-09-01 09:45:40
```

and one `devices` row per sensor, e.g.:

```
id=1250  group_id=83  name='BTMC OT1'  device_type='RHT'
         tb_device_id='90101b10-a457-11f0-8150-1ffa4442f0c7'
```

with its `device_keys` resolved exactly as the YAML said — `role='status'` pointed at
`BTMCTriggers` (`tb_source_device_id='82b83090-...'`), `total_use`/`active_ts`/
`inactive_ts` the same:

```
active_BTMC OT1       role=status        source=82b83090-...  (BTMCTriggers)
activeTs_BTMC OT1     role=active_ts     source=82b83090-...
InactiveTs_BTMC OT1   role=inactive_ts   source=82b83090-...
forTotalUse_BTMC OT1  role=total_use     source=82b83090-...
```

**Nothing in `backend/app/` was touched to make this happen** — `config_sync`,
`snapshot_service`, `downtime_service`, `report_service` all already loop over
whatever rows exist in these three tables. Adding a site is entirely this step.

## 2. A value into the database

```bash
curl -X POST localhost:8000/api/captures/run    # -> snapshots
curl -X POST localhost:8000/api/downtime/poll   # -> status_events
```

`snapshot_service._fetch_plan()` grouped BTMC's keys by `tb_source_device_id`
(`82b83090-...`, one batched ThingsBoard call for the whole site) and wrote one
`snapshots` row per key. `downtime_service.poll_all_statuses()` read `role='status'`
for each device and opened a `status_events` row.

## 3. A day's worth of history, not just this instant

```bash
python -m scripts.backfill_counters --date 2026-08-31 --site BTMC   # 25 values, source='backfill'
curl -X POST "localhost:8000/api/downtime/reconcile?date=2026-09-01&site=BTMC"
```

The first seeds yesterday's `forTotalUse_` snapshot so `downtime_service.counter_hours`
has something to difference today against — without it, ACTIVE/AFFECTED HRS reads
`source: events` instead of `source: counter` (both correct, counter is just TB's own
accounting rather than ours).

The second replaced today's `status_events` with what ThingsBoard's own history says —
`{"devices": 6, "events": 8, "skipped": 0}`. Every BTMC device has its own
`tb_device_id`, so this ran the **gap-based** path (`reconcile_service.gaps_from_points`
against each sensor's own telemetry), not the flag-walk fallback KPJAP needed.

**This is also where the heartbeat keys came back.** The user's hand-edit had removed
`role: heartbeat` from the YAML; `reconcile_service.heartbeat_key()` noticed each
device had none, asked ThingsBoard for its own key list, and wrote `Seq #` straight
into `device_keys` (`tb_source_device_id=NULL` — reads off the device's own
`tb_device_id`, not the trigger). Confirmed after the fact:

```sql
SELECT d.name, k.key_name FROM device_keys k JOIN devices d ON d.id=k.device_id
JOIN device_groups g ON g.id=d.group_id WHERE g.name='BTMC' AND k.role='heartbeat';
--  BTMC Flowmeter | Seq #
--  BTMC OT1       | Seq #
--  ... (all 6)
```

The YAML not listing a heartbeat key doesn't mean the system doesn't have one — it's
self-healing, one ThingsBoard call the first time, free every time after.

## 4. Into the report

```bash
curl -X POST localhost:8000/api/reports/2026-09-01/generate
```

`report_service.build_context()` looped `_groups()` — now 3 rows instead of 2 — and
built a site section for each from the same `day_summary()` / `_site_downtime()` every
other site uses. Result: **11 pages** (was 10 with just KPJAP+NUMed), BTMC's own DEVICE
HEALTH card in between them, 6/6 devices ACTIVE, 100% health, one 0.17h `NO DATA` gap on
the Flowmeter in DOWNTIME SUMMARY. `pytest -q` — 86 passed, unaffected (no code changed,
only config + database rows).

## Known, not a bug: BTMC's telemetry is stale

Every `active_` value read 5.5–7 days old and every `forTotalUse_` counter was frozen at
`[0,0]` before this was wired in — checked device by device, not just one. The user's
call: sync it anyway, since it's mid-commissioning. So right now BTMC will show ACTIVE /
0.0 hrs across the board until ThingsBoard's rule chain for this trigger starts writing
current values — that's ThingsBoard-side, same as NUMed's frozen-[0,0] RTD/UFM channels,
not something this app can fix. Worth a look once BTMC is confirmed live.

## The repeatable sequence

```bash
python -m scripts.discover_device_groups --trigger <Trigger> --name <Site> --write
#   review/edit the file — check role: status is present and every key_name is real
#   (README.md § "Add a site" has the verification snippet)
curl -X POST localhost:8000/api/config/reload
curl -X POST localhost:8000/api/captures/run
curl -X POST localhost:8000/api/downtime/poll
python -m scripts.backfill_counters --date <yesterday> --site <Site>
curl -X POST "localhost:8000/api/downtime/reconcile?date=<today>&site=<Site>"
python -m scripts.inspect_db --site <Site>          # eyeball it before trusting it
curl -X POST localhost:8000/api/reports/<today>/generate
```
