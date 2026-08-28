"""Thin async client for ThingsBoard PE REST API.

PE's REST surface closely matches CE but is not guaranteed identical — the paths
here are the common ones; confirm against the live instance when wiring real data
(CLAUDE.md "confirmed decisions"). Auth flow: POST /auth/login -> JWT, refreshed
via /auth/token before expiry.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..config import settings


class ThingsBoardError(RuntimeError):
    pass


class ThingsBoardClient:
    def __init__(self) -> None:
        self._base = settings.tb_base
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_exp: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    # --- auth -------------------------------------------------------------
    async def _login(self) -> None:
        if not settings.thingsboard_username or not settings.thingsboard_password:
            raise ThingsBoardError(
                "THINGSBOARD_USERNAME / THINGSBOARD_PASSWORD not set — see .env.example"
            )
        r = await self._http.post(
            f"{self._base}/auth/login",
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
                r = await self._http.post(
                    f"{self._base}/auth/token",
                    json={"refreshToken": self._refresh_token},
                )
                if r.status_code == 200:
                    data = r.json()
                    self._token = data["token"]
                    self._refresh_token = data.get("refreshToken", self._refresh_token)
                    self._token_exp = time.time() + 9 * 60
                    return
            await self._login()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        await self._ensure_token()
        headers = kw.pop("headers", {})
        headers["X-Authorization"] = f"Bearer {self._token}"
        url = f"{self._base}{path}"
        r = await self._http.request(method, url, headers=headers, **kw)
        if r.status_code == 401:
            # token rejected — force a fresh login once
            self._token = None
            await self._ensure_token()
            headers["X-Authorization"] = f"Bearer {self._token}"
            r = await self._http.request(method, url, headers=headers, **kw)
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
    async def latest_timeseries(self, device_id: str, keys: list[str]) -> dict[str, list[dict]]:
        """{'status': [{'ts': 1690000000000, 'value': 'ACTIVE'}], ...}"""
        return await self._request(
            "GET",
            f"/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
            params={"keys": ",".join(keys)},
        )

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
