"""Send logs to both stdout and logs/scheduler.log.

The 23:59 job runs unattended — a silent failure with no one watching is the
main real risk of this design (CLAUDE.md guardrails), so everything the
scheduler and the capture/report services do is on disk too.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import settings

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_FMT))
        root.addHandler(stream)

    logfile = settings.logs_dir / "scheduler.log"
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fileh = RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=5)
        fileh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fileh)
