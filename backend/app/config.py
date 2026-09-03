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
    # The instance answers most reads in well under a second but stalls unpredictably —
    # measured 2026-09-03: the same call took 0.05s, then 8.4s, then over 30s. A capture
    # is ~112 requests, so without retries a single stall lost the whole run.
    tb_timeout_seconds: float = 30.0
    tb_max_attempts: int = 3          # 1 = no retry
    tb_retry_backoff_seconds: float = 1.0   # doubles each attempt

    # --- Paths (all under the repo root, bind-mounted in Docker) ---
    db_path: Path = REPO_ROOT / "backend" / "data" / "cranes.db"
    device_groups_config: Path = REPO_ROOT / "backend" / "config" / "device_groups.yaml"
    # One file per site: backend/config/sites/<site>.yaml. device_groups.yaml keeps
    # the shared defaults; a site listed in both is taken from its own file.
    sites_dir: Path = REPO_ROOT / "backend" / "config" / "sites"
    reports_dir: Path = REPO_ROOT / "reports"
    uploads_dir: Path = REPO_ROOT / "uploads" / "pdfs"
    template_dir: Path = REPO_ROOT / "templates"
    frontend_dir: Path = REPO_ROOT / "frontend"
    logs_dir: Path = REPO_ROOT / "logs"
    backups_dir: Path = REPO_ROOT / "backups"

    # --- Behaviour ---
    timezone: str = "Asia/Kuala_Lumpur"
    snapshot_hour: int = 23
    snapshot_minute: int = 59
    # Downtime polling cadence (minutes). 0 = never poll on a timer.
    #
    # Both timers are OFF (2026-09-03, user's call): the ThingsBoard instance had gone
    # slow enough to time out a poll outright, and the loop was costing ~64 requests
    # every 5 minutes, ~760 an hour, around the clock. So the app now touches TB only
    # at 23:59 and when someone asks it to.
    #
    # The report does not suffer for it. CURRENT STATUS is read from the 23:59
    # snapshot, not from the poll, and the nightly job reconciles the whole day from
    # TB's own history before rendering — which is what actually catches short
    # outages; the poll only ever sampled them. What you lose is *intraday* freshness:
    # between runs the dashboard and the drill-down show the last capture, not live
    # status. POST /api/downtime/poll and POST /api/captures/run refresh on demand.
    downtime_poll_minutes: int = 0
    # How often today's events are rebuilt from ThingsBoard history. 0 disables it —
    # the nightly job reconciles the day anyway, right before the report is built.
    reconcile_minutes: int = 0
    # How far either side of the day to look for the transitions surrounding it.
    reconcile_lookback_days: int = 7
    # A downtime event is the sensor going quiet for this long. This is what
    # ThingsBoard's own Downtime Events widget shows and what its fault counter counts:
    # the silence, not how long the STATIC/STALLED flag stayed up afterwards. Measured
    # on NUMed 2026-09-01 — 10 min gives 99 events against the triggers' 1D total of
    # 102, where counting flag windows gave 116 that matched nothing.
    downtime_gap_minutes: int = 10
    # Retention: keep everything indefinitely (decided 2026-08-28). No prune job.
    retention_days: int = 0

    # --- Report: the DOWNTIME sections ------------------------------------
    # DOWNTIME SUMMARY and LONGEST OUTAGES are switched off in the PDF for now
    # (2026-09-03, user's call — "later will use for another time"). Nothing is
    # deleted: both are still computed into the context, the drill-down and
    # GET /api/downtime/events/{device_id} are untouched, and the template and
    # page-fitting for them are intact. Set this true to print them again.
    report_downtime_sections: bool = False
    # The PDF prints a per-device summary (one row per device that dropped, always
    # complete) and then this many individual windows, longest first. A day holds ~100
    # windows across a site; listing them all buried the outages that matter and cost
    # three extra pages. Nothing is lost — the summary counts every window, and
    # GET /api/downtime/events/{device_id} still has them one by one.
    report_longest_events: int = 10
    # Debounce. The reconcile pass reads every transition ThingsBoard recorded, and the
    # sensors chatter: on NUMed, 2026-09-01 held 491 non-active windows with a median
    # length of 38s — STATIC/STALLED blips a few seconds long. A window shorter than
    # this is absorbed into the window it interrupted, so ISSUE OCC., the site-detail
    # drill-down and the report's DOWNTIME EVENTS table all speak about real outages.
    # 0 = keep every transition. 120s leaves ~105 windows across the site.
    min_event_seconds: int = 120
    # DEVICE TYPE BREAKDOWN: a device that went down this many times today counts as
    # attention even if it is active right now — a device that flapped 10 times is not
    # "Healthy" just because it happens to be up when the report runs. 0 disables it.
    report_attention_issue_count: int = 5
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
