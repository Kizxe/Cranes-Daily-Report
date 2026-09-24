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
HISTORY_SECTION_PX = 114     # the separate status-summary card margin, title, and table head

ROW_BASE_PX = 11        # a row's vertical padding (5+5) + border
ROW_LINE_PX = 14        # one wrapped line of the 9.2px/1.45 mono table body (13.34, rounded up)
ROW_MIN_PX = 39         # the two-line status pill sets the floor for a device row
EVENT_ROW_PX = 25       # a one-line event row: ROW_BASE + one line

# Characters per line = (column width - 16px padding) / 5.75px, the measured advance
# width of JetBrains Mono at 9.2px. Keep these in step with the <colgroup>s in the
# template: every one of these columns wraps, and the tallest sets the row height.
DEVICE_CHARS = 16       # the 108px DEVICE column
REC_CHARS = 34          # the ~216px REMARK column (STATUS widened 80->88 on 2026-09-01
                        # so "INACTIVE" stops wrapping onto two lines, which took it from
                        # here — re-measure both together if either column moves again)
EVENT_CHARS = 25        # the 160px DEVICE column of the LONGEST OUTAGES table
SUMMARY_CHARS = 30      # the 190px DEVICE column of the DOWNTIME SUMMARY table
HISTORY_DEVICE_CHARS = 24
HISTORY_STATUS_CHARS = 10
HISTORY_TIME_CHARS = 5

# ---- SITES OVERVIEW, the cover table ------------------------------------------
# It used to render every site in one go, which was fine at 3 sites and ran straight
# over the footer at 16. Its cells are the page-level `tbody td` (11.5px/1.5), NOT the
# smaller `.in-sec` body the constants above describe, so it needs its own set.
COVER_CHROME_PX = 301        # head.bottom -> tbody.top: h1 + subtitle + stats + section + thead
COVER_DRAFT_PX = 71          # the DRAFT banner, when it's there: 29 margin + 42 box
COVER_CONT_CHROME_PX = 106   # a continuation page: section header + thead only

OVERVIEW_LINE_PX = 18        # one wrapped line of the 11.5px/1.5 body (17.25, rounded up)
OVERVIEW_NAME_LINE_PX = 17   # one line of .sitename, 11.5px/1.4 (16.1, rounded up)
OVERVIEW_PILL_PX = 26        # the HEALTHY/ATTENTION pill under the name: 7 margin + 19
OVERVIEW_PAD_PX = 19         # the row's vertical padding (9+9) + border

# Characters per line = (column width - 20.3px padding) / 6.9px, the measured advance
# of JetBrains Mono at 11.5px. Keep in step with the cover table's <colgroup>.
SITE_CHARS = 12         # the 108px SITE column
STATUS_CHARS = 15       # the 129px CURRENT STATUS column
OVERVIEW_REMARK_CHARS = 32   # the ~243px REMARK column, whatever is left over


def _lines(text: str, width: int) -> int:
    return len(textwrap.wrap(text or "", width)) or 1


def overview_px(site: dict) -> int:
    """Height of one SITES OVERVIEW row — the tallest of its wrapping columns.

    The SITE cell is the name stacked over its health pill, so it sets the floor at
    one line (61px) and grows faster than the text columns after that: "Robert Bosch
    Extended Name" wraps to three lines in 108px and is taller than any status
    breakdown beside it.
    """
    name_px = (_lines(site.get("name"), SITE_CHARS) * OVERVIEW_NAME_LINE_PX
               + OVERVIEW_PILL_PX)
    text_lines = max(
        _lines(site.get("status_breakdown"), STATUS_CHARS),
        _lines(site.get("remark") or "—", OVERVIEW_REMARK_CHARS),
    )
    return max(name_px, text_lines * OVERVIEW_LINE_PX) + OVERVIEW_PAD_PX


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


def history_row_px(row: dict) -> int:
    """Height of one row in a timestamp-grouped status table."""
    lines = max(
        _lines(row.get("device"), HISTORY_DEVICE_CHARS),
        _lines(row.get("status"), HISTORY_STATUS_CHARS),
        _lines((row.get("interval_start") or "—")[11:16], HISTORY_TIME_CHARS),
        _lines((row.get("recovered_at") or "—")[11:16], HISTORY_TIME_CHARS),
    )
    return ROW_BASE_PX + ROW_LINE_PX * lines


def _new_page(site: dict, first: bool) -> dict:
    return {
        "kind": "site",
        "number": 0,            # assigned once every page exists
        "anchor": site["anchor"] if first else None,
        "site": site,
        "first": first,
        "devices": [],
        "sites": [],
        "summary": [],
        "summary_start": False,
        "history_groups": [],
        "history_start": False,
        "events": [],
        "events_start": False,
    }


def _new_cover_page(kind: str) -> dict:
    return {
        "kind": kind, "number": 0, "anchor": None, "site": None,
        "first": True, "devices": [], "sites": [], "summary": [],
        "summary_start": False, "history_groups": [], "history_start": False,
        "events": [], "events_start": False,
    }


def _cover_pages(sites: list[dict], is_draft: bool) -> list[dict]:
    """The cover, plus a continuation page per overflow of the SITES OVERVIEW table.

    The cover carries the title block and the stats strip, so it fits far fewer rows
    than a continuation page does — about 10 against 15.
    """
    pages = [_new_cover_page("cover")]
    used = COVER_CHROME_PX + (COVER_DRAFT_PX if is_draft else 0)

    for s in sites:
        h = overview_px(s)
        if pages[-1]["sites"] and used + h > CONTENT_PX:
            pages.append(_new_cover_page("cover-cont"))
            used = COVER_CONT_CHROME_PX
        pages[-1]["sites"].append(s)
        used += h

    for p in pages:
        p["cover_last"] = False
    pages[-1]["cover_last"] = True      # the "click a site name" note goes here
    return pages


def paginate(site_details: list[dict], sites: list[dict] | None = None,
             is_draft: bool = False, downtime_sections: bool = True) -> list[dict]:
    """Cover page, then as many pages per site as its devices and events need.

    `downtime_sections` False leaves DOWNTIME SUMMARY and LONGEST OUTAGES off the
    report (settings.report_downtime_sections). The template gates both on the
    summary_start / events_start flags set below, so not setting them drops the
    sections and the space they'd have claimed in one go.
    """
    pages: list[dict] = _cover_pages(sites or [], is_draft)

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
        summary = (site.get("downtime") or []) if downtime_sections else []
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

        events = (site.get("events") or []) if downtime_sections else []
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
        elif downtime_sections:
            # Still render the section so the empty state ("No downtime events
            # recorded...") appears, if it fits.
            if used + SECTION_PX + EVENT_ROW_PX <= budget:
                page["events_start"] = True

        history_groups = site.get("status_history") or []
        if history_groups:
            # Give each poll timestamp its own page. This keeps the summary readable;
            # only a single timestamp can continue when that one poll has many devices.
            pages.append(page)
            page = _new_page(site, first=False)
            used = CONT_PAGE_CHROME_PX
            page["history_start"] = True
            for group in history_groups:
                group_device = group.get("device") or (
                    group.get("rows") or [{}]
                )[0].get("device", "Unknown device")
                group_height = HISTORY_SECTION_PX + 54 + sum(
                    history_row_px(row) for row in group["rows"]
                )
                if page["history_groups"] and used + group_height > budget:
                    pages.append(page)
                    page = _new_page(site, first=False)
                    used = CONT_PAGE_CHROME_PX
                    page["history_start"] = True
                current = None
                for row in group["rows"]:
                    row_height = history_row_px(row)
                    needs_new_page = (
                        (current is None and used + 54 + row_height > budget)
                        or (current is not None and used + row_height > budget)
                    )
                    if needs_new_page:
                        pages.append(page)
                        page = _new_page(site, first=False)
                        used = CONT_PAGE_CHROME_PX
                        page["history_start"] = True
                        current = None
                    if current is None:
                        current = {"device": group_device, "rows": []}
                        page["history_groups"].append(current)
                        used += HISTORY_SECTION_PX + 54
                    current["rows"].append(row)
                    used += row_height

        pages.append(page)

    for i, p in enumerate(pages, start=1):
        p["number"] = i
    return pages
