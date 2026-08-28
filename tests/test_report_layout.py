"""The load-bearing guard for services/report_layout.py.

Its constants mirror the template CSS. If either drifts, Chromium spills content onto
a page the estimator didn't plan for and test_pdf_page_count_matches_context fails.
Needs `playwright install chromium`, same as test_generate_report_writes_pdf.
"""
import pdfplumber
import pytest
from jinja2 import StrictUndefined

from backend.app.db.database import get_conn
from backend.app.services import report_service, seed_service

DATE = "2026-08-16"


def test_strict_undefined_is_enabled():
    """A renamed context key must raise, not silently render an empty cell."""
    assert report_service._env.undefined is StrictUndefined


@pytest.mark.asyncio
async def test_pdf_page_count_matches_context():
    seed_service.load_seed()
    ctx = report_service.build_context(DATE)
    out = await report_service.generate_report(DATE, trigger="manual")
    assert out["status"] == "generated", out["error"]

    with pdfplumber.open(out["pdf_path"]) as pdf:
        assert len(pdf.pages) == ctx["total_pages"]


@pytest.mark.asyncio
async def test_every_page_carries_chrome_and_its_number():
    seed_service.load_seed()
    out = await report_service.generate_report(DATE, trigger="manual")
    assert out["status"] == "generated", out["error"]

    with pdfplumber.open(out["pdf_path"]) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").replace("\n", " ")
            assert "CONFIDENTIAL" in text, f"page {i} lost its footer"
            assert f"{i} / {n}" in text, f"page {i} lost its page number"


@pytest.mark.asyncio
async def test_page_size_is_a4():
    seed_service.load_seed()
    out = await report_service.generate_report(DATE, trigger="manual")
    with pdfplumber.open(out["pdf_path"]) as pdf:
        # A4 = 595.28 x 841.89pt. prefer_css_page_size keeps us within a rounding hair.
        assert abs(pdf.pages[0].width - 595.28) < 2
        assert abs(pdf.pages[0].height - 841.89) < 2


@pytest.mark.asyncio
async def test_wide_site_paginates_without_dropping_devices():
    """A site far larger than one page must span pages and keep every device."""
    seed_service.load_seed()
    long_rec = "Inspect the sensor, reposition the bridge, then re-survey coverage. " * 2
    with get_conn() as conn:
        gid = conn.execute(
            "INSERT INTO device_groups (name, kind, sort_order) VALUES ('WideSite','device',99)"
        ).lastrowid
        for i in range(40):
            did = conn.execute(
                "INSERT INTO devices (group_id, name, device_type, sort_order)"
                " VALUES (?, ?, 'RHT', ?)",
                (gid, f"WD_{i:02d}", i),
            ).lastrowid
            conn.execute(
                "INSERT INTO device_keys (device_id, key_name, role) VALUES (?, 'status', 'status')",
                (did,),
            )
            conn.execute(
                "INSERT INTO snapshots (capture_ts, capture_date, trigger, device_id,"
                " key_name, value) VALUES (?, ?, 'manual', ?, 'status', 'INACTIVE')",
                (f"{DATE}T23:59:00+08:00", DATE, did),
            )
            conn.execute(
                "INSERT INTO remarks (group_id, device_id, report_date, body, author)"
                " VALUES (?, ?, ?, ?, 'test')",
                (gid, did, DATE, long_rec),
            )

    ctx = report_service.build_context(DATE)
    wide_pages = [p for p in ctx["pages"] if p["site"] and p["site"]["group"]["name"] == "WideSite"]
    assert len(wide_pages) >= 2, "40 devices must not claim to fit on one page"

    out = await report_service.generate_report(DATE, trigger="manual")
    assert out["status"] == "generated", out["error"]
    with pdfplumber.open(out["pdf_path"]) as pdf:
        assert len(pdf.pages) == ctx["total_pages"]
        text = " ".join((p.extract_text() or "") for p in pdf.pages).replace("\n", " ")
    for i in range(40):
        assert f"WD_{i:02d}" in text, f"WD_{i:02d} fell off the report"
