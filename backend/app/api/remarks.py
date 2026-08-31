from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import clock
from ..db.database import get_conn, read_conn
from ..models.schemas import FollowupIn, FollowupOut, RemarkIn, RemarkOut

router = APIRouter(tags=["remarks"])


# --- remarks: site-level (device_id NULL) and per-device recommendations ----
@router.get("/remarks", response_model=list[RemarkOut])
def list_remarks(
    group_id: int | None = None,
    report_date: str | None = None,
    device_id: int | None = None,
    scope: str | None = Query(None, pattern="^(site|device)$"),
):
    q = ("SELECT r.*, d.name AS device_name FROM remarks r "
         "LEFT JOIN devices d ON d.id = r.device_id WHERE 1=1")
    p: list = []
    if group_id is not None:
        q += " AND r.group_id = ?"; p.append(group_id)
    if report_date is not None:
        q += " AND r.report_date = ?"; p.append(report_date)
    if device_id is not None:
        q += " AND r.device_id = ?"; p.append(device_id)
    if scope == "site":
        q += " AND r.device_id IS NULL"
    elif scope == "device":
        q += " AND r.device_id IS NOT NULL"
    q += " ORDER BY r.created_at DESC"
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(q, p).fetchall()]


@router.post("/remarks", response_model=RemarkOut, status_code=201)
def create_remark(payload: RemarkIn):
    """Site remarks append; device recommendations upsert (one per device per day)."""
    with get_conn() as conn:
        if payload.device_id is None:
            cur = conn.execute(
                "INSERT INTO remarks (group_id, device_id, report_date, body, author,"
                " created_at, updated_at) VALUES (?, NULL, ?, ?, ?, ?, ?)",
                (payload.group_id, payload.report_date, payload.body, payload.author,
                 clock.now_iso(), clock.now_iso()),
            )
            new_id = cur.lastrowid
        else:
            owner = conn.execute(
                "SELECT group_id FROM devices WHERE id = ?", (payload.device_id,)
            ).fetchone()
            if owner is None:
                raise HTTPException(404, f"device {payload.device_id} not found")
            if owner["group_id"] != payload.group_id:
                raise HTTPException(
                    400, f"device {payload.device_id} does not belong to group {payload.group_id}"
                )
            conn.execute(
                """
                INSERT INTO remarks (group_id, device_id, report_date, body, author,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, report_date) WHERE device_id IS NOT NULL
                DO UPDATE SET body = excluded.body,
                              author = excluded.author,
                              updated_at = excluded.updated_at
                """,
                (payload.group_id, payload.device_id, payload.report_date,
                 payload.body, payload.author, clock.now_iso(), clock.now_iso()),
            )
            new_id = conn.execute(
                "SELECT id FROM remarks WHERE device_id = ? AND report_date = ?",
                (payload.device_id, payload.report_date),
            ).fetchone()["id"]
        row = conn.execute(
            "SELECT r.*, d.name AS device_name FROM remarks r "
            "LEFT JOIN devices d ON d.id = r.device_id WHERE r.id = ?",
            (new_id,),
        ).fetchone()
    return dict(row)


@router.put("/remarks/{remark_id}", response_model=RemarkOut)
def update_remark(remark_id: int, payload: RemarkIn):
    """Only body/author are editable — group_id, report_date and device_id in the
    payload are deliberately ignored, so a remark can't be moved between devices."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM remarks WHERE id = ?", (remark_id,)).fetchone():
            raise HTTPException(404, "remark not found")
        conn.execute(
            "UPDATE remarks SET body = ?, author = ?, updated_at = ? WHERE id = ?",
            (payload.body, payload.author, clock.now_iso(), remark_id),
        )
        row = conn.execute(
            "SELECT r.*, d.name AS device_name FROM remarks r "
            "LEFT JOIN devices d ON d.id = r.device_id WHERE r.id = ?",
            (remark_id,),
        ).fetchone()
    return dict(row)


@router.delete("/remarks/{remark_id}", status_code=204)
def delete_remark(remark_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM remarks WHERE id = ?", (remark_id,))


# --- PIC follow-ups -----------------------------------------------------
@router.get("/followups", response_model=list[FollowupOut])
def list_followups(group_id: int | None = None, report_date: str | None = None):
    q = ("SELECT p.*, d.name AS device_name FROM pic_followups p "
         "LEFT JOIN devices d ON d.id = p.device_id WHERE 1=1")
    p: list = []
    if group_id is not None:
        q += " AND p.group_id = ?"; p.append(group_id)
    if report_date is not None:
        q += " AND p.report_date = ?"; p.append(report_date)
    q += " ORDER BY p.created_at DESC"
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(q, p).fetchall()]


@router.post("/followups", response_model=FollowupOut, status_code=201)
def create_followup(payload: FollowupIn):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pic_followups
               (group_id, report_date, device_id, issue, remark, assigned_pic, date_assigned)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payload.group_id, payload.report_date, payload.device_id, payload.issue,
             payload.remark, payload.assigned_pic, payload.date_assigned),
        )
        row = conn.execute(
            "SELECT p.*, d.name AS device_name FROM pic_followups p "
            "LEFT JOIN devices d ON d.id = p.device_id WHERE p.id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


@router.delete("/followups/{followup_id}", status_code=204)
def delete_followup(followup_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM pic_followups WHERE id = ?", (followup_id,))
