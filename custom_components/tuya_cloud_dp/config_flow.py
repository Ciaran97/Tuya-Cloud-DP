from __future__ import annotations
import json
import voluptuous as vol
from typing import Dict, Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_REGION, CONF_DEVICE_ID,
    CONF_SETPOINT_CODE, CONF_MODE_CODE, CONF_POWER_CODE, CONF_CURTEMP_CODE,
    CONF_MIN_TEMP, CONF_MAX_TEMP, CONF_PRECISION, CONF_ENDPOINT
)
from .api import resolve_endpoint, connect_sync, get_spec_sync, get_status_sync

REGIONS = ["us", "eu", "in", "cn"]

STEP1_SCHEMA = vol.Schema({
    vol.Required(CONF_ACCESS_ID): str,
    vol.Required(CONF_ACCESS_SECRET): str,
    vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
    vol.Optional(CONF_ENDPOINT, default=""): str,  # allow explicit override
    vol.Required(CONF_DEVICE_ID): str,
})

def _choice_label(code: str, typ: str | None, cur: Any) -> str:
    cur_s = f"{cur}" if cur is not None else "—"
    t = typ or "unknown"
    return f"{code}  (type: {t}, current: {cur_s})"

def _build_step2_schema(spec: Dict[str, Dict[str, Any]], status: Dict[str, Any], defaults: Dict[str, str] | None = None):
    defaults = defaults or {}
    # Filter by likely types
    number_codes = [c for c, v in spec.items() if (v.get("type") or "").lower() == "integer" or (v.get("type") or "").lower() == "float" or (v.get("type") or "").lower() == "value"]
    bool_codes   = [c for c, v in spec.items() if (v.get("type") or "").lower() == "bool"]
    enum_codes   = [c for c, v in spec.items() if (v.get("type") or "").lower() == "enum"]

    # Map to {label: code}
    def map_choices(codes):
        return { _choice_label(c, (spec[c].get("type")), status.get(c)): c for c in codes }

    setpoint_choices = map_choices(number_codes)
    curtemp_choices  = {"(none)": ""} | map_choices(number_codes)
    power_choices    = {"(none)": ""} | map_choices(bool_codes)
    mode_choices     = {"(none)": ""} | map_choices(enum_codes)

    return vol.Schema({
        vol.Required(CONF_SETPOINT_CODE, default=defaults.get(CONF_SETPOINT_CODE, next(iter(setpoint_choices.values()), ""))): vol.In(setpoint_choices),
        vol.Optional(CONF_CURTEMP_CODE, default=defaults.get(CONF_CURTEMP_CODE, "")): vol.In(curtemp_choices),
        vol.Optional(CONF_POWER_CODE, default=defaults.get(CONF_POWER_CODE, "")): vol.In(power_choices),
        vol.Optional(CONF_MODE_CODE, default=defaults.get(CONF_MODE_CODE, "")): vol.In(mode_choices),
        vol.Optional(CONF_MIN_TEMP, default=5): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
        vol.Optional(CONF_PRECISION, default=1.0): vol.In([0.5, 1.0]),
    })

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._step1: Dict[str, Any] = {}
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA)
        # Save Step 1 and fetch spec/status
        self._step1 = user_input
        endpoint = resolve_endpoint(user_input.get(CONF_REGION), user_input.get(CONF_ENDPOINT) or None)
        try:
            api = await self.hass.async_add_executor_job(
                connect_sync, endpoint, user_input[CONF_ACCESS_ID], user_input[CONF_ACCESS_SECRET]
            )
            self._spec = await self.hass.async_add_executor_job(get_spec_sync, api, user_input[CONF_DEVICE_ID])
            self._status = await self.hass.async_add_executor_job(get_status_sync, api, user_input[CONF_DEVICE_ID])
        except Exception as e:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP1_SCHEMA,
                errors={"base": "cannot_connect"},
                description_placeholders={"err": str(e)[:200]},
            )
        if not self._spec:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP1_SCHEMA,
                errors={"base": "no_spec"},
            )
        return await self.async_step_map_dp()

    async def async_step_map_dp(self, user_input=None) -> FlowResult:
        schema = _build_step2_schema(self._spec, self._status)
        if user_input is None:
            return self.async_show_form(step_id="map_dp", data_schema=schema)

        data = {**self._step1, **user_input}
        title = f"Tuya Cloud DP ({data.get(CONF_DEVICE_ID)})"
        return self.async_create_entry(title=title, data=data)

    @callback
    def async_get_options_flow(self, entry):
        return TuyaCloudDPOptionsFlow(entry, self.hass)

class TuyaCloudDPOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry, hass):
        self.entry = entry
        self.hass = hass
        self._spec = {}
        self._status = {}

    async def async_step_init(self, user_input=None) -> FlowResult:
        data = {**self.entry.data, **(self.entry.options or {})}
        endpoint = resolve_endpoint(data.get(CONF_REGION), data.get(CONF_ENDPOINT) or None)
        try:
            api = await self.hass.async_add_executor_job(
                connect_sync, endpoint, data[CONF_ACCESS_ID], data[CONF_ACCESS_SECRET]
            )
            self._spec = await self.hass.async_add_executor_job(get_spec_sync, api, data[CONF_DEVICE_ID])
            self._status = await self.hass.async_add_executor_job(get_status_sync, api, data[CONF_DEVICE_ID])
        except Exception as e:
            # If fetch fails, still let them edit previous choices
            self._spec = {}
            self._status = {}

        defaults = {
            CONF_SETPOINT_CODE: data.get(CONF_SETPOINT_CODE, "temp_set"),
            CONF_CURTEMP_CODE: data.get(CONF_CURTEMP_CODE, ""),
            CONF_POWER_CODE: data.get(CONF_POWER_CODE, ""),
            CONF_MODE_CODE: data.get(CONF_MODE_CODE, ""),
        }
        schema = _build_step2_schema(self._spec, self._status, defaults)
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        # Save options
        new_opts = {**(self.entry.options or {}), **user_input}
        return self.async_create_entry(title="", data=new_opts)