"""Which jobs the scheduler actually registers.

Both timers went off on 2026-09-03 when the ThingsBoard instance slowed enough to time
a poll out. The downtime poll came back on 2026-09-04 at 10 minutes — one batched read
per trigger device, 16 requests a poll for all 537 devices — while the hourly reconcile
stayed off, since the nightly job reconciles the day anyway. These tests pin down both
directions: what runs on a timer by default, and that either one is a single setting
away from changing.

`scheduler.scheduler` is a module-level AsyncIOScheduler that binds whichever loop is
running when it starts, so these swap in a fresh one and stub out start() — the jobs
are registered either way, and registration is what's under test.
"""
from __future__ import annotations

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.app.config import settings
from backend.app.services import scheduler as sched


@pytest.fixture
def jobs(monkeypatch):
    """Run scheduler.start() against a throwaway scheduler; return {id: trigger}."""
    def _start(**over):
        for k, v in over.items():
            monkeypatch.setattr(settings, k, v)
        fake = AsyncIOScheduler(timezone=settings.timezone)
        monkeypatch.setattr(fake, "start", lambda *a, **kw: None)
        monkeypatch.setattr(sched, "scheduler", fake)
        sched.start()
        return {j.id: str(j.trigger) for j in fake.get_jobs()}

    return _start


def test_the_nightly_run_and_the_10_minute_poll_are_what_runs_by_default(jobs):
    registered = jobs()

    assert set(registered) == {"nightly", "downtime", "downtime-catchup"}, \
        f"unexpected timers: {set(registered) ^ {'nightly', 'downtime', 'downtime-catchup'}}"
    assert "interval[0:10:00]" in registered["downtime"]


def test_the_reconcile_stays_off_by_default(jobs):
    """The nightly job reconciles the day right before the report is built, which is
    the pass that matters — an hourly one is extra load for no report content."""
    assert "reconcile" not in jobs()


def test_the_poll_goes_away_with_one_setting(jobs):
    """At 0 the app makes no ThingsBoard call of its own between nightly runs. The
    catch-up goes with it — it exists to close the gap before the *next* poll, so with
    no poll loop it is one more unasked-for round trip to a struggling instance."""
    registered = jobs(downtime_poll_minutes=0)

    assert set(registered) == {"nightly"}, \
        f"something still runs on a timer: {set(registered) - {'nightly'}}"


def test_the_poll_cadence_follows_the_setting(jobs):
    registered = jobs(downtime_poll_minutes=5)

    assert "interval[0:05:00]" in registered["downtime"]
    assert "downtime-catchup" in registered, "the catch-up should stay with the loop"


def test_the_reconcile_comes_back_with_one_setting(jobs):
    assert "interval[1:00:00]" in jobs(reconcile_minutes=60)["reconcile"]


def test_the_nightly_run_still_fires_at_2359(jobs):
    trigger = jobs()["nightly"]

    assert "hour='23'" in trigger
    assert "minute='59'" in trigger
