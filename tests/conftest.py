"""Test fixtures. Each test run gets a throwaway SQLite file.

The env vars are set BEFORE any backend import so config.Settings picks them up.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="cranes-test-"))
os.environ["DB_PATH"] = str(_tmp / "test.db")
os.environ["REPORTS_DIR"] = str(_tmp / "reports")
os.environ["LOGS_DIR"] = str(_tmp / "logs")
os.environ["THINGSBOARD_URL"] = "https://tb.example.test"
os.environ["ENABLE_SCHEDULER"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.db.database import init_db  # noqa: E402
from backend.app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db = Path(os.environ["DB_PATH"])
    for f in db.parent.glob("test.db*"):
        f.unlink()
    init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:   # context manager -> lifespan runs (scheduler, dirs)
        yield c
