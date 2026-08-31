"""Backfill a past day's snapshot from ThingsBoard history.

    python -m scripts.backfill_counters --date 2026-08-30            # all sites
    python -m scripts.backfill_counters --date 2026-08-30 --site NUMed
    python -m scripts.backfill_counters --date 2026-08-30 --dry-run

Why: ACTIVE HRS / AFFECTED HRS in the report are one day's slice of the trigger's
running counters (`forTotalUse_<sensor>` = [inactive_ms, active_ms]), which means the
day needs the PREVIOUS day's capture to difference against. On the first day a site is
configured there isn't one, so the report silently falls back to the thinner
status_events math. This writes that missing baseline by asking ThingsBoard for the
last value of each key at or before 23:59:59 on `--date`.

Only fills gaps: a key already captured for that date is left alone, so re-running is
safe and a real 23:59 capture is never overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.app.config import settings
from backend.app.db.database import get_conn, read_conn
from backend.app.services.thingsboard_client import ThingsBoardError, tb_client

TZ = ZoneInfo(settings.timezone)


def _plan(date: str, site: str | None) -> list[dict]:
    """Keys with no captured value on `date`, grouped by the TB device reporting them."""
    sql = """
        SELECT d.id AS device_id, k.key_name,
               COALESCE(k.tb_source_device_id, d.tb_device_id) AS source_id
        FROM devices d
        JOIN device_keys k ON k.device_id = d.id
        JOIN device_groups g ON g.id = d.group_id
        WHERE COALESCE(k.tb_source_device_id, d.tb_device_id) IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM snapshots s
                           WHERE s.device_id = d.id AND s.key_name = k.key_name
                             AND s.capture_date = ? AND s.value IS NOT NULL)
    """
    params: list = [date]
    if site:
        sql += " AND g.name = ? COLLATE NOCASE"
        params.append(site)
    with read_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    by_source: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append((r["device_id"], r["key_name"]))
    return [{"tb_device_id": k, "keys": v} for k, v in by_source.items()]


async def _history(source: dict, start_ms: int, end_ms: int) -> dict[str, list[dict]]:
    names = sorted({k for _, k in source["keys"]})
    out: dict[str, list[dict]] = {}
    for i in range(0, len(names), tb_client.KEY_BATCH):
        try:
            out.update(await tb_client.timeseries_history(
                source["tb_device_id"], names[i:i + tb_client.KEY_BATCH], start_ms, end_ms))
        except ThingsBoardError as e:
            print(f"  ! {source['tb_device_id']}: {str(e)[:100]}")
    return out


async def run(date: str, site: str | None, dry_run: bool, lookback: int) -> None:
    try:
        await _run(date, site, dry_run, lookback)
    finally:
        await tb_client.close()


async def _run(date: str, site: str | None, dry_run: bool, lookback: int) -> None:
    day_end = datetime.fromisoformat(f"{date}T23:59:59").replace(tzinfo=TZ)
    # A busy sensor writes these on every status change, but a quiet one can go months
    # without a single write — Meatrol DPM last moved in January. Look back far enough
    # to find the value that was actually standing at the end of `date`.
    start_ms = int((day_end - timedelta(days=lookback)).timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)
    capture_ts = day_end.isoformat()

    sources = _plan(date, site)
    if not sources:
        print(f"nothing missing for {date}" + (f" at {site}" if site else ""))
        return

    rows = []
    for source in sources:
        data = await _history(source, start_ms, end_ms)
        for device_id, key in source["keys"]:
            points = data.get(key) or []
            if not points:
                continue
            last = max(points, key=lambda p: p["ts"])       # history comes back ASC
            rows.append((capture_ts, date, "backfill", device_id, key,
                         str(last.get("value")),
                         datetime.fromtimestamp(last["ts"] / 1000, TZ).isoformat()))

    print(f"{date}: {len(rows)} value(s) recovered across {len(sources)} TB device(s)")
    if dry_run:
        for r in rows[:10]:
            print(f"  {r[4][:50]:52} = {r[5][:40]}")
        print("  (dry run — nothing written)")
        return

    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO snapshots
                 (capture_ts, capture_date, trigger, device_id, key_name, value, value_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(capture_ts, device_id, key_name) DO NOTHING""",
            rows,
        )
    print(f"wrote {len(rows)} snapshot row(s) tagged trigger='backfill'")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="the day to backfill, YYYY-MM-DD")
    ap.add_argument("--site", help="limit to one site")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written")
    ap.add_argument("--lookback-days", type=int, default=400,
                    help="how far back to search for the value standing at the end of "
                         "--date (default 400; a quiet sensor may not write for months)")
    args = ap.parse_args()
    asyncio.run(run(args.date, args.site, args.dry_run, args.lookback_days))


if __name__ == "__main__":
    main()
