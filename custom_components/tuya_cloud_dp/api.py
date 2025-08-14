from __future__ import annotations
from typing import Dict, Any, List
from tuya_iot import TuyaOpenAPI

ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
}

def resolve_endpoint(region: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    return ENDPOINTS.get((region or "us").lower(), ENDPOINTS["us"])

def connect_sync(endpoint: str, access_id: str, access_secret: str) -> TuyaOpenAPI:
    api = TuyaOpenAPI(endpoint, access_id, access_secret)
    api.connect()  # sync networking; call from executor in HA code
    return api

def get_status_sync(api: TuyaOpenAPI, device_id: str) -> Dict[str, Any]:
    res = api.get(f"/v1.0/iot-03/devices/{device_id}/status", None, None)
    if not res or not res.get("success"):
        return {}
    out = {}
    for item in (res.get("result") or []):
        code = item.get("code")
        if code:
            out[code] = item.get("value")
    return out

def get_spec_sync(api: TuyaOpenAPI, device_id: str) -> Dict[str, Dict[str, Any]]:
    """Return {code: {type, values...}} from device specifications."""
    res = api.get(f"/v1.0/iot-03/devices/{device_id}/specifications", None, None)
    if not res or not res.get("success"):
        return {}
    result = res.get("result") or {}
    functions: List[Dict[str, Any]] = result.get("functions") or []
    out: Dict[str, Dict[str, Any]] = {}
    for f in functions:
        code = f.get("code")
        if code:
            out[code] = {
                "type": f.get("type"),
                "values": f.get("values"),
            }
    return out