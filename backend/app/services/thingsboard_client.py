"""Thin async client for ThingsBoard PE REST API.

PE's REST surface closely matches CE but is not guaranteed identical — the paths
here are the common ones; confirm against the live instance when wiring real data
(CLAUDE.md "confirmed decisions"). Auth flow: POST /auth/login -> JWT, refreshed
via /auth/token before expiry.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..config import settings


class ThingsBoardError(RuntimeError):
    pass


log = logging.getLogger("cranes.thingsboard")


class ThingsBoardClient:
    def __init__(self) -> None:
        self._base = settings.tb_base
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_exp: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=settings.tb_timeout_seconds)

    async def close(self) -> None:
        await self._http.aclose()

    # --- auth -------------------------------------------------------------
    async def _login(self) -> None:
        if not settings.thingsboard_username or not settings.thingsboard_password:
            raise ThingsBoardError(
                "THINGSBOARD_USERNAME / THINGSBOARD_PASSWORD not set — see .env.example"
            )
        # Retried like every other call: a stall here fails the whole run, not one device.
        r = await self._send(
            "POST", f"{self._base}/auth/login", {},
            json={
                "username": settings.thingsboard_username,
                "password": settings.thingsboard_password,
            },
        )
        if r.status_code != 200:
            raise ThingsBoardError(f"login failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        self._token = data["token"]
        self._refresh_token = data.get("refreshToken")
        # JWT lifetime is instance-configured; refresh a bit early regardless.
        self._token_exp = time.time() + 9 * 60

    async def _ensure_token(self) -> None:
        async with self._lock:
            if self._token and time.time() < self._token_exp:
                return
            if self._refresh_token:
                try:
                    r = await self._send("POST", f"{self._base}/auth/token", {},
                                         json={"refreshToken": self._refresh_token})
                except ThingsBoardError:
                    r = None      # fall through to a full login rather than give up
                if r is not None and r.status_code == 200:
                    data = r.json()
                    self._token = data["token"]
                    self._refresh_token = data.get("refreshToken", self._refresh_token)
                    self._token_exp = time.time() + 9 * 60
                    return
            await self._login()

    async def _send(self, method: str, url: str, headers: dict, **kw: Any) -> httpx.Response:
        """One request, retried through the instance's stalls.

        httpx raises TransportError (ReadTimeout, ConnectError, ...) rather than
        returning a response, and it is NOT a ThingsBoardError — so before this existed
        it sailed straight past the `except ThingsBoardError` in snapshot_service and
        downtime_service that exist to let one unreadable device degrade to empty
        values. One stalled read out of a capture's ~112 therefore killed the whole
        run: three failed manual captures at 17:20 on 2026-09-03, all ReadTimeout.

        Retrying is the right shape here rather than a longer timeout — the same call
        measured 0.05s, 8.4s and >30s minutes apart, so it is a stall to ride out, not
        a slow response to wait for. Exhausted retries raise ThingsBoardError, which
        the callers already know how to degrade.
        """
        attempts = max(1, settings.tb_max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return await self._http.request(method, url, headers=headers, **kw)
            except httpx.TransportError as e:   # covers ReadTimeout, ConnectError, ...
                if attempt == attempts:
                    raise ThingsBoardError(
                        f"{method} {url} failed after {attempts} attempt(s): "
                        f"{type(e).__name__}"
                    ) from e
                delay = settings.tb_retry_backoff_seconds * 2 ** (attempt - 1)
                log.warning("%s %s: %s — retry %d/%d in %.1fs",
                            method, url.rsplit("/", 1)[-1][:60], type(e).__name__,
                            attempt, attempts - 1, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")   # pragma: no cover

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        if self._http.is_closed:
            # close() runs on app shutdown; a later call (a second app lifespan in
            # one process, or a manual capture during shutdown) must not die on a
            # closed pool.
            self._http = httpx.AsyncClient(timeout=settings.tb_timeout_seconds)
        await self._ensure_token()
        headers = kw.pop("headers", {})
        headers["X-Authorization"] = f"Bearer {self._token}"
        url = f"{self._base}{path}"
        r = await self._send(method, url, headers, **kw)
        if r.status_code == 401:
            # token rejected — force a fresh login once
            self._token = None
            await self._ensure_token()
            headers["X-Authorization"] = f"Bearer {self._token}"
            r = await self._send(method, url, headers, **kw)
        if r.status_code >= 400:
            raise ThingsBoardError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    # --- discovery (build-order step 1) ---------------------------------
    async def entity_groups(self, group_type: str = "DEVICE") -> list[dict]:
        """All entity groups of a type: DEVICE | ALARM | ... (PE feature)."""
        return await self._request("GET", f"/entityGroups/{group_type}")

    async def group_devices(self, entity_group_id: str, limit: int = 500) -> list[dict]:
        data = await self._request(
            "GET",
            f"/entityGroup/{entity_group_id}/devices",
            params={"pageSize": limit, "page": 0, "sortProperty": "name", "sortOrder": "ASC"},
        )
        return data.get("data", data) if isinstance(data, dict) else data

    async def timeseries_keys(self, device_id: str) -> list[str]:
        return await self._request(
            "GET", f"/plugins/telemetry/DEVICE/{device_id}/keys/timeseries"
        )

    # --- values --------------------------------------------------------
    # A trigger device carries every sensor's keys, so a "one device" read can be
    # hundreds of keys. Comma-joined they overflow the request line and TB answers
    # 400 with an HTML error page, so split into batches.
    KEY_BATCH = 30

    async def latest_timeseries(self, device_id: str, keys: list[str]) -> dict[str, list[dict]]:
        """{'status': [{'ts': 1690000000000, 'value': 'ACTIVE'}], ...}"""
        # Some sensors are named with commas in them — `DPM Chiller 7,8,13`, BSC's
        # `West Third Floor (T09,T10,...)`. ThingsBoard splits the `keys` parameter on
        # commas server-side (percent-encoding doesn't help, it decodes first), so such
        # a key comes back as two fragments with value None and the device silently
        # reads UNKNOWN forever. The unfiltered read has no `keys` to split, so fetch
        # every key once and pick those out.
        plain = [k for k in keys if "," not in k]
        commaed = [k for k in keys if "," in k]

        out: dict[str, list[dict]] = {}
        for i in range(0, len(plain), self.KEY_BATCH):
            out.update(
                await self._request(
                    "GET",
                    f"/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
                    params={"keys": ",".join(plain[i : i + self.KEY_BATCH])},
                )
            )
        if commaed:
            everything = await self._request(
                "GET", f"/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
            )
            out.update({k: everything[k] for k in commaed if k in everything})
        return out

    async def latest_attributes(self, device_id: str, keys: list[str] | None = None) -> list[dict]:
        params = {"keys": ",".join(keys)} if keys else {}
        return await self._request(
            "GET",
            f"/plugins/telemetry/DEVICE/{device_id}/values/attributes",
            params=params,
        )

    async def timeseries_history(
        self, device_id: str, keys: list[str], start_ms: int, end_ms: int, limit: int = 50000
    ) -> dict[str, list[dict]]:
        return await self._request(
            "GET",
            f"/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
            params={
                "keys": ",".join(keys),
                "startTs": start_ms,
                "endTs": end_ms,
                "limit": limit,
                "orderBy": "ASC",
            },
        )


# Module-level singleton — the app has one backend process.
tb_client = ThingsBoardClient()
