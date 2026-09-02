"""Reading telemetry keys whose *names* contain commas.

ThingsBoard splits the `keys` query parameter on commas server-side, so a sensor
named `DPM Chiller 7,8,13` (Fuji) or `West Third Floor (T09,T10,...)` (BSC) is
unreadable through the filtered endpoint — it comes back as two fragments with
value None, and the device reads UNKNOWN forever with nothing in the logs.
Percent-encoding doesn't help: TB decodes before it splits. The unfiltered read
has no parameter to split, so those keys are picked out of a full fetch instead.
"""
from __future__ import annotations

import pytest

from backend.app.services.thingsboard_client import tb_client

PLAIN = "active_Cafeteria"
COMMAED = "active_West Third Floor (T09,T10,T11)"


@pytest.fixture
def calls(monkeypatch):
    """Record every request and answer as ThingsBoard would."""
    seen: list[dict | None] = []

    async def fake_request(method, path, **kw):
        params = kw.get("params")
        seen.append(params)
        if params and "keys" in params:
            # TB splits on commas, so a comma-named key matches nothing.
            return {k: [{"ts": 1, "value": None}] if k not in (PLAIN,)
                    else [{"ts": 1, "value": "true"}]
                    for k in params["keys"].split(",")}
        return {PLAIN: [{"ts": 1, "value": "true"}],
                COMMAED: [{"ts": 2, "value": "false"}]}

    monkeypatch.setattr(tb_client, "_request", fake_request)
    return seen


@pytest.mark.asyncio
async def test_comma_named_key_is_read_from_the_unfiltered_fetch(calls):
    out = await tb_client.latest_timeseries("dev-1", [PLAIN, COMMAED])

    assert out[COMMAED] == [{"ts": 2, "value": "false"}]
    assert out[PLAIN] == [{"ts": 1, "value": "true"}]
    # One filtered batch for the plain key, one unfiltered fetch for the comma one.
    assert calls == [{"keys": PLAIN}, None]


@pytest.mark.asyncio
async def test_no_unfiltered_fetch_when_every_key_is_plain(calls):
    out = await tb_client.latest_timeseries("dev-1", [PLAIN])

    assert out == {PLAIN: [{"ts": 1, "value": "true"}]}
    assert calls == [{"keys": PLAIN}]


@pytest.mark.asyncio
async def test_comma_keys_absent_upstream_are_dropped_not_faked(calls, monkeypatch):
    """A key TB doesn't have must be missing, not present with a None value."""
    async def only_plain(method, path, **kw):
        calls.append(kw.get("params"))
        return {PLAIN: [{"ts": 1, "value": "true"}]}

    monkeypatch.setattr(tb_client, "_request", only_plain)
    out = await tb_client.latest_timeseries("dev-1", [COMMAED])

    assert COMMAED not in out
