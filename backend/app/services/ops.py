"""job_runs bookkeeping — 'did last night's capture actually work?'"""
from __future__ import annotations

from ..db.database import get_conn, read_conn


def record(job: str, status: str, trigger: str = "scheduled", detail: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO job_runs (job, trigger, status, detail) VALUES (?, ?, ?, ?)",
            (job, trigger, status, detail),
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
