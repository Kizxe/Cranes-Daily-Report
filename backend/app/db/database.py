"""SQLite connection helper. Single writer process, WAL mode."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import settings

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column patches for databases created before a column existed.

    schema.sql is all CREATE TABLE IF NOT EXISTS, so it can never add a column to a table
    that is already on disk. Each patch checks PRAGMA table_info first, so this is safe to
    run on every startup. Runs after executescript, so the tables are guaranteed to exist.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(remarks)")}
    if "device_id" not in cols:
        # NULL = site-level remark (unchanged behaviour). Non-NULL = that device's engineer
        # recommendation. The ADD COLUMN is legal under foreign_keys=ON only because the
        # default is NULL.
        conn.execute(
            "ALTER TABLE remarks ADD COLUMN device_id INTEGER "
            "REFERENCES devices(id) ON DELETE CASCADE"
        )
    # Kept out of schema.sql: on a pre-migration DB the column doesn't exist yet, and
    # executescript runs as one blob — the whole script would abort.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_remarks_device_date "
        "ON remarks(device_id, report_date) WHERE device_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_remarks_device ON remarks(device_id)")

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(device_groups)")}
    if "tb_trigger_id" not in cols:
        # A site is identified by its trigger device, not its name, so that renaming a
        # site in its YAML is just an edit. ALTER TABLE can't add a UNIQUE column, hence
        # the separate partial index — partial so the pre-backfill NULLs don't collide
        # with each other. config_sync fills it in on the next sync, matching on name
        # that one time.
        conn.execute("ALTER TABLE device_groups ADD COLUMN tb_trigger_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_device_groups_trigger "
        "ON device_groups(tb_trigger_id) WHERE tb_trigger_id IS NOT NULL"
    )

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(device_keys)")}
    if "tb_source_device_id" not in cols:
        # Status keys live on the site's trigger device, not the sensor. NULL keeps
        # the old behaviour (read the key off the device's own tb_device_id).
        conn.execute("ALTER TABLE device_keys ADD COLUMN tb_source_device_id TEXT")


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA_PATH.read_text())
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
