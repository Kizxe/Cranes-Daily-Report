"""Request/response models for the API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RemarkIn(BaseModel):
    group_id: int
    report_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    body: str
    author: str | None = None


class RemarkOut(RemarkIn):
    id: int
    created_at: str
    updated_at: str


class FollowupIn(BaseModel):
    group_id: int
    report_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    device_id: int | None = None
    issue: str
    remark: str
    assigned_pic: str
    date_assigned: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class FollowupOut(FollowupIn):
    id: int
    created_at: str
    device_name: str | None = None


class CaptureResult(BaseModel):
    capture_ts: str
    capture_date: str
    trigger: str
    devices: int
    values: int


class GenerateResult(BaseModel):
    date: str
    status: str
    pdf_path: str
    error: str | None = None
