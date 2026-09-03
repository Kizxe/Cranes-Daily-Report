"""Which jobs the scheduler actually registers.

The 5-minute poll and the hourly reconcile were switched off on 2026-09-03 — the
ThingsBoard instance had slowed enough to time a poll out entirely, and the loop was
costing ~760 requests an hour around the clock. The app now touches TB at 23:59 and
on demand, so these tests pin down that the timers really are gone, that the nightly
run is not, and that turning either back on is one setting.

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


def test_only_the_nightly_run_is_scheduled_by_default(jobs):
    registered = jobs()

    assert set(registered) == {"nightly"}, \
        f"something still runs on a timer: {set(registered) - {'nightly'}}"


def test_no_boot_catchup_poll_when_polling_is_off(jobs):
    """The catch-up exists to close the gap before the *next* poll. With no poll loop
    it is just one more unasked-for round trip to a struggling instance."""
    assert "downtime-catchup" not in jobs()


def test_the_poll_comes_back_with_one_setting(jobs):
    registered = jobs(downtime_poll_minutes=5)

    assert "interval[0:05:00]" in registered["downtime"]
    assert "downtime-catchup" in registered, "the catch-up should return with the loop"


def test_the_reconcile_comes_back_with_one_setting(jobs):
    assert "interval[1:00:00]" in jobs(reconcile_minutes=60)["reconcile"]


def test_the_nightly_run_still_fires_at_2359(jobs):
    trigger = jobs()["nightly"]

    assert "hour='23'" in trigger
    assert "minute='59'" in trigger
