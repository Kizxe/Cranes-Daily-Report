"""job_runs bookkeeping — 'did last night's capture actually work?'"""
from __future__ import annotations

from ..clock import now_iso
from ..db.database import get_conn, read_conn


def record(job: str, status: str, trigger: str = "scheduled", detail: str | None = None) -> None:
    # ran_at is written here, in local time, NOT by the column's datetime('now')
    # default — SQLite's is UTC, which would print the 23:59 job as 15:59 on the
    # dashboard and make a missed run impossible to spot at a glance.
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO job_runs (job, trigger, status, detail, ran_at) VALUES (?, ?, ?, ?, ?)",
            (job, trigger, status, detail, now_iso()),
        )


def last_run(job: str) -> dict | None:
    with read_conn() as conn:
        row = conn.execute(
            "SELECT * FROM job_runs WHERE job = ? ORDER BY ran_at DESC, id DESC LIMIT 1",
            (job,),
        ).fetchone()
    return dict(row) if row else None


def recent(limit: int = 30) -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM job_runs ORDER BY ran_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()]
