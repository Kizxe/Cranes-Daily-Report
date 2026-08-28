"""Central configuration. Values come from environment / .env — never hard-code secrets."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (backend/app/config.py -> repo/)
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- ThingsBoard (PE, cloud-hosted) ---
    thingsboard_url: str = "https://your-instance.thingsboard.cloud"
    thingsboard_username: str = ""
    thingsboard_password: str = ""
    # PE REST base is usually <url>/api. Confirm against the live instance.
    thingsboard_api_prefix: str = "/api"

    # --- Paths (all under the repo root, bind-mounted in Docker) ---
    db_path: Path = REPO_ROOT / "backend" / "data" / "cranes.db"
    device_groups_config: Path = REPO_ROOT / "backend" / "config" / "device_groups.yaml"
    reports_dir: Path = REPO_ROOT / "reports"
    uploads_dir: Path = REPO_ROOT / "uploads" / "pdfs"
    template_dir: Path = REPO_ROOT / "templates"
    frontend_dir: Path = REPO_ROOT / "frontend"
    logs_dir: Path = REPO_ROOT / "logs"
    backups_dir: Path = REPO_ROOT / "backups"
    seed_file: Path = REPO_ROOT / "seed" / "sample_report_20260816.json"

    # --- Behaviour ---
    timezone: str = "Asia/Kuala_Lumpur"
    snapshot_hour: int = 23
    snapshot_minute: int = 59
    # Downtime polling cadence (minutes). CLAUDE.md: 5–15 min is fine.
    downtime_poll_minutes: int = 10
    # Retention: keep everything indefinitely (decided 2026-08-28). No prune job.
    retention_days: int = 0

    # --- Report: DOWNTIME EVENTS table caps -------------------------------
    # A flapping device can produce dozens of events a day. Cap what the PDF prints;
    # nothing is lost — GET /api/downtime/events/{device_id} still has them all.
    report_max_events_per_device: int = 8
    report_max_events_per_site: int = 40
    # 0 = print every event. Raise to ~60 to suppress sub-minute poll flaps.
    report_min_event_seconds: int = 0
    # Set false in tests — APScheduler's AsyncIOScheduler binds the running loop.
    enable_scheduler: bool = True

    @property
    def tb_base(self) -> str:
        return self.thingsboard_url.rstrip("/") + self.thingsboard_api_prefix

    def report_pdf_path(self, date: str) -> Path:
        return self.reports_dir / date / f"Cranes_Daily_Report_{date}.pdf"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
