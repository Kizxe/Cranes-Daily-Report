"""Load the sample data and render its report — offline, no ThingsBoard.

    python -m scripts.load_seed            # load seed into the DB
    python -m scripts.load_seed --report   # ... then also render the PDF

Use this to exercise steps 2–5 of the roadmap without live cloud access.
"""
from __future__ import annotations

import argparse
import asyncio

from backend.app.db.database import init_db
from backend.app.logging_setup import configure as configure_logging
from backend.app.services import report_service, seed_service


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="also render the PDF for the seed date")
    args = ap.parse_args()

    configure_logging()
    init_db()
    result = seed_service.load_seed()
    print("seed loaded:", result)

    if args.report:
        out = asyncio.run(report_service.generate_report(result["date"], trigger="manual"))
        print("report:", out)


if __name__ == "__main__":
    main()
