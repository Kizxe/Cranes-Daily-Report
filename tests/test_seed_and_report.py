import pytest

from backend.app.services import report_service, seed_service


def test_seed_loads_all_sites():
    result = seed_service.load_seed()
    assert result["date"] == "2026-08-16"
    assert result["groups"] == 6
    assert result["devices"] == 42        # 12+10+8+6+4+2 from the mockup
    assert result["followups"] == 3


def test_seed_is_idempotent():
    a = seed_service.load_seed()
    b = seed_service.load_seed()
    assert a == b


def test_build_context_matches_shape():
    seed_service.load_seed()
    ctx = report_service.build_context("2026-08-16")
    assert ctx["monitored_sites"] == 6
    assert ctx["total_devices"] == 42
    assert len(ctx["site_details"]) == 6
    computime = next(s for s in ctx["site_details"] if s["group"]["name"] == "Computime")
    assert len(computime["devices"]) == 10
    assert computime["followups"]


def test_render_html_has_all_pages():
    seed_service.load_seed()
    html = report_service.render_html("2026-08-16")
    for site in ("BSC East Wing", "Computime", "Robert Bosch"):
        assert site in html


@pytest.mark.asyncio
async def test_generate_report_writes_pdf(tmp_path):
    seed_service.load_seed()
    out = await report_service.generate_report("2026-08-16", trigger="manual")
    assert out["status"] == "generated", out["error"]
    assert report_service.settings.report_pdf_path("2026-08-16").exists()
