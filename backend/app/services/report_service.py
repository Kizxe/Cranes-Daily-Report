"""Assemble the daily report context, render HTML -> PDF, save into reports/YYYY-MM-DD/.

Generated at 23:59 off that day's snapshot. No wait for late remarks — a remark
added after 23:59 gets in by regenerating: POST /api/reports/{date}/generate.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import settings
from ..db.database import get_conn, read_conn
from . import downtime_service as dt

TZ = ZoneInfo(settings.timezone)

_env = Environment(
    loader=FileSystemLoader(str(settings.template_dir)),
    autoescape=select_autoescape(["html"]),
)


def doc_number(date: str) -> str:
    return "DDR-" + date.replace("-", "")


def _groups() -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM device_groups ORDER BY sort_order, name"
        ).fetchall()]


def _devices(group_id: int) -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM devices WHERE group_id = ? ORDER BY sort_order, name",
            (group_id,),
        ).fetchall()]


def _snapshot_map(date: str) -> dict[tuple[int, str], str]:
    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT device_id, key_name, value FROM snapshots
            WHERE capture_date = ?
              AND capture_ts = (SELECT MAX(capture_ts) FROM snapshots WHERE capture_date = ?)
            """,
            (date, date),
        ).fetchall()
    return {(r["device_id"], r["key_name"]): r["value"] for r in rows}


def _remarks(group_id: int, date: str) -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM remarks WHERE group_id = ? AND report_date = ? ORDER BY created_at",
            (group_id, date),
        ).fetchall()]


def _followups(group_id: int, date: str) -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT p.*, d.name AS device_name
            FROM pic_followups p
            LEFT JOIN devices d ON d.id = p.device_id
            WHERE p.group_id = ? AND p.report_date = ?
            ORDER BY p.created_at
            """,
            (group_id, date),
        ).fetchall()]


def build_context(date: str) -> dict:
    groups = _groups()
    snap = _snapshot_map(date)
    site_rows = []
    site_details = []

    for g in groups:
        devices = _devices(g["id"])
        dev_ctx = []
        active_now = 0
        attention = 0
        for d in devices:
            status = (snap.get((d["id"], "status")) or "UNKNOWN").upper()
            summ = dt.day_summary(d["id"], date)
            if status in dt.ACTIVE_STATES:
                active_now += 1
            else:
                attention += 1
            dev_ctx.append({**d, "status": status, **summ})

        total = len(devices) or 1
        avg_active_pct = round(sum(x["active_pct"] for x in dev_ctx) / total, 1)
        no_data_hrs = round(sum(x["affected_hours"] for x in dev_ctx), 1)

        site_rows.append({
            "name": g["name"],
            "devices": len(devices),
            "no_data_hrs": no_data_hrs,
            "active_pct": avg_active_pct,
            "current_active": active_now,
            "health": "HEALTHY" if attention == 0 else "ATTENTION",
            "remarks": _remarks(g["id"], date),
        })
        site_details.append({
            "group": g,
            "devices": dev_ctx,
            "total": len(devices),
            "current_active": active_now,
            "attention": attention,
            "device_health_pct": round(100 * active_now / total),
            "followups": _followups(g["id"], date),
            "remarks": _remarks(g["id"], date),
        })

    total_devices = sum(s["devices"] for s in site_rows)
    total_active = sum(s["current_active"] for s in site_rows)
    return {
        "date": date,
        "date_human": datetime.fromisoformat(date).strftime("%d %b %Y"),
        "doc_number": doc_number(date),
        "generated_at": datetime.now(TZ).strftime("%d %b %Y %H:%M %Z"),
        "monitored_sites": len(groups),
        "total_devices": total_devices,
        "current_active": total_active,
        "attention_needed": total_devices - total_active,
        "sites": site_rows,
        "site_details": site_details,
    }


def render_html(date: str) -> str:
    ctx = build_context(date)
    return _env.get_template("daily_report.html").render(**ctx)


async def generate_report(date: str, trigger: str = "manual") -> dict:
    from playwright.async_api import async_playwright

    out_dir = settings.reports_dir / date
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{doc_number(date)}.pdf"
    json_path = out_dir / "snapshot.json"

    ctx = build_context(date)
    json_path.write_text(json.dumps(ctx, indent=2, default=str))
    html = _env.get_template("daily_report.html").render(**ctx)
    (out_dir / "report.html").write_text(html)

    error = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "14mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
            )
            await browser.close()
        status = "generated"
    except Exception as e:  # noqa: BLE001 — record and surface, don't crash the job
        error = str(e)
        status = "failed"

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reports
                (report_date, doc_number, pdf_path, snapshot_json_path, trigger, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (date, doc_number(date), str(pdf_path), str(json_path), trigger, status, error),
        )
    return {"date": date, "status": status, "pdf_path": str(pdf_path), "error": error}


def list_reports() -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reports ORDER BY report_date DESC, generated_at DESC"
        ).fetchall()]
