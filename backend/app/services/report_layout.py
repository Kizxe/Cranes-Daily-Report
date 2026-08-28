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
# render_html(), then read getBoundingClientRect().height for each block in a browser.
# Usable height is .foot.top - .head.bottom = 955px; 10px is held back as slack.
CONTENT_PX = 945

SITE_HEAD_PX = 206      # SITE/SYSTEM TYPE card 145 + margins 4/11/34 + "Expected..." 12
SUMMARY_PX = 82         # SUMMARY label + the 4-up stat row        (first page of a site)
BREAKDOWN_PX = 105      # DEVICE TYPE BREAKDOWN label + chip row   (first page of a site)
CARD_CHROME_PX = 72     # health-card 25+25 padding + the italic site name 11 + margin 11
SECTION_PX = 78         # one in-card section: divider + centred label + 30px navy thead

ROW_BASE_PX = 11        # a row's vertical padding (5+5) + border
ROW_LINE_PX = 14        # one wrapped line of the 9.2px/1.45 mono table body
ROW_MIN_PX = 39         # the two-line status pill sets the floor for a device row
EVENT_ROW_PX = 24
REC_CHARS = 44          # chars per line in the 263px ENGINEER RECOMMENDATION column


def device_px(device: dict) -> int:
    """Height of one device row, driven by how far the recommendation wraps."""
    text = device.get("recommendation") or ""
    lines = len(textwrap.wrap(text, REC_CHARS)) or 1
    return max(ROW_MIN_PX, ROW_BASE_PX + ROW_LINE_PX * lines)


def _new_page(site: dict, first: bool) -> dict:
    return {
        "kind": "site",
        "number": 0,            # assigned once every page exists
        "anchor": site["anchor"] if first else None,
        "site": site,
        "first": first,
        "devices": [],
        "events": [],
        "events_start": False,
    }


def paginate(site_details: list[dict]) -> list[dict]:
    """Cover page, then as many pages per site as its devices and events need."""
    pages: list[dict] = [{
        "kind": "cover", "number": 0, "anchor": None, "site": None,
        "first": True, "devices": [], "events": [], "events_start": False,
    }]

    for site in site_details:
        page = _new_page(site, first=True)
        used = SITE_HEAD_PX + SUMMARY_PX + BREAKDOWN_PX + CARD_CHROME_PX + SECTION_PX
        budget = CONTENT_PX

        for d in site["devices"]:
            h = device_px(d)
            if page["devices"] and used + h > budget:
                pages.append(page)
                page = _new_page(site, first=False)
                used = CARD_CHROME_PX + SECTION_PX
            page["devices"].append(d)
            used += h

        # The DOWNTIME EVENTS section: keep the header with at least one row, else
        # push the whole section to the next page rather than orphaning the label.
        events = site.get("events") or []
        if events:
            if used + SECTION_PX + EVENT_ROW_PX > budget:
                pages.append(page)
                page = _new_page(site, first=False)
                used = CARD_CHROME_PX
            page["events_start"] = True
            used += SECTION_PX
            for ev in events:
                if page["events"] and used + EVENT_ROW_PX > budget:
                    pages.append(page)
                    page = _new_page(site, first=False)
                    used = CARD_CHROME_PX + SECTION_PX
                    page["events_start"] = True
                page["events"].append(ev)
                used += EVENT_ROW_PX
        else:
            # Still render the section so the empty state ("No downtime events
            # recorded...") appears, if it fits.
            if used + SECTION_PX + EVENT_ROW_PX <= budget:
                page["events_start"] = True

        pages.append(page)

    for i, p in enumerate(pages, start=1):
        p["number"] = i
    return pages
