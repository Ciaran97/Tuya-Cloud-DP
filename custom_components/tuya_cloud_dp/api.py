"""Minimal Tuya Cloud API for Tuya Cloud DP (thermostat) config/option flows."""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

_LOGGER = logging.getLogger(__name__)

ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
}


def resolve_endpoint(region_code: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    return ENDPOINTS.get((region_code or "us").lower(), ENDPOINTS["us"])


def _calc_sign(msg: str, key: str) -> str:
    return (
        hmac.new(msg=msg.encode("latin-1"), key=key.encode("latin-1"), digestmod=hashlib.sha256)
        .hexdigest()
        .upper()
    )


class TuyaCloudApi:
    """Tiny signed Tuya OpenAPI client for config/option flows."""

    def __init__(self, hass, region: str, client_id: str, client_secret: str, user_id: str = "", endpoint: str = ""):
        self._hass = hass
        self._endpoint = resolve_endpoint(region, endpoint)
        self._client_id = client_id or ""
        self._secret = client_secret or ""
        self._user_id = user_id or ""
        self._access_token = ""  # set by async_exchange_user_code or async_get_access_token
        self.device_list: Dict[str, Dict[str, Any]] = {}

    # ---------- Low-level signed request ----------

    def _signed_headers(self, method: str, url_path: str, body: Optional[str], extra_headers: Optional[Dict[str, str]] = None):
        ts = str(int(time.time() * 1000))
        content_sha256 = hashlib.sha256((body or "").encode("utf-8")).hexdigest()

        # Build canonical headers string based on Signature-Headers
        headers = {
            "t": ts,
            "client_id": self._client_id,
            "sign_method": "HMAC-SHA256",
        }
        if extra_headers:
            headers.update(extra_headers)

        sig_headers = headers.get("Signature-Headers", "")
        canon = "".join(f"{k}:{headers[k]}\n" for k in sig_headers.split(":") if k and k in headers)

        # Build payload to sign (see Tuya docs)
        payload = (
            f"{self._client_id}{self._access_token}{ts}"
            f"{method}\n{content_sha256}\n{canon}\n/{url_path.lstrip('/')}"
        )
        headers["sign"] = _calc_sign(payload, self._secret)
        if self._access_token:
            headers["access_token"] = self._access_token
        return headers

    async def _async_request(self, method: str, url_path: str, body_obj: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None):
        body = json.dumps(body_obj) if body_obj is not None else None
        headers = self._signed_headers(method, url_path, body, extra_headers={})
        full_url = f"{self._endpoint}/{url_path.lstrip('/')}"

        def _do():
            if method == "GET":
                return requests.get(full_url, headers=headers, params=params, timeout=30)
            elif method == "POST":
                return requests.post(full_url, headers=headers, params=params, data=body, timeout=30)
            elif method == "PUT":
                return requests.put(full_url, headers=headers, params=params, data=body, timeout=30)
            else:
                raise ValueError(f"Unsupported method {method}")

        resp = await self._hass.async_add_executor_job(_do)
        return resp

    # ---------- Auth helpers ----------

    async def async_get_access_token(self) -> str:
        """grant_type=1 bootstrap (project token). Often not enough alone."""
        resp = await self._async_request("GET", "/v1.0/token", params={"grant_type": 1})
        if not resp.ok:
            return f"HTTP {resp.status_code}"
        data = resp.json()
        if not data.get("success"):
            return f"Error {data.get('code')}: {data.get('msg')}"
        self._access_token = (data.get("result") or {}).get("access_token", "")
        return "ok"

    async def async_exchange_user_code(self, user_code: str) -> str:
        """QR/User-Code auth: GET /v1.0/token?grant_type=2&code=..."""
        resp = await self._async_request("GET", "/v1.0/token", params={"grant_type": 2, "code": user_code})
        if not resp.ok:
            return f"HTTP {resp.status_code}"
        data = resp.json()
        if not data.get("success"):
            return f"Error {data.get('code')}: {data.get('msg')}"
        self._access_token = (data.get("result") or {}).get("access_token", "")
        return "ok"

    # ---------- Convenience wrappers used by config/option flow ----------

    async def async_probe_time(self) -> Dict[str, Any]:
        resp = await self._async_request("GET", "/v1.0/time")
        return resp.json() if resp.ok else {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}

    async def async_get_devices_list(self) -> str:
        """List devices associated with the authorized user (needs user-bound token)."""
        if not self._user_id:
            # Fallback: associated-users list (no explicit user_id)
            resp = await self._async_request("GET", "/v1.0/iot-01/associated-users/devices", params={"page_no": 1, "page_size": 100})
            if not resp.ok:
                return f"HTTP {resp.status_code}"
            data = resp.json()
            if not data.get("success"):
                return f"Error {data.get('code')}: {data.get('msg')}"
            result = data.get("result")
            if isinstance(result, list):
                devices = result
            elif isinstance(result, dict):
                devices = result.get("list") or []
            else:
                devices = []
        else:
            # LocalTuya-style explicit user_id
            resp = await self._async_request("GET", f"/v1.0/users/{self._user_id}/devices")
            if not resp.ok:
                return f"HTTP {resp.status_code}"
            data = resp.json()
            if not data.get("success"):
                return f"Error {data.get('code')}: {data.get('msg')}"
            devices = data.get("result") or []

        self.device_list = {d.get("id") or d.get("device_id"): d for d in devices if (d.get("id") or d.get("device_id"))}
        return "ok"

    async def async_get_device_status(self, device_id: str) -> Dict[str, Any]:
        resp = await self._async_request("GET", f"/v1.0/iot-03/devices/{device_id}/status")
        if not resp.ok:
            return {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}
        return resp.json()

    async def async_get_device_spec(self, device_id: str) -> Dict[str, Any]:
        resp = await self._async_request("GET", f"/v1.0/iot-03/devices/{device_id}/specifications")
        if not resp.ok:
            return {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}
        return resp.json()