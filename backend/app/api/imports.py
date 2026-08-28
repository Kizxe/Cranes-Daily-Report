from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..services import pdf_import_service

router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("")
def list_imports() -> list[dict]:
    return pdf_import_service.list_imports()


@router.post("", status_code=201)
async def import_pdf(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "expected a .pdf file")
    content = await file.read()
    return pdf_import_service.store_pdf(file.filename, content)
