from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..services import seed_service

router = APIRouter(prefix="/seed", tags=["seed"])


@router.post("/load")
def load() -> dict:
    """Load seed/sample_report_20260816.json into the DB (offline pipeline testing)."""
    if not settings.seed_file.exists():
        raise HTTPException(404, f"seed file not found: {settings.seed_file}")
    return seed_service.load_seed()
