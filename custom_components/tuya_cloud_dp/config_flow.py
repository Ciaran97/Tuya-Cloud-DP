"""Config flow for Tuya Cloud DP Climate (UID-based, LocalTuya-style)."""
from __future__ import annotations

from typing import Any, Dict, Optional, List

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_REGION, CONF_DEVICE_ID
from homeassistant.helpers.selector import selector
import logging

from .const import DOMAIN
from .cloud_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)

# Minimal inputs (match LocalTuya cloud style)
CONF_ACCESS_ID     = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_USER_ID       = "user_id"   # UID from "Link Tuya App Account" in Tuya IoT console

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
    vol.Required(CONF_USER_ID): str,  # UID (linked app account)
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

def _dp_schema(spec_map, status_map, defaults=None) -> vol.Schema:
    defaults = defaults or {}

    def t(v): return (v.get("type") or "").lower()
    nums = [c for c, v in spec_map.items() if t(v) in ("integer", "float", "value")]
    bls  = [c for c, v in spec_map.items() if t(v) == "bool"]
    ens  = [c for c, v in spec_map.items() if t(v) == "enum"]

    def opt_list(codes):
        return [{"value": c, "label": _label(c, spec_map[c].get("type"), status_map.get(c))} for c in codes]

    num_opts  = opt_list(nums)
    bool_opts = opt_list(bls)
    enum_opts = opt_list(ens)

    # ensure "(none)" is a selectable option for optional fields
    none_opt = {"value": "", "label": "(none)"}

    default_set = defaults.get(CONF_SETPOINT_CODE, (nums[0] if nums else ""))

    # Build selectors
    setpoint_sel = selector({"select": {"options": (num_opts or [{"value": "", "label": "(no numeric DPs)"}]), "mode": "dropdown"}})
    curtemp_sel  = selector({"select": {"options": [none_opt] + num_opts, "mode": "dropdown"}})
    power_sel    = selector({"select": {"options": [none_opt] + bool_opts, "mode": "dropdown"}})
    mode_sel     = selector({"select": {"options": [none_opt] + enum_opts, "mode": "dropdown"}})

    return vol.Schema({
        vol.Required(CONF_SETPOINT_CODE, default=default_set): setpoint_sel,
        vol.Optional(CONF_CURTEMP_CODE,  default=defaults.get(CONF_CURTEMP_CODE, "")):  curtemp_sel,
        vol.Optional(CONF_POWER_CODE,    default=defaults.get(CONF_POWER_CODE, "")):    power_sel,
        vol.Optional(CONF_MODE_CODE,     default=defaults.get(CONF_MODE_CODE, "")):     mode_sel,
        vol.Optional(CONF_MIN_TEMP, default=5):  vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
        vol.Optional(CONF_PRECISION, default=1.0): vol.In([0.5, 1.0]),
    })

def _error_schema(msg: str) -> vol.Schema:
    # add a read-only-ish field to display the actual error reason
    return STEP1_SCHEMA.extend({ vol.Optional("error_detail", default=str(msg)): str })

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
            region = user_input[CONF_REGION]
            aid    = user_input[CONF_ACCESS_ID]
            sec    = user_input[CONF_ACCESS_SECRET]
            uid    = user_input[CONF_USER_ID].strip()

            # Try selected region, then the other of (us, eu) as a fallback
            region_order = [region] + ([r for r in ("us","eu") if r != region] if region in ("us","eu") else ["us","eu"])
            last_err = None
            api = None

            for rgn in region_order:
                try:
                    api = TuyaCloudApi(self.hass, rgn, aid, sec)
                    res = await api.grant_type_1()
                    if res != "ok":
                        last_err = res
                        api = None
                        continue

                    devs = await api.list_devices_for_uid(uid)
                    if not devs or not devs.get("success"):
                        last_err = devs
                        api = None
                        continue

                    # success
                    self._cfg1[CONF_REGION] = rgn
                    result = devs.get("result")
                    if isinstance(result, list):
                        self._devices = result
                    else:
                        self._devices = result or []
                    break
                except Exception as e:
                    api = None
                    last_err = str(e)

            if api is None:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema(_readable(last_err or "unknown")),
                    errors={"base": "cannot_connect"},
                )

            if not self._devices:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_error_schema("0 devices for this UID (check link/region)"),
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

        # Fetch spec/status to build DP dropdowns
        try:
            api = TuyaCloudApi(self.hass, self._cfg1[CONF_REGION], self._cfg1[CONF_ACCESS_ID], self._cfg1[CONF_ACCESS_SECRET])
            if await api.grant_type_1() != "ok":
                return self.async_show_form(step_id="pick_device", data_schema=schema, errors={"base": "cannot_connect"})
            spec = await api.device_spec(self._cfg1[CONF_DEVICE_ID])
            status = await api.device_status(self._cfg1[CONF_DEVICE_ID])
            self._spec = _extract_spec(spec)
            self._status = _extract_status(status)
        except Exception as e:
            _LOGGER.exception("pick_device failed: %s", e)
            return self.async_show_form(step_id="pick_device", data_schema=schema, errors={"base": "unknown"})

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

    @callback
    def async_get_options_flow(self, entry: config_entries.ConfigEntry):
        return TuyaCloudDPOptionsFlow(entry)

class TuyaCloudDPOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_init(self, user_input=None) -> FlowResult:
        from .cloud_api import TuyaCloudApi  # lazy import
        data = {**self.entry.data, **(self.entry.options or {})}
        api = TuyaCloudApi(self.hass, data[CONF_REGION], data[CONF_ACCESS_ID], data[CONF_ACCESS_SECRET])
        if await api.grant_type_1() == "ok":
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