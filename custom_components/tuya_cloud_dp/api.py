# custom_components/tuya_cloud_dp/api.py
from __future__ import annotations
from typing import Dict, Any, List
from tuya_iot import TuyaOpenAPI

ENDPOINTS = {
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
}

def resolve_endpoint(region: str) -> str:
    return ENDPOINTS.get((region or "eu").lower(), ENDPOINTS["eu"])

def connect_sync(endpoint: str, access_id: str, access_secret: str) -> TuyaOpenAPI:
    api = TuyaOpenAPI(endpoint, access_id, access_secret)
    api.connect()  # call this in executor from HA
    return api

def authorized_login_sync(api: TuyaOpenAPI, user_code: str) -> Dict[str, Any]:
    return api.post("/v1.0/iot-01/associated-users/actions/authorized-login", {"user_code": user_code})

def get_user_devices_sync(api: TuyaOpenAPI) -> List[Dict[str, Any]]:
    """Return list of devices associated with the authorized user."""
    res = api.get("/v1.0/iot-01/associated-users/devices", params={"page_no": 1, "page_size": 100})
    if not res or not res.get("success"):
        return []
    return res.get("result") or []

def get_status_sync(api: TuyaOpenAPI, device_id: str) -> Dict[str, Any]:
    res = api.get(f"/v1.0/iot-03/devices/{device_id}/status")
    out = {}
    if res and res.get("success"):
        for item in (res.get("result") or []):
            code = item.get("code")
            if code:
                out[code] = item.get("value")
    return out

def get_spec_sync(api: TuyaOpenAPI, device_id: str) -> Dict[str, Dict[str, Any]]:
    res = api.get(f"/v1.0/iot-03/devices/{device_id}/specifications")
    out: Dict[str, Dict[str, Any]] = {}
    if res and res.get("success"):
        for f in (res.get("result", {}).get("functions") or []):
            code = f.get("code")
            if code:
                out[code] = {"type": f.get("type"), "values": f.get("values")}
    return out