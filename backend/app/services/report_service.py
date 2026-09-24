"""Assemble the daily report context, render HTML -> PDF, save into reports/YYYY-MM-DD/.

Generated at 23:59 off that day's snapshot. No wait for late remarks — a remark
added after 23:59 gets in by regenerating: POST /api/reports/{date}/generate.
"""
from __future__ import annotations

import base64
import json
import logging
from collections import Counter
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from .. import clock
from ..config import settings
from ..db.database import get_conn, read_conn
from . import downtime_service as dt
from . import ops
from . import report_layout

log = logging.getLogger("cranes.report")

TZ = ZoneInfo(settings.timezone)

_env = Environment(
    loader=FileSystemLoader(str(settings.template_dir)),
    autoescape=select_autoescape(["html"]),
    # A renamed context key must fail loudly, not render a blank cell into a
    # client-facing PDF. generate_report() catches it and records status='failed'.
    undefined=StrictUndefined,
)
_env.filters["hrs"] = lambda v: f"{float(v):.1f}"


_LOGO_PATH = settings.template_dir / "assets" / "logo-mark.png"


@lru_cache(maxsize=1)
def _logo_uri() -> str | None:
    """The Squarecloud mark as a data: URI, or None if the asset is missing.

    Same reasoning as _font_css(): an about:blank page (Playwright's set_content) can't
    load file:// images, so it has to be inlined for the PDF, the HTTP preview and an
    offline container alike. None makes the template fall back to the plain "SC"
    monogram it always drew, so a missing asset degrades instead of breaking the render.
    """
    if not _LOGO_PATH.exists():
        return None
    b64 = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


_env.globals["logo_uri"] = _logo_uri


# Variable fonts: one file per family+style, the weight axis lives inside it.
_FONT_FACES = [
    ("JetBrains Mono", "JetBrainsMono-Variable.woff2", "normal", "100 800"),
    ("JetBrains Mono", "JetBrainsMono-Italic.woff2", "italic", "100 800"),
    ("Source Serif 4", "SourceSerif4-Variable.woff2", "normal", "200 900"),
]


@lru_cache(maxsize=1)
def _font_css() -> Markup:
    """@font-face rules with the woff2 files inlined as data URIs.

    set_content() gives the page an about:blank origin, so file:// fonts are
    CORS-blocked and a Google Fonts <link> needs live internet at 23:59. Inlining is
    the only form that works for the PDF, the HTTP preview and an offline container
    alike. Returns "" when templates/fonts/ is empty — the template then falls back
    to the Google Fonts link.
    """
    parts = []
    for family, filename, style, weight in _FONT_FACES:
        path = settings.template_dir / "fonts" / filename
        if not path.exists():
            continue
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append(
            f"@font-face{{font-family:'{family}';font-style:{style};"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}"
        )
    return Markup("".join(parts))


_env.globals["font_css"] = _font_css


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


def _remarks(group_id: int, date: str) -> list[dict]:
    """Every remark for the group on this date — both site-level and per-device.

    Fetched once and split on device_id by the caller.
    """
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM remarks WHERE group_id = ? AND report_date = ? ORDER BY created_at",
            (group_id, date),
        ).fetchall()]


def _status_history(group_id: int, date: str) -> list[dict]:
    """Visible non-ACTIVE intervals grouped under each device."""
    rows = dt.status_interval_rows(group_id, date)
    grouped: dict[int, dict] = {}
    for row in rows:
        group = grouped.setdefault(row["device_id"], {
            "device_id": row["device_id"], "device": row["device"], "rows": []
        })
        group["rows"].append(row)
    return list(grouped.values())


def _is_draft(date: str) -> bool:
    """True when nothing was captured for this date."""
    with read_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM snapshots WHERE capture_date = ? LIMIT 1",
            (date,),
        ).fetchone() is None


def _fmt_status_breakdown(statuses: list[str]) -> str:
    """['ACTIVE','ACTIVE','STATIC'] -> '2 Active / 1 Static' (active first, then count desc)."""
    counts = Counter(statuses)
    ordered = sorted(
        counts.items(),
        key=lambda kv: (kv[0] not in dt.ACTIVE_STATES, -kv[1], kv[0]),
    )
    return " / ".join(f"{n} {s.title()}" for s, n in ordered) or "—"


def _needs_attention(device: dict) -> bool:
    """Whether one device counts against its type's chip.

    Two ways in. The obvious one is a bad status right now. The other is repeated
    downtime: a device that dropped `report_attention_issue_count` times today has a
    real problem even if the poll happens to catch it up — NUMed's LT JORDAN Front
    went down 10 times and still reported ACTIVE when the report ran.
    """
    if device["severity"] != "ok":
        return True
    threshold = settings.report_attention_issue_count
    return bool(threshold) and (device.get("issue_occurrences") or 0) >= threshold


def _type_breakdown(devices: list[dict]) -> list[dict]:
    """The DEVICE TYPE BREAKDOWN chip row: one chip per device_type, in first-seen order."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for d in devices:
        t = (d.get("device_type") or "OTHER").upper()
        chip = seen.get(t)
        if chip is None:
            chip = {"type": t, "count": 0, "attention": 0}
            seen[t] = chip
            out.append(chip)
        chip["count"] += 1
        if _needs_attention(d):
            chip["attention"] += 1
    for chip in out:
        chip["note"] = f"{chip['attention']} attention" if chip["attention"] else "Healthy"
        chip["severity"] = "bad" if chip["attention"] else "ok"
    return out


def _worst_first(row: dict) -> tuple:
    """Ranking for what survives a cap: a real outage before a warning, longest first."""
    return (0 if row["severity"] == "bad" else 1, -row["seconds"])


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def _site_downtime(devices: list[dict], date: str) -> tuple[list[dict], list[dict], int]:
    """(per-device summary, the longest windows, how many windows aren't listed).

    A day holds far too many windows to print one row each — NUMed averages ~100 across
    the site, and listing them buried the outages that matter under a wall of short
    ones. So the report answers the two questions worth asking: which devices dropped
    and for how long in total (the summary, one row each), then the individual outages
    long enough to be worth reading times for.
    """
    summary: list[dict] = []
    windows: list[dict] = []

    for order, d in enumerate(devices):
        evs = [e for e in dt.downtime_for_date(d["id"], date) if not e["is_active"]]
        if not evs:
            continue
        built = []
        for e in evs:
            start = datetime.fromisoformat(e["start"])
            built.append({
                "device_id": d["id"],
                "device": d["name"],
                "status": e["status"],
                "severity": dt.severity(e["status"]),
                "date": start.strftime("%d %b %Y"),
                "start": _hhmm(e["start"]),
                "end": _hhmm(e["end"]),
                "duration_hours": round(e["seconds_in_day"] / 3600, 2),
                "seconds": e["seconds_in_day"],
                "_sort": (order, e["start"]),
            })
        windows += built

        worst = max(built, key=lambda r: r["seconds"])
        total = sum(r["seconds"] for r in built)
        summary.append({
            "device_id": d["id"],
            "device": d["name"],
            "events": len(built),
            "total_hours": round(total / 3600, 2),
            "longest": f"{worst['start']}–{worst['end']}",
            "longest_hours": worst["duration_hours"],
            "status": worst["status"],
            "severity": "bad" if any(r["severity"] == "bad" for r in built) else "warn",
            "seconds": total,
        })

    # Worst first, so the top of the summary is where the day's trouble is.
    summary.sort(key=_worst_first)
    for row in summary:
        del row["seconds"]

    listed = sorted(windows, key=_worst_first)[: settings.report_longest_events]
    omitted = len(windows) - len(listed)
    listed.sort(key=lambda r: r["_sort"])       # print in device / time order
    for r in listed:
        del r["_sort"], r["seconds"]
    return summary, listed, omitted


def build_context(date: str, trigger: str = "scheduled") -> dict:
    groups = _groups()
    status_of = dt.current_status_map(date)
    denominator_seconds = dt.report_denominator_seconds(date, trigger)
    site_rows = []
    site_details = []

    for idx, g in enumerate(groups, start=1):
        anchor = f"site-{idx}"
        devices = _devices(g["id"])

        rows = _remarks(g["id"], date)
        site_remarks = [r for r in rows if r["device_id"] is None]
        dev_remark = {r["device_id"]: r for r in rows if r["device_id"] is not None}
        status_history = _status_history(g["id"], date)

        dev_ctx = []
        active_now = 0
        attention = 0
        for d in devices:
            status = status_of.get(d["id"], "UNKNOWN")
            sev = dt.severity(status)
            summ = dt.day_summary(d["id"], date, denominator_seconds)
            summ.pop("events", None)   # kept off the device — see _site_events
            if status in dt.ACTIVE_STATES:
                active_now += 1
                rec, rec_auto, remark_id = "No action.", True, None
            else:
                # "Attention needed" on the site card counts every non-active device
                # (the approved report shows 3 for Computime: Static + Stalled + Inactive).
                # Whether the SITE escalates to ATTENTION is a stricter test — see below.
                attention += 1
                r = dev_remark.get(d["id"])
                rec = (r["body"].strip() if r else "")
                rec_auto = False
                remark_id = r["id"] if r else None
            dev_ctx.append({
                **d, "status": status, "severity": sev, **summ,
                "recommendation": rec,
                "recommendation_auto": rec_auto,
                "remark_id": remark_id,
            })

        total = len(devices) or 1
        downtime, events, omitted = _site_downtime(dev_ctx, date)
        # A site escalates only on a 'bad' status (INACTIVE and friends). Static and
        # Stalled are warnings — the approved report runs BSC East Wing with 1 Static
        # + 1 Stalled and still prints HEALTHY.
        health = "ATTENTION" if any(x["severity"] == "bad" for x in dev_ctx) else "HEALTHY"
        remark_body = site_remarks[0]["body"].strip() if site_remarks else ""

        # Colour of the cover-page remark: red when the site needs attention, grey when
        # nothing at all is off, amber for the in-between (a Static/Stalled warning).
        if health == "ATTENTION":
            remark_sev = "bad"
        elif all(x["severity"] == "ok" for x in dev_ctx):
            remark_sev = "muted"
        else:
            remark_sev = "warn"

        site_rows.append({
            "anchor": anchor,
            "name": g["name"],
            "devices": len(devices),
            # Site-level hours are the average per device, so this day-based metric
            # cannot exceed 24 even when several devices are down simultaneously.
            "no_data_hrs": round(
                sum(x["affected_hours"] for x in dev_ctx) / total, 1
            ),
            "active_pct": round(sum(x["active_pct"] for x in dev_ctx) / total, 1),
            "current_active": active_now,
            "status_breakdown": _fmt_status_breakdown([x["status"] for x in dev_ctx]),
            "health": health,
            "remark": remark_body,
            "remark_severity": remark_sev,
        })

        health_pct = round(100 * active_now / total)
        site_details.append({
            "anchor": anchor,
            "group": g,
            "devices": dev_ctx,
            "total": len(devices),
            "current_active": active_now,
            "attention": attention,
            "device_health_pct": health_pct,
            "device_health_severity": (
                "ok" if health_pct >= 95 else "warn" if health_pct >= 60 else "bad"
            ),
            "health": health,
            "breakdown": _type_breakdown(dev_ctx),
            "downtime": downtime,
            "events": events,
            "events_truncated": omitted,
            "remarks": site_remarks,
            "status_history": status_history,
        })

    total_devices = sum(s["devices"] for s in site_rows)
    total_active = sum(s["current_active"] for s in site_rows)
    is_draft = _is_draft(date)
    pages = report_layout.paginate(site_details, site_rows, is_draft,
                                   settings.report_downtime_sections)
    return {
        "date": date,
        "date_human": datetime.fromisoformat(date).strftime("%d %b %Y"),
        "doc_number": doc_number(date),
        "generated_at": datetime.now(TZ).strftime("%d %b %Y %H:%M %Z"),
        "is_draft": is_draft,
        "monitored_sites": len(groups),
        "total_devices": total_devices,
        "current_active": total_active,
        "attention_needed": total_devices - total_active,
        "sites": site_rows,
        "site_details": site_details,
        "pages": pages,
        "total_pages": len(pages),
    }


def render_html(date: str, trigger: str = "scheduled") -> str:
    ctx = build_context(date, trigger=trigger)
    return _env.get_template("daily_report.html").render(**ctx)


def _snapshot_payload(ctx: dict) -> dict:
    """The context minus `pages`, which just re-points at the same site dicts."""
    return {k: v for k, v in ctx.items() if k != "pages"}


def has_snapshot(date: str) -> bool:
    with read_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM snapshots WHERE capture_date = ? LIMIT 1", (date,)
        ).fetchone() is not None


async def generate_report(date: str, trigger: str = "manual") -> dict:
    from playwright.async_api import async_playwright

    out_dir = settings.reports_dir / date
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = settings.report_pdf_path(date)
    json_path = out_dir / f"snapshot_{date}.json"

    error = None
    try:
        # Inside the try: with StrictUndefined a template typo raises, and the
        # unattended 23:59 job must record status='failed' rather than die silently.
        ctx = build_context(date, trigger=trigger)
        json_path.write_text(json.dumps(_snapshot_payload(ctx), indent=2, default=str))
        html = _env.get_template("daily_report.html").render(**ctx)
        (out_dir / "report.html").write_text(html)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.emulate_media(media="print")
                await page.set_content(html, wait_until="networkidle")
                # networkidle is not a font guarantee, and the layout depends on them.
                await page.evaluate("() => document.fonts.ready")
                await page.pdf(
                    path=str(pdf_path),
                    print_background=True,
                    # Honour @page{size:210mm 297mm}. format="A4" is 0.6pt wider than
                    # true A4 — exactly the slack that spawns a phantom blank page.
                    prefer_css_page_size=True,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    scale=1,
                )
            finally:
                await browser.close()
        status = "generated"
    except Exception as e:  # noqa: BLE001 — record and surface, don't crash the job
        error = str(e)
        status = "failed"
        log.exception("report generation failed for %s", date)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reports
                (report_date, doc_number, pdf_path, snapshot_json_path, trigger, status,
                 error, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date, doc_number(date), str(pdf_path), str(json_path), trigger, status, error,
             clock.now_iso()),
        )
    ops.record("report", "success" if status == "generated" else "failed", trigger,
               f"{date}: {error or pdf_path.name}")
    log.info("report %s for %s (trigger=%s) -> %s", status, date, trigger, error or pdf_path)
    return {"date": date, "status": status, "pdf_path": str(pdf_path), "error": error}


def list_reports() -> list[dict]:
    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reports ORDER BY report_date DESC, generated_at DESC"
        ).fetchall()]
