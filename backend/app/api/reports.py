from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from ..models.schemas import GenerateResult
from ..services import report_service

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("")
def list_reports() -> list[dict]:
    return report_service.list_reports()


@router.post("/{date}/generate", response_model=GenerateResult)
async def generate(date: str):
    """(Re)generate a day's PDF from the latest snapshot for that date.

    This is how a remark added after the 23:59 run gets into the report.
    """
    return await report_service.generate_report(date, trigger="manual")


@router.get("/{date}/preview", response_class=HTMLResponse)
def preview(date: str):
    """Render the report HTML without producing a PDF — fast design iteration."""
    return report_service.render_html(date)


@router.get("/{date}/pdf")
def download_pdf(date: str):
    pdf = report_service.settings.reports_dir / date / f"{report_service.doc_number(date)}.pdf"
    if not Path(pdf).exists():
        raise HTTPException(404, "no PDF for that date yet — POST /reports/{date}/generate")
    return FileResponse(pdf, media_type="application/pdf", filename=pdf.name)
