"""The remarks.device_id migration must not disturb a database created before it."""
import sqlite3

from backend.app.config import settings
from backend.app.db.database import init_db, read_conn

# The remarks DDL exactly as it shipped before device_id existed.
LEGACY_REMARKS = """
CREATE TABLE remarks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL,
    report_date     TEXT NOT NULL,
    body            TEXT NOT NULL,
    author          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _reset_to_legacy() -> None:
    """Drop the migrated table and put the pre-migration one back in its place."""
    conn = sqlite3.connect(settings.db_path)
    conn.executescript("DROP TABLE IF EXISTS remarks;" + LEGACY_REMARKS)
    conn.execute(
        "INSERT INTO remarks (group_id, report_date, body, author) VALUES (1, ?, ?, 'irfan')",
        ("2026-08-16", "pre-existing site remark"),
    )
    conn.commit()
    conn.close()


def test_migrate_adds_device_id_and_keeps_existing_rows():
    _reset_to_legacy()
    with read_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(remarks)")}
    assert "device_id" not in cols          # precondition: we really are on the old shape

    init_db()

    with read_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(remarks)")}
        rows = [dict(r) for r in conn.execute("SELECT * FROM remarks")]
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='remarks'"
        )}
    assert "device_id" in cols
    assert len(rows) == 1
    assert rows[0]["body"] == "pre-existing site remark"
    assert rows[0]["device_id"] is None     # an old remark is a site remark
    assert "uq_remarks_device_date" in idx


def test_init_db_is_idempotent():
    _reset_to_legacy()
    for _ in range(3):
        init_db()
    with read_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM remarks").fetchone()["c"] == 1
