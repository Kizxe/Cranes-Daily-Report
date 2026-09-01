"""Page-fitting for templates/daily_report.html.

The report needs page chrome and "k / N" on every page. Chromium can't do that on its
own: CSS `@page` margin boxes are unimplemented in Blink (no counter(page)), and
Playwright's display_header_footer renders the chrome in a separate frame that loads no
fonts, which would break the letterspaced wordmark on every page.

So the template emits fixed-size 210x297mm `.page` boxes and we decide here what goes on
each one. That also makes the HTML preview paginate identically to the PDF, because the
page box is the same in both media.

THE CONSTANTS BELOW MIRROR THE TEMPLATE CSS. Change them together.
tests/test_report_layout.py::test_pdf_page_count_matches_context is the guard: if the
estimate drifts, Chromium spills onto an extra page and that test fails.
"""
from __future__ import annotations

import textwrap

# Every value below was measured off the rendered page, not estimated. To re-measure:
# generate a report, open its reports/<date>/report.html in Chromium and read
# getBoundingClientRect() for .head, .foot, .health-card, tbody and .card-name.
USABLE_PX = 954         # .foot.top (1061.8) - .head.bottom (107.0)
CONTENT_PX = 942        # USABLE_PX less 12px of slack

# Fixed chrome per page, everything except the table rows themselves. Measured as
# (head.bottom -> card.top) + (card.top -> first row top) + (tbody.bottom -> card-name.bottom).
# The first of those three is the 37px gap under the header rule, which an earlier
# version of this file left out entirely — that is what ran page 3 over the footer.
FIRST_PAGE_CHROME_PX = 557   # site card + SUMMARY + DEVICE TYPE BREAKDOWN + one section
CONT_PAGE_CHROME_PX = 189    # card padding + the italic site name + one section
SECTION_PX = 78              # a SECOND section opened on a page that already has one

ROW_BASE_PX = 11        # a row's vertical padding (5+5) + border
ROW_LINE_PX = 14        # one wrapped line of the 9.2px/1.45 mono table body (13.34, rounded up)
ROW_MIN_PX = 39         # the two-line status pill sets the floor for a device row
EVENT_ROW_PX = 25       # a one-line event row: ROW_BASE + one line

# Characters per line = (column width - 16px padding) / 5.75px, the measured advance
# width of JetBrains Mono at 9.2px. Keep these in step with the <colgroup>s in the
# template: every one of these columns wraps, and the tallest sets the row height.
DEVICE_CHARS = 16       # the 108px DEVICE column
REC_CHARS = 36          # the 227px ENGINEER RECOMMENDATION column
EVENT_CHARS = 25        # the 160px DEVICE column of the LONGEST OUTAGES table
SUMMARY_CHARS = 30      # the 190px DEVICE column of the DOWNTIME SUMMARY table


def _lines(text: str, width: int) -> int:
    return len(textwrap.wrap(text or "", width)) or 1


def device_px(device: dict) -> int:
    """Height of one device row — the tallest of its wrapping columns.

    The recommendation is not the only column that wraps: "Numed RHT External Trash
    Collection Area" takes three lines in the 108px DEVICE column and sets the row
    height on its own. Measuring only the recommendation under-counted those rows,
    packed too many onto a page, and ran the card over the footer.
    """
    lines = max(
        _lines(device.get("name"), DEVICE_CHARS),
        _lines(device.get("recommendation"), REC_CHARS),
    )
    return max(ROW_MIN_PX, ROW_BASE_PX + ROW_LINE_PX * lines)


def event_px(event: dict) -> int:
    """Height of one downtime-event row. Device name and status share one cell."""
    label = f"{event.get('device') or ''} {event.get('status') or ''}".strip()
    return ROW_BASE_PX + ROW_LINE_PX * _lines(label, EVENT_CHARS)


def summary_px(row: dict) -> int:
    """Height of one DOWNTIME SUMMARY row — only the device name wraps."""
    return ROW_BASE_PX + ROW_LINE_PX * _lines(row.get("device"), SUMMARY_CHARS)


def _new_page(site: dict, first: bool) -> dict:
    return {
        "kind": "site",
        "number": 0,            # assigned once every page exists
        "anchor": site["anchor"] if first else None,
        "site": site,
        "first": first,
        "devices": [],
        "summary": [],
        "summary_start": False,
        "events": [],
        "events_start": False,
    }


def paginate(site_details: list[dict]) -> list[dict]:
    """Cover page, then as many pages per site as its devices and events need."""
    pages: list[dict] = [{
        "kind": "cover", "number": 0, "anchor": None, "site": None,
        "first": True, "devices": [], "summary": [], "summary_start": False,
        "events": [], "events_start": False,
    }]

    for site in site_details:
        page = _new_page(site, first=True)
        used = FIRST_PAGE_CHROME_PX
        budget = CONTENT_PX

        for d in site["devices"]:
            h = device_px(d)
            if page["devices"] and used + h > budget:
                pages.append(page)
                page = _new_page(site, first=False)
                used = CONT_PAGE_CHROME_PX
            page["devices"].append(d)
            used += h

        # DOWNTIME SUMMARY, then LONGEST OUTAGES. Each keeps its header with at least
        # one row, else the whole section moves to the next page rather than orphaning
        # the label.
        summary = site.get("downtime") or []
        if summary:
            if used + SECTION_PX + EVENT_ROW_PX > budget:
                pages.append(page)
                page = _new_page(site, first=False)
                used = CONT_PAGE_CHROME_PX
            else:
                used += SECTION_PX
            page["summary_start"] = True
            for row in summary:
                h = summary_px(row)
                if page["summary"] and used + h > budget:
                    pages.append(page)
                    page = _new_page(site, first=False)
                    used = CONT_PAGE_CHROME_PX
                    page["summary_start"] = True
                page["summary"].append(row)
                used += h

        events = site.get("events") or []
        if events:
            if used + SECTION_PX + EVENT_ROW_PX > budget:
                pages.append(page)
                page = _new_page(site, first=False)
                used = CONT_PAGE_CHROME_PX   # already covers one section header
            else:
                used += SECTION_PX           # a second section on this page
            page["events_start"] = True
            for ev in events:
                ev_h = event_px(ev)
                if page["events"] and used + ev_h > budget:
                    pages.append(page)
                    page = _new_page(site, first=False)
                    used = CONT_PAGE_CHROME_PX
                    page["events_start"] = True
                page["events"].append(ev)
                used += ev_h
        else:
            # Still render the section so the empty state ("No downtime events
            # recorded...") appears, if it fits.
            if used + SECTION_PX + EVENT_ROW_PX <= budget:
                page["events_start"] = True

        pages.append(page)

    for i, p in enumerate(pages, start=1):
        p["number"] = i
    return pages
