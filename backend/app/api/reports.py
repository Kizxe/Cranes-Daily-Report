from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from ..config import settings
from ..models.schemas import GenerateResult
from ..services import report_service

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("")
def list_reports() -> list[dict]:
    return report_service.list_reports()


@router.post("/{date}/generate", response_model=GenerateResult)
async def generate(date: str, allow_empty: bool = Query(default=False)):
    """(Re)generate a day's PDF from the latest snapshot for that date.

    This is how a remark added after the 23:59 run gets into the report.
    Refuses to build an empty report unless allow_empty=true.
    """
    if not allow_empty and not report_service.has_snapshot(date):
        raise HTTPException(
            409, f"no snapshot data for {date} — capture first, or pass allow_empty=true"
        )
    return await report_service.generate_report(date, trigger="manual")


@router.get("/{date}/preview", response_class=HTMLResponse)
def preview(date: str):
    """Render the report HTML without producing a PDF — fast design iteration."""
    return report_service.render_html(date)


@router.get("/{date}/pdf")
def download_pdf(date: str):
    pdf = settings.report_pdf_path(date)
    if not pdf.exists():
        raise HTTPException(404, "no PDF for that date yet — POST /reports/{date}/generate")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)
