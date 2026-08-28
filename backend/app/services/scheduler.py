"""APScheduler wiring.

- 23:59 daily: capture the scheduled snapshot, then generate that day's report.
- every N min: poll device statuses for downtime detection.

A cron / Task Scheduler entry hitting POST /api/captures/run + /api/reports/{date}/generate
is the backup trigger if the process was down at 23:59 (see README).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings
from . import downtime_service, ops, report_service, snapshot_service

log = logging.getLogger("cranes.scheduler")
TZ = ZoneInfo(settings.timezone)

scheduler = AsyncIOScheduler(timezone=settings.timezone)


async def nightly_job() -> None:
    date = datetime.now(TZ).strftime("%Y-%m-%d")
    log.info("nightly job start for %s", date)
    try:
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


def start() -> None:
    scheduler.add_job(
        nightly_job,
        CronTrigger(hour=settings.snapshot_hour, minute=settings.snapshot_minute, timezone=TZ),
        id="nightly", replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )
    scheduler.add_job(
        downtime_job,
        IntervalTrigger(minutes=settings.downtime_poll_minutes, timezone=TZ),
        id="downtime", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info(
        "scheduler started: nightly %02d:%02d %s, downtime every %d min",
        settings.snapshot_hour, settings.snapshot_minute, settings.timezone,
        settings.downtime_poll_minutes,
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
