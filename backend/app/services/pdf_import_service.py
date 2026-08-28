"""Import an existing PDF report: archive as-received first, extract best-effort.

Build order step 6: "archive first; extraction quality can improve later."
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..db.database import get_conn

_DOC_RE = re.compile(r"DDR-(\d{8})")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def store_pdf(original_name: str, content: bytes) -> dict:
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    stored = settings.uploads_dir / f"{stamp}__{_safe_name(original_name)}"
    stored.write_bytes(content)

    report_date, doc_no, extract = _extract(stored)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO imported_pdfs
                (original_name, stored_path, report_date, doc_number, extracted_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (original_name, str(stored), report_date, doc_no,
             json.dumps(extract) if extract else None),
        )
    return {
        "original_name": original_name,
        "stored_path": str(stored),
        "report_date": report_date,
        "doc_number": doc_no,
        "extracted": bool(extract),
    }


def _extract(path: Path) -> tuple[str | None, str | None, dict | None]:
    try:
        import pdfplumber
    except ImportError:
        return None, None, None
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
            tables = [t for pg in pdf.pages for t in (pg.extract_tables() or [])]
    except Exception:
        return None, None, None

    doc_no = None
    report_date = None
    m = _DOC_RE.search(text)
    if m:
        doc_no = f"DDR-{m.group(1)}"
        report_date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
    return report_date, doc_no, {"text": text, "tables": tables}


def list_imports() -> list[dict]:
    from ..db.database import read_conn

    with read_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, original_name, stored_path, report_date, doc_number, imported_at "
            "FROM imported_pdfs ORDER BY imported_at DESC"
        ).fetchall()]
