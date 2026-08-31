"""The load-bearing guard for services/report_layout.py.

Its constants mirror the template CSS. If either drifts, Chromium spills content onto
a page the estimator didn't plan for and test_pdf_page_count_matches_context fails.
Needs `playwright install chromium`, same as test_generate_report_writes_pdf.
"""
import pdfplumber
import pytest
from jinja2 import StrictUndefined

from backend.app.db.database import get_conn
from backend.app.services import report_service
from tests.factories import DATE, device, make_site


def _long_named_site(n: int = 26):
    """Device names long enough to wrap in the 108px DEVICE column.

    Row height is set by whichever column wraps furthest. Estimating it from the
    recommendation alone packed too many rows onto page 3 and ran the card over
    the footer, so the fixture deliberately uses names that wrap to three lines.
    """
    return make_site("NUMed", [
        device(f"Numed RHT External Utility Room {i}", active_h=13.2,
               affected_h=1.1, issues=i % 4)
        for i in range(n)
    ])


def test_strict_undefined_is_enabled():
    """A renamed context key must raise, not silently render an empty cell."""
    assert report_service._env.undefined is StrictUndefined


@pytest.mark.asyncio
async def test_pdf_page_count_matches_context():
    _long_named_site()
    ctx = report_service.build_context(DATE)
    out = await report_service.generate_report(DATE, trigger="manual")
    assert out["status"] == "generated", out["error"]

    with pdfplumber.open(out["pdf_path"]) as pdf:
        assert len(pdf.pages) == ctx["total_pages"]


@pytest.mark.asyncio
async def test_every_page_carries_chrome_and_its_number():
    _long_named_site()
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
    _long_named_site()
    out = await report_service.generate_report(DATE, trigger="manual")
    with pdfplumber.open(out["pdf_path"]) as pdf:
        # A4 = 595.28 x 841.89pt. prefer_css_page_size keeps us within a rounding hair.
        assert abs(pdf.pages[0].width - 595.28) < 2
        assert abs(pdf.pages[0].height - 841.89) < 2


@pytest.mark.asyncio
async def test_wide_site_paginates_without_dropping_devices():
    """A site far larger than one page must span pages and keep every device."""
    _long_named_site()
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


# --- DEVICE TYPE BREAKDOWN: repeated downtime counts as attention -------------
from backend.app.config import settings as _settings  # noqa: E402
from backend.app.services.report_service import _needs_attention, _type_breakdown  # noqa: E402


def _dev(dtype, severity="ok", issues=0):
    return {"device_type": dtype, "severity": severity, "issue_occurrences": issues}


def test_repeated_downtime_counts_as_attention_even_when_active():
    """A device that dropped 5+ times today is not Healthy just because it is up now."""
    assert _needs_attention(_dev("RHT", issues=5)) is True
    assert _needs_attention(_dev("RHT", issues=10)) is True
    assert _needs_attention(_dev("RHT", issues=4)) is False
    # A bad status still counts on its own, whatever the issue count.
    assert _needs_attention(_dev("UFM", severity="bad", issues=0)) is True


def test_type_breakdown_counts_flapping_devices():
    chips = {c["type"]: c for c in _type_breakdown([
        _dev("RHT", issues=10),          # active but flapping -> attention
        _dev("RHT", issues=1),           # fine
        _dev("DPM", issues=0),
        _dev("UFM", severity="bad"),     # down right now
    ])}
    assert (chips["RHT"]["count"], chips["RHT"]["attention"]) == (2, 1)
    assert chips["RHT"]["note"] == "1 attention" and chips["RHT"]["severity"] == "bad"
    assert chips["DPM"]["note"] == "Healthy" and chips["DPM"]["severity"] == "ok"
    assert chips["UFM"]["note"] == "1 attention"


def test_attention_threshold_is_configurable_and_disablable(monkeypatch):
    monkeypatch.setattr(_settings, "report_attention_issue_count", 3)
    assert _needs_attention(_dev("RHT", issues=3)) is True

    monkeypatch.setattr(_settings, "report_attention_issue_count", 0)
    assert _needs_attention(_dev("RHT", issues=99)) is False, "0 must disable the rule"
    assert _needs_attention(_dev("RHT", severity="bad", issues=0)) is True


@pytest.mark.asyncio
async def test_no_page_content_overruns_the_footer():
    """The estimator must never pack a page past the footer.

    Counting only the recommendation column let three-line device names overflow:
    page 3 of the NUMed report ran 15px into the footer and the card border was cut
    off mid-page. Measuring the rendered page is the only guard that catches it —
    the page count can be right while the content still spills.
    """
    from playwright.async_api import async_playwright

    _long_named_site(40)
    html = report_service.render_html(DATE)

    js = """
    () => [...document.querySelectorAll('.page')].map((pg, i) => {
      const pr = pg.getBoundingClientRect();
      const foot = pg.querySelector('.foot');
      const footTop = foot.getBoundingClientRect().top - pr.top;
      let bottom = 0, spills = [];
      pg.querySelectorAll('*').forEach(el => {
        if (el === foot || foot.contains(el)) return;
        const r = el.getBoundingClientRect();
        if (r.height) bottom = Math.max(bottom, r.bottom - pr.top);
      });
      pg.querySelectorAll('tbody td').forEach(td => {
        const cr = td.getBoundingClientRect();
        td.querySelectorAll('*').forEach(el => {
          const er = el.getBoundingClientRect();
          if (er.width && er.right > cr.right + 0.6) spills.push(el.className);
        });
        if (td.scrollWidth > td.clientWidth + 1) spills.push('text:' + td.innerText.slice(0, 20));
      });
      return {n: i + 1, footTop, bottom, spills};
    })
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        pages = await page.evaluate(js)
        await browser.close()

    assert pages, "render produced no pages"
    for info in pages:
        assert info["bottom"] <= info["footTop"], (
            f"page {info['n']} content runs {info['bottom'] - info['footTop']:.0f}px "
            f"past the footer"
        )
        assert not info["spills"], f"page {info['n']} has cells spilling: {info['spills']}"
