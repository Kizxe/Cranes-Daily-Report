"""Rebuild a day's downtime events from ThingsBoard history.

    python -m scripts.backfill_downtime --date 2026-08-30              # fill gaps only
    python -m scripts.backfill_downtime --date 2026-08-30 --refresh    # replace the day
    python -m scripts.backfill_downtime --date 2026-08-30 --site NUMed
    python -m scripts.backfill_downtime --date 2026-08-30 --dry-run

The work lives in `backend/app/services/reconcile_service.py` — the scheduler runs the
same code hourly against today, and this is the manual door to it for a past day.

Default is gap-fill: a device that already has a `status_events` row starting on
`--date` is left untouched, so a day the poll loop covered is never overwritten and
re-running is safe. Rows are tagged `source='backfill'`.

`--refresh` rebuilds the day for every device instead, replacing that day's rows with
what ThingsBoard's history says (tagged `source='reconcile'`). Use it when the poll
loop was running but sampled too coarsely — a device that dropped and recovered inside
one poll interval never made it into `status_events` at all.
"""
from __future__ import annotations

import argparse
import asyncio

from backend.app.services import reconcile_service
from backend.app.services.thingsboard_client import tb_client


async def run(date: str, site: str | None, dry_run: bool, lookback: int,
              refresh: bool) -> None:
    try:
        summary = await reconcile_service.reconcile_day(
            date, site=site, refresh=refresh, lookback_days=lookback, dry_run=dry_run,
            trigger="manual",
        )
    finally:
        await tb_client.close()

    if not summary["devices"]:
        print(f"nothing to rebuild for {date}" + (f" at {site}" if site else "")
              + ("" if refresh else " (every device already has events that day —"
                                   " pass --refresh to rebuild anyway)"))
        return

    print(f"{date}: {summary['events']} event(s) reconstructed for "
          f"{summary['devices']} device(s)"
          + (f", {summary['skipped']} skipped (no history)" if summary["skipped"] else ""))
    if dry_run:
        for e in summary["preview"][:20]:
            span = f"{e['start'][11:19]}–{e['end'][11:19]}" if e["end"] \
                else f"{e['start'][11:19]}–(open)"
            print(f"  {e['device'][:28]:30} {e['status']:10} {span}")
        if len(summary["preview"]) > 20:
            print(f"  … {len(summary['preview']) - 20} more")
        print("  (dry run — nothing written)")
    else:
        print(f"wrote {summary['events']} status_events row(s) tagged "
              f"source='{'reconcile' if refresh else 'backfill'}'")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="the day to rebuild, YYYY-MM-DD")
    ap.add_argument("--site", help="limit to one site")
    ap.add_argument("--refresh", action="store_true",
                    help="replace the day's events instead of only filling gaps")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written")
    ap.add_argument("--lookback-days", type=int, default=7,
                    help="how far outside the day to search for the surrounding "
                         "transitions (default 7; a quiet sensor changes rarely)")
    args = ap.parse_args()
    asyncio.run(run(args.date, args.site, args.dry_run, args.lookback_days, args.refresh))


if __name__ == "__main__":
    main()
