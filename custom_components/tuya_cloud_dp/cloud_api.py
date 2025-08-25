"""Ultra-minimal Tuya Cloud API for UID-based (LocalTuya-style) flow."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

_LOGGER = logging.getLogger(__name__)

ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
}

def resolve_endpoint(region: str) -> str:
    return ENDPOINTS.get((region or "us").lower(), ENDPOINTS["us"])

def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest().upper()

class TuyaCloudApi:
    """Tiny signed client (requests run in executor by HA)."""

    def __init__(self, hass, region: str, access_id: str, access_secret: str) -> None:
        self._hass = hass
        self._endpoint = resolve_endpoint(region)
        self._id = access_id
        self._secret = access_secret
        self._token = ""

    def _headers(self, method: str, path: str, body: Optional[str]) -> Dict[str, str]:
        t = str(int(time.time() * 1000))
        content_sha = hashlib.sha256((body or "").encode("utf-8")).hexdigest()
        # Canonical string per Tuya OpenAPI v2 (no Signature-Headers used)
        payload = f"{self._id}{self._token}{t}{method}\n{content_sha}\n\n/{path.lstrip('/')}"
        headers = {
            "t": t,
            "client_id": self._id,
            "sign_method": "HMAC-SHA256",
            "sign": _sign(payload, self._secret),
        }
        if self._token:
            headers["access_token"] = self._token
        if method == "POST":
            headers["Content-Type"] = "application/json"
        return headers

    async def _req(self, method: str, path: str, *, params=None, body_obj=None):
        # Build path+query deterministically and use it for BOTH signing and the actual URL
        qs = urllib.parse.urlencode(params or {}, doseq=True)
        path_with_query = path if not qs else f"{path}?{qs}"

        body = json.dumps(body_obj) if body_obj is not None else ""
        include_token = not (method.upper() == "GET" and path.startswith("/v1.0/token"))

        # Optional but recommended nonce
        nonce = uuid.uuid4().hex

        headers = self._signed_headers(
            method,
            path_with_query,
            body if method.upper() != "GET" else "",
            include_token,
        )
        headers["nonce"] = nonce

        url = f"{self._endpoint}{path_with_query}"
        _LOGGER.debug("Tuya request: %s %s", method, path_with_query)

        def _do():
            if method.upper() == "GET":
                return requests.get(url, headers=headers)
            if method.upper() == "POST":
                return requests.post(url, headers=headers, data=body)
            if method.upper() == "PUT":
                return requests.put(url, headers=headers, data=body)
            raise ValueError(f"Unsupported method {method}")

        # First attempt
        resp = await self._hass.async_add_executor_job(_do)
        try:
            j = resp.json()
        except Exception:
            j = {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}
        _LOGGER.debug("Tuya response (%s %s): %s", method, path_with_query, json.dumps(j, ensure_ascii=False))

        # If sign invalid (1004), clear token, refresh, and retry once
        if isinstance(j, dict) and str(j.get("code")) == "1004" and not path.startswith("/v1.0/token"):
            self._token = ""
            self._token_expire_ms = 0
            # refresh token
            gt = await self.grant_type_1()
            if gt == "ok":
                # re-sign with new token and resend
                headers = self._signed_headers(
                    method,
                    path_with_query,
                    body if method.upper() != "GET" else "",
                    True,
                )
                headers["nonce"] = uuid.uuid4().hex
                resp = await self._hass.async_add_executor_job(_do)
                try:
                    j = resp.json()
                except Exception:
                    j = {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}
                _LOGGER.debug("Tuya retry response (%s %s): %s", method, path_with_query, json.dumps(j, ensure_ascii=False))

        return resp

    # ---- Auth (project token) ----
    async def grant_type_1(self) -> str:
        """Project token (no user-code)."""
        r = await self._req("GET", "/v1.0/token?grant_type=1")
        if not r.ok:
            return f"HTTP {r.status_code}"
        j = r.json()
        if not j.get("success"):
            return f"Error {j.get('code')}: {j.get('msg')}"
        res = j.get("result") or {}
        self._token = res.get("access_token") or ""
        expire = int(res.get("expire_time") or 0)
        now_ms = int(time.time() * 1000)
        # expire_time may be seconds or milliseconds depending on env; normalize to ms
        self._token_expire_ms = now_ms + (expire if expire > 1_000_000_000 else expire * 1000)
        return "ok"

    # ---- Device list for a linked app account (UID) ----
    async def list_devices_for_uid(self, user_id: str) -> Dict[str, Any]:
        r = await self._req("GET", f"/v1.0/users/{user_id}/devices")
        if not r.ok:
            _LOGGER.warning("list_devices_for_uid HTTP error %s (endpoint=%s)", r.status_code, self._endpoint)
            return {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}
        return r.json()

    async def device_spec(self, device_id: str) -> Dict[str, Any]:
        r = await self._req("GET", f"/v1.0/devices/{device_id}/specifications")
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}

    async def device_functions(self, device_id: str) -> Dict[str, Any]:
        # Some tenants expose writable functions here (sometimes richer than /specifications)
        r = await self._req("GET", f"/v1.0/devices/{device_id}/functions")
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}

    async def device_status(self, device_id: str) -> Dict[str, Any]:
        r = await self._req("GET", f"/v1.0/devices/{device_id}/status")
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}

    async def send_command(self, device_id: str, commands: list[dict[str, Any]]):
        """Send a command to a device via Tuya OpenAPI. Expects commands like: [{"code": "temp_set", "value": 215}]"""
        # Primary (iot-03) endpoint
        #_LOGGER.debug("Sending Command: %s: ", str(commands))
        resp = await self._req(
            "POST",
            f"/v1.0/iot-03/devices/{device_id}/commands",
            body_obj={"commands": commands},
        )
        try:
            j = resp.json()
        except Exception:
            j = {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}

        # Optional fallback: if project isn’t asset-bound yet (1106), try legacy path
        if not j.get("success") and str(j.get("code")) == "1106":
            resp2 = await self._req(
                "POST",
                f"/v1.0/devices/{device_id}/commands",
                body_obj={"commands": commands},
            )
            try:
                j = resp2.json()
            except Exception:
                j = {"success": False, "code": "http", "msg": f"HTTP {resp2.status_code}"}

        return j