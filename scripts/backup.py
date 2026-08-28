"""Dated zip of cranes.db + reports/ into backups/ (CLAUDE.md guardrail).

    python -m scripts.backup

One file, one folder, one machine, no replication — so copy the produced zip
somewhere off this PC (cron it if you like).
"""
from __future__ import annotations

import zipfile
from datetime import datetime

from backend.app.config import settings


def main() -> None:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = settings.backups_dir / f"cranes-backup-{stamp}.zip"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for db in settings.db_path.parent.glob("cranes.db*"):
            z.write(db, f"data/{db.name}")
        if settings.reports_dir.exists():
            for f in settings.reports_dir.rglob("*"):
                if f.is_file():
                    z.write(f, f"reports/{f.relative_to(settings.reports_dir)}")

    print(f"wrote {out} ({out.stat().st_size / 1_000_000:.1f} MB)")
    print("copy this off the machine.")


if __name__ == "__main__":
    main()
