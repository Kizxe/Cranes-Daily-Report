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


# --- riding out the instance's stalls ------------------------------------------

@pytest.fixture
def stub_http(monkeypatch):
    """Drive tb_client's transport, with retries but without the backoff wait.

    `_request` rebuilds `self._http` when it finds it closed, which silently discards a
    patch applied to a client another test already closed — so hand it a fresh open one
    and patch that.
    """
    import httpx

    import backend.app.services.thingsboard_client as tbc

    async def instant(_seconds):
        return None

    monkeypatch.setattr(tbc.asyncio, "sleep", instant)
    monkeypatch.setattr(tb_client, "_token", "tok")
    monkeypatch.setattr(tb_client, "_token_exp", 1e18)

    client = httpx.AsyncClient()
    monkeypatch.setattr(tb_client, "_http", client)

    def _patch(handler):
        monkeypatch.setattr(client, "request", handler)

    yield _patch


@pytest.mark.asyncio
async def test_a_stalled_read_is_retried_not_lost(stub_http):
    """One ReadTimeout out of a capture's ~112 requests used to kill the whole run."""
    import httpx

    calls = []

    async def flaky(method, url, headers=None, **kw):
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ReadTimeout("instance stalled")
        return httpx.Response(200, json={"ok": [{"ts": 1, "value": "1"}]},
                              request=httpx.Request(method, url))

    stub_http(flaky)

    out = await tb_client._request("GET", "/whatever")

    assert out == {"ok": [{"ts": 1, "value": "1"}]}
    assert len(calls) == 3, "it gave up before the configured attempts"


@pytest.mark.asyncio
async def test_exhausted_retries_raise_ThingsBoardError_not_httpx(stub_http):
    """The callers degrade one unreadable device on ThingsBoardError. A raw httpx
    exception sails past them and takes the whole capture with it — which is exactly
    what happened on 2026-09-03."""
    import httpx

    from backend.app.services.thingsboard_client import ThingsBoardError

    async def always_stalls(method, url, headers=None, **kw):
        raise httpx.ReadTimeout("instance down")

    stub_http(always_stalls)

    with pytest.raises(ThingsBoardError, match="attempt"):
        await tb_client._request("GET", "/whatever")


@pytest.mark.asyncio
async def test_one_dead_device_does_not_sink_the_whole_capture(stub_http):
    """The behaviour that actually matters: the capture completes, that source empty."""
    import httpx

    from backend.app.services import snapshot_service as snap

    async def dead(method, url, headers=None, **kw):
        raise httpx.ReadTimeout("instance down")

    stub_http(dead)

    rows = await snap._fetch_source({"tb_device_id": "dev-1", "keys": [(7, "active_X")]})

    assert rows == [(7, "active_X", None, None)]
