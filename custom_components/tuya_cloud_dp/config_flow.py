"""Config flow for Tuya Cloud DP Climate (cloud thermostat via DP mapping)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant import config_entries, exceptions
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_NAME,
    CONF_REGION,
)

import logging

from .const import (
    DOMAIN,
)

# Optional additional keys we store with the entry/options:
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_USER_ID = "user_id"
CONF_USER_CODE = "user_code"
CONF_ENDPOINT = "endpoint"

# DP mapping keys
CONF_SETPOINT_CODE = "setpoint_code"
CONF_CURTEMP_CODE = "curtemp_code"
CONF_POWER_CODE = "power_code"
CONF_MODE_CODE = "mode_code"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_PRECISION = "precision"

_LOGGER = logging.getLogger(__name__)

REGIONS = ["us", "eu", "in", "cn"]

def _api(hass):
    # Lazy import; avoids import-time failures breaking the handler
    from .cloud_api import TuyaCloudApi, resolve_endpoint
    return TuyaCloudApi, resolve_endpoint


# ---------- Small helpers ----------

def _choice_label(code: str, typ: Optional[str], cur: Any) -> str:
    cur_s = "—" if cur is None else f"{cur}"
    t = typ or "unknown"
    return f"{code}  (type: {t}, current: {cur_s})"

def _extract_spec_map(spec_resp: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not spec_resp or not spec_resp.get("success"):
        return out
    for f in (spec_resp.get("result", {}).get("functions") or []):
        code = f.get("code")
        if code:
            out[code] = {"type": f.get("type"), "values": f.get("values")}
    return out

def _extract_status_map(status_resp: Dict[str, Any]) -> Dict[str, Any]:
    res = {}
    if not status_resp or not status_resp.get("success"):
        return res
    for item in (status_resp.get("result") or []):
        code = item.get("code")
        if code:
            res[code] = item.get("value")
    return res

def _build_dp_schema(spec: Dict[str, Dict[str, Any]], status: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> vol.Schema:
    defaults = defaults or {}
    def t(v: Dict[str, Any]) -> str:
        return (v.get("type") or "").lower()

    numbers = [c for c, v in spec.items() if t(v) in ("integer", "float", "value")]
    booleans = [c for c, v in spec.items() if t(v) == "bool"]
    enums = [c for c, v in spec.items() if t(v) == "enum"]

    num_map = { _choice_label(c, spec[c].get("type"), status.get(c)): c for c in numbers }
    cur_map = {"(none)": ""} | num_map
    pow_map = {"(none)": ""} | { _choice_label(c, spec[c].get("type"), status.get(c)): c for c in booleans }
    mode_map = {"(none)": ""} | { _choice_label(c, spec[c].get("type"), status.get(c)): c for c in enums }

    default_set = defaults.get(CONF_SETPOINT_CODE, (numbers[0] if numbers else ""))

    return vol.Schema({
        vol.Required(CONF_SETPOINT_CODE, default=default_set): vol.In(num_map or {"(no numeric DPs found)": ""}),
        vol.Optional(CONF_CURTEMP_CODE, default=defaults.get(CONF_CURTEMP_CODE, "")): vol.In(cur_map),
        vol.Optional(CONF_POWER_CODE,   default=defaults.get(CONF_POWER_CODE,   "")): vol.In(pow_map),
        vol.Optional(CONF_MODE_CODE,    default=defaults.get(CONF_MODE_CODE,    "")): vol.In(mode_map),
        vol.Optional(CONF_MIN_TEMP, default=5): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
        vol.Optional(CONF_PRECISION, default=1.0): vol.In([0.5, 1.0]),
    })


# ---------- Step 1: Cloud setup (QR code user_code supported) ----------

STEP1_SCHEMA = vol.Schema({
    vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
    vol.Optional(CONF_ENDPOINT, default=""): str,      # leave blank normally
    vol.Required(CONF_ACCESS_ID): str,
    vol.Required(CONF_ACCESS_SECRET): str,
    vol.Optional(CONF_USER_ID, default=""): str,       # optional (we can list via associated-users)
    vol.Optional(CONF_USER_CODE, default=""): str,     # user code from Tuya/Smart Life app
})

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Tuya Cloud DP Climate."""
    VERSION = 1

    def __init__(self) -> None:
        self._cfg1: Dict[str, Any] = {}
        self._devices: List[Dict[str, Any]] = []
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_user(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA)

        # Save inputs
        self._cfg1 = dict(user_input)

        TuyaCloudApi, resolve_endpoint = _api(self.hass)
        api = TuyaCloudApi(
            self.hass,
            user_input[CONF_REGION],
            user_input[CONF_ACCESS_ID],
            user_input[CONF_ACCESS_SECRET],
            user_input.get(CONF_USER_ID, ""),
            user_input.get(CONF_ENDPOINT, ""),
        )

        # 1) Get project token
        res = await api.async_get_access_token()
        if res != "ok":
            _LOGGER.warning("Tuya grant_type=1 failed: %s", res)
            return self.async_show_form(
                step_id="user",
                data_schema=STEP1_SCHEMA,
                errors={"base": "cannot_connect"},
                description_placeholders={"msg": res},
            )

        # 2) Optional: exchange user_code → user-bound token (recommended)
        uc = (user_input.get(CONF_USER_CODE) or "").strip()
        if uc:
            res2 = await api.async_exchange_user_code(uc)
            if res2 != "ok":
                _LOGGER.warning("Tuya user_code exchange failed: %s", res2)
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP1_SCHEMA,
                    errors={"base": "cannot_connect"},
                    description_placeholders={"msg": res2},
                )

        # 3) Probe time (quick sanity)
        probe = await api.async_probe_time()
        if not probe or not probe.get("success"):
            msg = f"{probe}"[:300]
            _LOGGER.warning("Tuya /time probe failed: %s", msg)
            return self.async_show_form(
                step_id="user",
                data_schema=STEP1_SCHEMA,
                errors={"base": "cannot_connect"},
                description_placeholders={"msg": msg},
            )

        # 4) List devices (either /users/{uid}/devices OR associated-users/devices)
        res3 = await api.async_get_devices_list()
        if res3 != "ok":
            _LOGGER.warning("Tuya get devices failed: %s", res3)
            return self.async_show_form(
                step_id="user",
                data_schema=STEP1_SCHEMA,
                errors={"base": "no_devices"},
                description_placeholders={"msg": res3},
            )

        self._devices = list(api.device_list.values())

        # cache minimal seed for next step (not strictly required here)
        self.hass.data.setdefault(DOMAIN, {})["cfg_seed"] = self._cfg1

        return await self.async_step_pick_device()

    async def async_step_pick_device(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        choices: Dict[str, str] = {}
        for d in self._devices:
            did = d.get("id") or d.get("device_id")
            if not did:
                continue
            name = d.get("name") or "Unnamed"
            prod = d.get("product_name") or d.get("category") or ""
            choices[f"{name} · {prod} · {did}"] = did

        if not choices:
            return self.async_abort(reason="no_devices")

        schema = vol.Schema({ vol.Required(CONF_DEVICE_ID): vol.In(choices) })

        if user_input is None:
            return self.async_show_form(step_id="pick_device", data_schema=schema)

        self._cfg1[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID]

        # Fetch spec/status so we can build DP dropdowns
        TuyaCloudApi, _ = _api(self.hass)
        api = TuyaCloudApi(
            self.hass,
            self._cfg1[CONF_REGION],
            self._cfg1[CONF_ACCESS_ID],
            self._cfg1[CONF_ACCESS_SECRET],
            self._cfg1.get(CONF_USER_ID, ""),
            self._cfg1.get(CONF_ENDPOINT, ""),
        )
        # tokens again (quick, avoids surprises after restart)
        if await api.async_get_access_token() != "ok":
            return self.async_abort(reason="cannot_connect")
        uc = (self._cfg1.get(CONF_USER_CODE) or "").strip()
        if uc:
            if await api.async_exchange_user_code(uc) != "ok":
                return self.async_abort(reason="cannot_connect")

        spec_resp = await api.async_get_device_spec(self._cfg1[CONF_DEVICE_ID])
        status_resp = await api.async_get_device_status(self._cfg1[CONF_DEVICE_ID])
        self._spec = _extract_spec_map(spec_resp)
        self._status = _extract_status_map(status_resp)

        return await self.async_step_map_dp()

    async def async_step_map_dp(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        schema = _build_dp_schema(self._spec, self._status)
        if user_input is None:
            return self.async_show_form(step_id="map_dp", data_schema=schema)

        data = {**self._cfg1, **user_input}
        title = f"Tuya Cloud DP ({data.get(CONF_DEVICE_ID)})"
        # unique_id by device id (so re-adds replace)
        await self.async_set_unique_id(data.get(CONF_DEVICE_ID))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=data)

    @callback
    def async_get_options_flow(self, entry: config_entries.ConfigEntry):
        return TuyaCloudDPOptionsFlow(entry)


# ---------- Options Flow: edit mapping later ----------

class TuyaCloudDPOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_init(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        data = {**self.entry.data, **(self.entry.options or {})}
        TuyaCloudApi, _ = _api(self.hass)
        api = TuyaCloudApi(
            self.hass,
            data[CONF_REGION],
            data[CONF_ACCESS_ID],
            data[CONF_ACCESS_SECRET],
            data.get(CONF_USER_ID, ""),
            data.get(CONF_ENDPOINT, ""),
        )
        if await api.async_get_access_token() == "ok":
            uc = (data.get(CONF_USER_CODE) or "").strip()
            if uc:
                await api.async_exchange_user_code(uc)
            self._spec = _extract_spec_map(await api.async_get_device_spec(data[CONF_DEVICE_ID]))
            self._status = _extract_status_map(await api.async_get_device_status(data[CONF_DEVICE_ID]))

        defaults = {
            CONF_SETPOINT_CODE: data.get(CONF_SETPOINT_CODE, "temp_set"),
            CONF_CURTEMP_CODE:  data.get(CONF_CURTEMP_CODE, ""),
            CONF_POWER_CODE:    data.get(CONF_POWER_CODE, ""),
            CONF_MODE_CODE:     data.get(CONF_MODE_CODE, ""),
        }
        schema = _build_dp_schema(self._spec, self._status, defaults)

        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        new_opts = {**(self.entry.options or {}), **user_input}
        return self.async_create_entry(title="", data=new_opts)