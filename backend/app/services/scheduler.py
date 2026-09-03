"""APScheduler wiring.

- 23:59 daily: rebuild the day's events from ThingsBoard history, capture the
  scheduled snapshot, then generate that day's report.
- every N min: poll device statuses for downtime detection.
- every `reconcile_minutes`: rebuild today's events from ThingsBoard history, so a
  drop-and-recover that fell between two polls still reaches the report.

A cron / Task Scheduler entry hitting POST /api/captures/run + /api/reports/{date}/generate
is the backup trigger if the process was down at 23:59 (see README).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings
from . import downtime_service, ops, reconcile_service, report_service, snapshot_service

log = logging.getLogger("cranes.scheduler")
TZ = ZoneInfo(settings.timezone)

scheduler = AsyncIOScheduler(timezone=settings.timezone)


def report_date_for(now: datetime | None = None) -> str:
    """Which day the 23:59 run is closing out.

    NOT simply today's date. `misfire_grace_time` lets the job run up to an hour late —
    a sleeping laptop woke at 00:03 and the run stamped itself 2026-09-01, so 31 Aug
    never got its scheduled report and 1 Sep got one built from three minutes of data.

    The rule: the run belongs to the most recent day whose scheduled time has passed.
    Fire at 23:59:00 and that is today; fire at 00:03 and it is still yesterday's run.
    """
    now = now or datetime.now(TZ)
    scheduled_today = now.replace(hour=settings.snapshot_hour,
                                  minute=settings.snapshot_minute,
                                  second=0, microsecond=0)
    day = now if now >= scheduled_today else now - timedelta(days=1)
    return day.strftime("%Y-%m-%d")


async def nightly_job() -> None:
    date = report_date_for()
    log.info("nightly job start for %s", date)
    try:
        # Last word on the day's downtime before it is printed: the poll loop only
        # sampled it, ThingsBoard's history has every transition. Never fatal — a TB
        # hiccup here must not cost the whole report.
        try:
            summary = await reconcile_service.reconcile_day(
                date, refresh=True, lookback_days=settings.reconcile_lookback_days,
                trigger="scheduled")
            log.info("pre-report reconcile for %s: %s", date, summary)
        except Exception:  # noqa: BLE001
            log.exception("pre-report reconcile failed for %s — reporting anyway", date)
        await snapshot_service.capture_snapshot(trigger="scheduled", capture_date=date)
        await report_service.generate_report(date, trigger="scheduled")
        ops.record("nightly", "success", "scheduled", date)
        log.info("nightly job done for %s", date)
    except Exception as e:  # noqa: BLE001
        ops.record("nightly", "failed", "scheduled", f"{date}: {e}")
        log.exception("nightly job failed for %s", date)


async def downtime_job() -> None:
    try:
        result = await downtime_service.poll_all_statuses(trigger="scheduled")
        if result["changes"]:
            log.info("downtime poll: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("downtime poll failed")


async def reconcile_job() -> None:
    try:
        result = await reconcile_service.reconcile_today(trigger="scheduled")
        log.info("reconcile: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("reconcile failed")


def start() -> None:
    scheduler.add_job(
        nightly_job,
        CronTrigger(hour=settings.snapshot_hour, minute=settings.snapshot_minute, timezone=TZ),
        id="nightly", replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )
    # Both timers are optional. At 0 the app makes no ThingsBoard call of its own
    # between nightly runs — see settings.downtime_poll_minutes for why that is the
    # default and what it costs.
    if settings.downtime_poll_minutes:
        scheduler.add_job(
            downtime_job,
            IntervalTrigger(minutes=settings.downtime_poll_minutes, timezone=TZ),
            id="downtime", replace_existing=True, max_instances=1, coalesce=True,
        )
        # Catch-up poll a few seconds after boot — a restart shouldn't leave a blind
        # spot the length of a whole poll interval before downtime is next checked.
        # Pointless when nothing polls on a timer, so it goes with the loop.
        scheduler.add_job(
            downtime_job,
            "date", run_date=datetime.now(TZ) + timedelta(seconds=5),
            id="downtime-catchup", replace_existing=True, max_instances=1,
        )
    if settings.reconcile_minutes:
        scheduler.add_job(
            reconcile_job,
            IntervalTrigger(minutes=settings.reconcile_minutes, timezone=TZ),
            id="reconcile", replace_existing=True, max_instances=1, coalesce=True,
        )
    scheduler.start()
    every = lambda m: f"every {m} min" if m else "never"  # noqa: E731
    log.info(
        "scheduler started: nightly %02d:%02d %s, downtime poll %s, reconcile %s",
        settings.snapshot_hour, settings.snapshot_minute, settings.timezone,
        every(settings.downtime_poll_minutes), every(settings.reconcile_minutes),
    )


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def next_runs() -> dict:
    if not scheduler.running:
        return {}
    out = {}
    for job in scheduler.get_jobs():
        out[job.id] = job.next_run_time.isoformat() if job.next_run_time else None
    return out
