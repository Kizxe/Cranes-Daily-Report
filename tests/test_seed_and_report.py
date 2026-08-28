import pytest

from backend.app.services import report_service, seed_service


def test_seed_loads_all_sites():
    result = seed_service.load_seed()
    assert result["date"] == "2026-08-16"
    assert result["groups"] == 6
    assert result["devices"] == 42        # 12+10+8+6+4+2 from the mockup
    assert result["followups"] == 3
    # CT_RHT_02 (Static), CT_RHT_04 (Inactive), RTD CH1 (Stalled) — active devices get
    # no stored remark, the report auto-fills "No action." for them.
    assert result["device_remarks"] == 3


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
    assert ctx["total_pages"] == len(ctx["pages"])
    assert ctx["pages"][0]["kind"] == "cover"

    computime = next(s for s in ctx["site_details"] if s["group"]["name"] == "Computime")
    assert len(computime["devices"]) == 10
    # PIC follow-ups are no longer part of the report context (the table and the
    # /api/followups routes stay — see test_api.py::test_followup_create).
    assert "followups" not in computime
    # The event list rides on the site, not on every device — that is what kept
    # snapshot_<date>.json at 41KB.
    assert "events" not in computime["devices"][0]
    assert computime["events"]
    assert computime["breakdown"]


def test_site_health_matches_approved_report():
    """Static/Stalled are warnings; only INACTIVE escalates a site to ATTENTION.

    The approved sample prints BSC East Wing (1 Static + 1 Stalled) as HEALTHY.
    """
    seed_service.load_seed()
    ctx = report_service.build_context("2026-08-16")
    health = {s["name"]: s["health"] for s in ctx["sites"]}
    assert health["BSC East Wing"] == "HEALTHY"
    assert health["G Hotel"] == "HEALTHY"
    assert health["Sanmina"] == "HEALTHY"
    assert health["Computime"] == "ATTENTION"
    assert health["KPJAP"] == "ATTENTION"
    assert health["Robert Bosch"] == "ATTENTION"

    computime = next(s for s in ctx["site_details"] if s["group"]["name"] == "Computime")
    # "Attention needed" on the site card counts every non-active device, not just bad.
    assert computime["attention"] == 3
    assert computime["current_active"] == 7
    assert computime["device_health_pct"] == 70


def test_recommendation_auto_for_active_manual_otherwise():
    seed_service.load_seed()
    ctx = report_service.build_context("2026-08-16")
    computime = next(s for s in ctx["site_details"] if s["group"]["name"] == "Computime")
    by_name = {d["name"]: d for d in computime["devices"]}

    active = by_name["CT_RHT_01"]
    assert active["recommendation"] == "No action."
    assert active["recommendation_auto"] is True

    down = by_name["CT_RHT_04"]
    assert down["recommendation_auto"] is False
    assert "bridge" in down["recommendation"].lower()


def test_render_html_has_all_pages():
    seed_service.load_seed()
    html = report_service.render_html("2026-08-16")
    for site in ("BSC East Wing", "Computime", "Robert Bosch"):
        assert site in html
    assert "SQUARECLOUD MALAYSIA" in html
    assert "DEVICE TYPE BREAKDOWN" in html
    assert "DOWNTIME EVENTS" in html
    # Removed by request: PIC follow-ups, the signal column, the duration Category
    # column, and the reports/ path that leaked into a client-facing document.
    assert "PIC FOLLOW-UP" not in html
    assert "SIGNAL HEALTH" not in html
    assert "reports/2026-08-16" not in html


@pytest.mark.asyncio
async def test_generate_report_writes_pdf(tmp_path):
    seed_service.load_seed()
    out = await report_service.generate_report("2026-08-16", trigger="manual")
    assert out["status"] == "generated", out["error"]
    assert report_service.settings.report_pdf_path("2026-08-16").exists()
