"""FastAPI entrypoint. Serves the API and the static frontend from one process."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import captures, devices, downtime, imports, remarks, reports
from .config import settings
from .db.database import init_db
from .services import config_sync
from .services.scheduler import shutdown as sched_shutdown
from .services.scheduler import start as sched_start
from .services.thingsboard_client import tb_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cranes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        counts = config_sync.sync_from_yaml()
        log.info("config sync: %s", counts)
    except Exception:  # noqa: BLE001
        log.exception("config sync failed — check device_groups.yaml")
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    sched_start()
    yield
    sched_shutdown()
    await tb_client.close()


app = FastAPI(title="Cranes Daily Report", version="0.1.0", lifespan=lifespan)

for r in (devices.router, captures.router, downtime.router, remarks.router,
          reports.router, imports.router):
    app.include_router(r, prefix="/api")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "tz": settings.timezone, "tb": settings.thingsboard_url}


# Static frontend last, so it doesn't shadow /api.
app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
