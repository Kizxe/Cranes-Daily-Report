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
    assert "DOWNTIME EVENTS" in html
    # Removed by request: PIC follow-ups, the signal column, the duration Category
    # column, and the reports/ path that leaked into a client-facing document.
    assert "PIC FOLLOW-UP" not in html
    assert "SIGNAL HEALTH" not in html
    assert f"reports/{DATE}" not in html


@pytest.mark.asyncio
async def test_generate_report_writes_pdf():
    make_site("S", [device("A"), device("B", status="INACTIVE")])
    out = await report_service.generate_report(DATE, trigger="manual")
    assert out["status"] == "generated", out["error"]
    assert report_service.settings.report_pdf_path(DATE).exists()
    with pdfplumber.open(out["pdf_path"]) as pdf:
        assert len(pdf.pages) >= 2


def test_the_event_table_keeps_the_worst_events_when_it_hits_the_cap(monkeypatch):
    """A chatty sensor must not push a real outage off the page.

    The reconcile pass reads every transition ThingsBoard recorded, so one RHT sensor
    can hold dozens of short STATIC windows. Capped alphabetically, the DPM's INACTIVE
    outage never printed.
    """
    from backend.app.services import downtime_service as dt, report_service as rs

    make_site("NUMed", [device("A_RHT", status="STATIC"), device("Z_DPM", type="DPM")])
    monkeypatch.setattr(rs.settings, "report_max_events_per_device", 2)
    monkeypatch.setattr(rs.settings, "report_max_events_per_site", 3)

    def _name(device_id):
        from backend.app.db.database import read_conn
        with read_conn() as c:
            return c.execute("SELECT name FROM devices WHERE id = ?",
                             (device_id,)).fetchone()["name"]

    def fake_day(device_id, date):
        if "A_RHT" in _name(device_id):
            return [{"status": "STATIC", "is_active": False, "seconds_in_day": 200 + i,
                     "start": f"{DATE}T0{i}:00:00+08:00", "end": f"{DATE}T0{i}:05:00+08:00"}
                    for i in range(1, 6)]
        return [{"status": "INACTIVE", "is_active": False, "seconds_in_day": 7200,
                 "start": f"{DATE}T09:00:00+08:00", "end": f"{DATE}T11:00:00+08:00"}]

    monkeypatch.setattr(dt, "downtime_for_date", fake_day)
    site = rs.build_context(DATE)["site_details"][0]

    statuses = [e["status"] for e in site["events"]]
    assert "INACTIVE" in statuses, "the real outage must survive the cap"
    assert len(site["events"]) == 3 and site["events_truncated"] == 3
    # Survivors print in device / time order, not in ranking order.
    assert [e["device"] for e in site["events"]] == ["A_RHT", "A_RHT", "Z_DPM"]

