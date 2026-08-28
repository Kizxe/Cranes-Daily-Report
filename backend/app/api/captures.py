from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..config import settings
from ..models.schemas import CaptureResult
from ..services import snapshot_service

router = APIRouter(prefix="/captures", tags=["captures"])
TZ = ZoneInfo(settings.timezone)


@router.post("/run", response_model=CaptureResult)
async def run_capture(date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Manual snapshot — the 'Import Data' button. Tagged trigger=manual."""
    return await snapshot_service.capture_snapshot(trigger="manual", capture_date=date)


@router.get("/{date}")
def view_capture(date: str) -> dict:
    return {
        "date": date,
        "values": snapshot_service.latest_snapshot_values(date),
    }


@router.get("")
def today() -> dict:
    date = datetime.now(TZ).strftime("%Y-%m-%d")
    return {"date": date, "values": snapshot_service.latest_snapshot_values(date)}
