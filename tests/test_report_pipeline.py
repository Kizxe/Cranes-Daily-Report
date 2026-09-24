"""The report pipeline end to end: context -> HTML -> PDF.

Sites are built by tests/factories.py, so each test states the devices it needs.
"""
import pdfplumber
import pytest

from backend.app.services import report_service
from tests.factories import DATE, device, make_site


def test_build_context_matches_shape():
    make_site("Computime", [
        device("CT_RHT_01"),
        device("CT_RHT_02", status="STATIC", active_h=20.0, affected_h=4.0, issues=2),
        device("CT_DPM_01", type="DPM"),
    ], remark="Bridge may improve connection.")

    ctx = report_service.build_context(DATE)
    assert ctx["monitored_sites"] == 1
    assert ctx["total_devices"] == 3
    assert ctx["total_pages"] == len(ctx["pages"])
    assert ctx["pages"][0]["kind"] == "cover"

    site = ctx["site_details"][0]
    assert len(site["devices"]) == 3
    assert site["breakdown"]
    # PIC follow-ups are retired and must not come back into the context.
    assert "followups" not in site
    # The event list rides on the site, not on every device — that is what keeps
    # snapshot_<date>.json small.
    assert "events" not in site["devices"][0]


def test_hours_and_issues_come_through_to_the_context():
    make_site("NUMed", [device("RHT-1", active_h=13.2, affected_h=4.4, issues=8)])
    site = report_service.build_context(DATE)["site_details"][0]
    d = site["devices"][0]
    assert (d["active_hours"], d["affected_hours"], d["issue_occurrences"]) == (13.2, 4.4, 8)


def test_site_no_data_hours_are_average_device_hours():
    make_site("S", [device("A", active_h=0.0, affected_h=24.0),
                     device("B", active_h=0.0, affected_h=24.0)])
    site = report_service.build_context(DATE)["sites"][0]
    assert site["no_data_hrs"] == 24.0


def test_static_and_stalled_are_warnings_but_inactive_escalates_the_site():
    """The approved report prints a site with 1 Static + 1 Stalled as HEALTHY."""
    make_site("Warn Site", [device("A"), device("B", status="STATIC"),
                            device("C", status="STALLED")], sort_order=0)
    make_site("Bad Site", [device("D"), device("E", status="INACTIVE")], sort_order=1)

    health = {s["name"]: s["health"] for s in report_service.build_context(DATE)["sites"]}
    assert health["Warn Site"] == "HEALTHY"
    assert health["Bad Site"] == "ATTENTION"


def test_attention_counts_every_non_active_device_not_just_bad():
    make_site("S", [device("A"), device("B", status="STATIC"),
                    device("C", status="INACTIVE")])
    site = report_service.build_context(DATE)["site_details"][0]
    assert site["attention"] == 2
    assert site["current_active"] == 1


def test_recommendation_auto_for_active_manual_otherwise():
    make_site("S", [
        device("Up"),
        device("Down", status="INACTIVE", recommendation="Install a bridge at Level 2."),
    ])
    by_name = {d["name"]: d for d in report_service.build_context(DATE)["site_details"][0]["devices"]}

    assert by_name["Up"]["recommendation"] == "No action."
    assert by_name["Up"]["recommendation_auto"] is True
    assert by_name["Down"]["recommendation_auto"] is False
    assert "bridge" in by_name["Down"]["recommendation"].lower()


def test_render_html_has_the_sections_and_none_of_the_removed_ones():
    make_site("Computime", [device("CT_RHT_01"), device("CT_RHT_04", status="INACTIVE")])
    html = report_service.render_html(DATE)

    assert "Computime" in html
    assert "SQUARECLOUD MALAYSIA" in html
    assert "DEVICE TYPE BREAKDOWN" in html
    # Removed by request: PIC follow-ups, the signal column, the duration Category
    # column, and the reports/ path that leaked into a client-facing document.
    assert "PIC FOLLOW-UP" not in html
    assert "SIGNAL HEALTH" not in html
    assert f"reports/{DATE}" not in html
    # Switched off 2026-09-03, to come back later — see report_downtime_sections.
    assert "DOWNTIME SUMMARY" not in html
    assert "LONGEST OUTAGES" not in html


def test_downtime_sections_stay_off_but_the_data_behind_them_survives():
    """Off in the PDF only. The context still carries both, ready to switch back on."""
    make_site("NUMed", [device("A_RHT", status="INACTIVE", affected_h=3.0, issues=2)])
    ctx = report_service.build_context(DATE)
    [site] = ctx["site_details"]

    assert site["downtime"], "the summary rows stopped being computed"
    assert not any(p["summary_start"] or p["events_start"] for p in ctx["pages"])
    assert not any(p["summary"] or p["events"] for p in ctx["pages"])


def test_downtime_sections_come_back_when_the_setting_is_on(monkeypatch):
    """The one flip that restores them, so the removal stays reversible."""
    monkeypatch.setattr(report_service.settings, "report_downtime_sections", True)
    make_site("NUMed", [device("A_RHT", status="INACTIVE", affected_h=3.0, issues=2)])
    html = report_service.render_html(DATE)

    assert "DOWNTIME SUMMARY" in html
    assert "LONGEST OUTAGES" in html


@pytest.mark.asyncio
async def test_generate_report_writes_pdf():
    make_site("S", [device("A"), device("B", status="INACTIVE")])
    out = await report_service.generate_report(DATE, trigger="manual")
    assert out["status"] == "generated", out["error"]
    assert report_service.settings.report_pdf_path(DATE).exists()
    with pdfplumber.open(out["pdf_path"]) as pdf:
        assert len(pdf.pages) >= 2


def test_the_summary_counts_every_window_while_the_list_keeps_the_worst(monkeypatch):
    """The two blocks answer different questions and must stay consistent.

    A chatty RHT sensor holds dozens of short windows; a DPM holds one long outage.
    Listing them all buried the outage, so the list keeps only the longest — but the
    summary still has to account for every window, or the report quietly loses them.
    """
    from backend.app.services import downtime_service as dt, report_service as rs

    make_site("NUMed", [device("A_RHT", status="STATIC"), device("Z_DPM", type="DPM")])
    monkeypatch.setattr(rs.settings, "report_longest_events", 3)

    def _name(device_id):
        from backend.app.db.database import read_conn
        with read_conn() as c:
            return c.execute("SELECT name FROM devices WHERE id = ?",
                             (device_id,)).fetchone()["name"]

    def fake_day(device_id, date):
        if "A_RHT" in _name(device_id):
            return [{"status": "STATIC", "is_active": False, "seconds_in_day": 600 + i,
                     "start": f"{DATE}T0{i}:00:00+08:00", "end": f"{DATE}T0{i}:10:00+08:00"}
                    for i in range(1, 6)]
        return [{"status": "INACTIVE", "is_active": False, "seconds_in_day": 7200,
                 "start": f"{DATE}T09:00:00+08:00", "end": f"{DATE}T11:00:00+08:00"}]

    monkeypatch.setattr(dt, "downtime_for_date", fake_day)
    site = rs.build_context(DATE)["site_details"][0]

    summary = {row["device"]: row for row in site["downtime"]}
    assert summary["A_RHT"]["events"] == 5, "every window has to be counted somewhere"
    assert summary["Z_DPM"]["events"] == 1
    assert summary["Z_DPM"]["total_hours"] == 2.0
    assert summary["A_RHT"]["longest"] == "05:00–05:10"
    # Worst first: the DPM's real outage heads the summary.
    assert [r["device"] for r in site["downtime"]] == ["Z_DPM", "A_RHT"]

    assert len(site["events"]) == 3 and site["events_truncated"] == 3
    assert "INACTIVE" in [e["status"] for e in site["events"]], "the outage must survive"
    # What is listed prints in device / time order, not in ranking order.
    assert [e["device"] for e in site["events"]] == ["A_RHT", "A_RHT", "Z_DPM"]
