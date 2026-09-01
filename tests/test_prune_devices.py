"""scripts/prune_devices.py — the manual door for a device dropped from the config.

config_sync upserts and never deletes, so without this a device removed from a site's
YAML keeps its DB row and keeps printing in the report.
"""
from __future__ import annotations

import pytest
import yaml

from backend.app.config import settings
from backend.app.db.database import get_conn, read_conn
from scripts import prune_devices
from tests.factories import device, make_site


@pytest.fixture(autouse=True)
def clean_sites_dir():
    """The site files these tests write are theirs alone — config_sync reads the whole
    directory, so leaving one behind would change what an unrelated test syncs."""
    yield
    for f in settings.sites_dir.glob("*.yaml"):
        f.unlink()


def _write_site(name: str, device_names: list[str]) -> None:
    (settings.sites_dir / f"{name.lower()}.yaml").write_text(yaml.safe_dump({
        "name": name,
        "devices": [{"name": d, "device_type": "RHT", "keys": []} for d in device_names],
    }))


def _names() -> list[str]:
    with read_conn() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM devices ORDER BY name")]


def test_dry_run_lists_devices_the_config_no_longer_has(capsys):
    make_site("NUMed", [device("Keep"), device("Flag")])
    _write_site("NUMed", ["Keep"])

    rows = prune_devices._targets("NUMed", [])
    assert [r["name"] for r in rows] == ["Flag"]
    # Its rows travel with it, and the caller is told how many before deleting.
    assert rows[0]["counts"]["device_keys"] > 0
    assert _names() == ["Flag", "Keep"], "a dry run must not delete anything"


def test_apply_deletes_the_device_and_everything_referencing_it():
    ids = make_site("NUMed", [device("Keep"), device("Flag", status="INACTIVE")])
    _write_site("NUMed", ["Keep"])
    flag = ids["Flag"]
    with read_conn() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM status_events WHERE device_id = ?",
                            (flag,)).fetchone()["n"] > 0

    rows = prune_devices._targets("NUMed", [])
    with get_conn() as conn:
        conn.executemany("DELETE FROM devices WHERE id = ?", [(r["id"],) for r in rows])

    assert _names() == ["Keep"]
    with read_conn() as conn:
        for table in prune_devices.CHILD_TABLES:
            left = conn.execute(f"SELECT COUNT(*) n FROM {table} WHERE device_id = ?",
                                (flag,)).fetchone()["n"]
            assert left == 0, f"{table} rows outlived their device"


def test_a_configured_device_is_never_a_candidate():
    make_site("NUMed", [device("Keep"), device("AlsoKeep")])
    _write_site("NUMed", ["Keep", "AlsoKeep"])
    assert prune_devices._targets("NUMed", []) == []


def test_naming_a_device_targets_it_even_though_the_config_still_lists_it():
    make_site("NUMed", [device("Keep"), device("Flag")])
    _write_site("NUMed", ["Keep", "Flag"])
    assert [r["name"] for r in prune_devices._targets("NUMed", ["Flag"])] == ["Flag"]
