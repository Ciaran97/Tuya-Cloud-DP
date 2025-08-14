"""Config flow for Tuya Cloud DP Climate (minimal)."""
from __future__ import annotations

from typing import Any, Dict, Optional, List

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_REGION, CONF_DEVICE_ID
import logging

from .const import DOMAIN
from .cloud_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)


def _error_schema(msg: str) -> vol.Schema:
    # read-only-ish display of the error message
    return STEP1_SCHEMA.extend({ vol.Optional("error_detail", default=str(msg)): str })

# Minimal inputs
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_USER_CODE = "user_code"

# DP mapping
CONF_SETPOINT_CODE = "setpoint_code"
CONF_CURTEMP_CODE  = "curtemp_code"
CONF_POWER_CODE    = "power_code"
CONF_MODE_CODE     = "mode_code"
CONF_MIN_TEMP      = "min_temp"
CONF_MAX_TEMP      = "max_temp"
CONF_PRECISION     = "precision"

REGIONS = ["us", "eu", "in", "cn"]

STEP1_SCHEMA = vol.Schema({
    vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
    vol.Required(CONF_ACCESS_ID): str,
    vol.Required(CONF_ACCESS_SECRET): str,
    vol.Required(CONF_USER_CODE): str,  # from Tuya/Smart Life app
})

def _readable(x) -> str:
    if isinstance(x, dict):
        return x.get("msg") or x.get("message") or x.get("code") or str(x)
    return str(x)

def _extract_spec(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not spec or not spec.get("success"):
        return out
    for f in (spec.get("result", {}).get("functions") or []):
        c = f.get("code")
        if c:
            out[c] = {"type": f.get("type"), "values": f.get("values")}
    return out

def _extract_status(status: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not status or not status.get("success"):
        return out
    for i in (status.get("result") or []):
        c = i.get("code")
        if c:
            out[c] = i.get("value")
    return out

def _label(code: str, typ: Optional[str], cur: Any) -> str:
    cur_s = "—" if cur is None else f"{cur}"
    t = typ or "unknown"
    return f"{code} (type:{t}, current:{cur_s})"

def _dp_schema(spec_map: Dict[str, Dict[str, Any]], status_map: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> vol.Schema:
    defaults = defaults or {}
    def t(v): return (v.get("type") or "").lower()
    nums = [c for c,v in spec_map.items() if t(v) in ("integer","float","value")]
    bls  = [c for c,v in spec_map.items() if t(v) == "bool"]
    ens  = [c for c,v in spec_map.items() if t(v) == "enum"]

    num_map  = { _label(c, spec_map[c].get("type"), status_map.get(c)): c for c in nums }
    cur_map  = {"(none)": ""} | num_map
    pow_map  = {"(none)": ""} | { _label(c, spec_map[c].get("type"), status_map.get(c)): c for c in bls }
    mode_map = {"(none)": ""} | { _label(c, spec_map[c].get("type"), status_map.get(c)): c for c in ens }

    default_set = defaults.get(CONF_SETPOINT_CODE, (nums[0] if nums else ""))

    return vol.Schema({
        vol.Required(CONF_SETPOINT_CODE, default=default_set): vol.In(num_map or {"(no numeric DPs)": ""}),
        vol.Optional(CONF_CURTEMP_CODE,  default=defaults.get(CONF_CURTEMP_CODE,"")): vol.In(cur_map),
        vol.Optional(CONF_POWER_CODE,    default=defaults.get(CONF_POWER_CODE,"")):   vol.In(pow_map),
        vol.Optional(CONF_MODE_CODE,     default=defaults.get(CONF_MODE_CODE,"")):    vol.In(mode_map),
        vol.Optional(CONF_MIN_TEMP, default=5):  vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
        vol.Optional(CONF_PRECISION, default=1.0): vol.In([0.5, 1.0]),
    })

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cfg1: Dict[str, Any] = {}
        self._devices: List[Dict[str, Any]] = []
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA)

        try:
            self._cfg1 = dict(user_input)
            region_selected = user_input[CONF_REGION]
            access_id = user_input[CONF_ACCESS_ID]
            access_secret = user_input[CONF_ACCESS_SECRET]
            user_code = user_input[CONF_USER_CODE].strip()

            # Try selected region first, then the other of (us, eu)
            region_order = [region_selected] + ([r for r in ("us","eu") if r != region_selected] if region_selected in ("us","eu") else ["us","eu"])

            last_err = None
            api = None
            for region in region_order:
                try:
                    api = TuyaCloudApi(self.hass, region, access_id, access_secret)
                    res = await api.grant_type_1()
                    if res != "ok":
                        last_err = res
                        continue
                    res2 = await api.exchange_user_code(user_code)
                    if res2 != "ok":
                        last_err = res2
                        continue
                    # success on this region
                    self._cfg1[CONF_REGION] = region
                    break
                except Exception as e:
                    last_err = str(e)
                    api = None

            if api is None:
                # Show the real reason in a field
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema(last_err or "unknown"),
                    errors={"base": "cannot_connect"},
                )

            # Device list
            devs = await api.list_devices()
            if not devs or not devs.get("success"):
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema(devs),
                    errors={"base": "no_devices"},
                )

            result = devs.get("result")
            if isinstance(result, list):
                self._devices = result
            elif isinstance(result, dict):
                self._devices = result.get("list") or []
            else:
                self._devices = []

            if not self._devices:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema("0 devices returned"),
                    errors={"base": "no_devices"},
                )

            return await self.async_step_pick_device()

        except Exception as e:
            _LOGGER.exception("Config step 'user' failed: %s", e)
            return self.async_show_form(
                step_id="user",
                data_schema=_error_schema(e),
                errors={"base": "unknown"},
            )

    async def async_step_pick_device(self, user_input=None) -> FlowResult:
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

        # Fetch spec/status for DP selector
        try:
            api = TuyaCloudApi(self.hass, self._cfg1[CONF_REGION], self._cfg1[CONF_ACCESS_ID], self._cfg1[CONF_ACCESS_SECRET])
            if await api.grant_type_1() != "ok":
                return self._err_form("pick_device", "cannot_connect", "grant_type_1 failed")
            if await api.exchange_user_code(self._cfg1[CONF_USER_CODE].strip()) != "ok":
                return self._err_form("pick_device", "cannot_connect", "user_code exchange failed")

            spec = await api.device_spec(self._cfg1[CONF_DEVICE_ID])
            status = await api.device_status(self._cfg1[CONF_DEVICE_ID])
            self._spec = _extract_spec(spec)
            self._status = _extract_status(status)
        except Exception as e:
            _LOGGER.exception("pick_device failed: %s", e)
            return self._err_form("pick_device", "unknown", e)

        return await self.async_step_map_dp()

    async def async_step_map_dp(self, user_input=None) -> FlowResult:
        schema = _dp_schema(self._spec, self._status)
        if user_input is None:
            return self.async_show_form(step_id="map_dp", data_schema=schema)

        data = {**self._cfg1, **user_input}
        await self.async_set_unique_id(data.get(CONF_DEVICE_ID))
        self._abort_if_unique_id_configured()
        title = f"Tuya Cloud DP ({data.get(CONF_DEVICE_ID)})"
        return self.async_create_entry(title=title, data=data)

    def _err_form(self, step_id: str, base: str, detail: Any) -> FlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP1_SCHEMA if step_id == "user" else vol.Schema({CONF_DEVICE_ID: str}),
            errors={"base": base},
            description_placeholders={"msg": _readable(detail)},
        )

    @callback
    def async_get_options_flow(self, entry: config_entries.ConfigEntry):
        return TuyaCloudDPOptionsFlow(entry)

class TuyaCloudDPOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_init(self, user_input=None) -> FlowResult:
        # Minimal options: just re-offer the DP dropdowns with current values
        from .cloud_api import TuyaCloudApi  # lazy import
        data = {**self.entry.data, **(self.entry.options or {})}
        api = TuyaCloudApi(self.hass, data[CONF_REGION], data[CONF_ACCESS_ID], data[CONF_ACCESS_SECRET])
        if await api.grant_type_1() == "ok":
            await api.exchange_user_code(data[CONF_USER_CODE].strip())
            self._spec = _extract_spec(await api.device_spec(data[CONF_DEVICE_ID]))
            self._status = _extract_status(await api.device_status(data[CONF_DEVICE_ID]))

        defaults = {
            CONF_SETPOINT_CODE: data.get(CONF_SETPOINT_CODE, "temp_set"),
            CONF_CURTEMP_CODE:  data.get(CONF_CURTEMP_CODE, ""),
            CONF_POWER_CODE:    data.get(CONF_POWER_CODE, ""),
            CONF_MODE_CODE:     data.get(CONF_MODE_CODE, ""),
        }
        schema = _dp_schema(self._spec, self._status, defaults)

        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        new_opts = {**(self.entry.options or {}), **user_input}
        return self.async_create_entry(title="", data=new_opts)