"""Ultra-minimal Tuya Cloud API used only by the config/option flows."""
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
    return hmac.new(secret.encode("latin-1"), payload.encode("latin-1"), hashlib.sha256).hexdigest().upper()

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
        # No Signature-Headers used; canonical string per Tuya spec
        payload = f"{self._id}{self._token}{t}{method}\n{content_sha}\n\n/{path.lstrip('/')}"
        return {
            "t": t,
            "client_id": self._id,
            "sign_method": "HMAC-SHA256",
            "sign": _sign(payload, self._secret),
            **({"access_token": self._token} if self._token else {}),
        }

    async def _req(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body_obj: Optional[Dict[str, Any]] = None):
        body = json.dumps(body_obj) if body_obj is not None else None
        hdrs = self._headers(method, path, body)
        url = f"{self._endpoint}/{path.lstrip('/')}"
        def _do():
            if method == "GET":
                return requests.get(url, headers=hdrs, params=params, timeout=30)
            if method == "POST":
                return requests.post(url, headers=hdrs, params=params, data=body, timeout=30)
            raise ValueError("Unsupported method")
        return await self._hass.async_add_executor_job(_do)

    async def grant_type_1(self) -> str:
        """Project token (needed before user-code)."""
        r = await self._req("GET", "/v1.0/token", params={"grant_type": 1})
        if not r.ok:
            return f"HTTP {r.status_code}"
        j = r.json()
        if not j.get("success"):
            return f"Error {j.get('code')}: {j.get('msg')}"
        self._token = (j.get("result") or {}).get("access_token", "")
        return "ok"

    async def exchange_user_code(self, user_code: str) -> str:
        """QR/User Code → user-bound token."""
        r = await self._req("GET", "/v1.0/token", params={"grant_type": 2, "code": user_code})
        if not r.ok:
            return f"HTTP {r.status_code}"
        j = r.json()
        if not j.get("success"):
            return f"Error {j.get('code')}: {j.get('msg')}"
        self._token = (j.get("result") or {}).get("access_token", "")
        return "ok"

    async def time_probe(self) -> Dict[str, Any]:
        r = await self._req("GET", "/v1.0/time")
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}

    async def list_devices(self) -> Dict[str, Any]:
        """Associated user’s devices (no user_id required)."""
        r = await self._req("GET", "/v1.0/iot-01/associated-users/devices", params={"page_no": 1, "page_size": 100})
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}

    async def device_spec(self, device_id: str) -> Dict[str, Any]:
        r = await self._req("GET", f"/v1.0/iot-03/devices/{device_id}/specifications")
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}

    async def device_status(self, device_id: str) -> Dict[str, Any]:
        r = await self._req("GET", f"/v1.0/iot-03/devices/{device_id}/status")
        return r.json() if r.ok else {"success": False, "code": "http", "msg": f"HTTP {r.status_code}"}