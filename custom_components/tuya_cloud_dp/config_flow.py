# custom_components/tuya_cloud_dp/config_flow.py
from __future__ import annotations
import voluptuous as vol
from typing import Dict, Any
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN, CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_REGION, CONF_DEVICE_ID,
    CONF_SETPOINT_CODE, CONF_MODE_CODE, CONF_POWER_CODE, CONF_CURTEMP_CODE,
    CONF_MIN_TEMP, CONF_MAX_TEMP, CONF_PRECISION, CONF_ENDPOINT, CONF_USER_CODE
)
from .api import (
    resolve_endpoint, connect_sync, authorized_login_sync,
    get_user_devices_sync, get_spec_sync, get_status_sync
)

REGIONS = ["us","eu","in","cn"]

STEP1_SCHEMA = vol.Schema({
    vol.Required(CONF_ACCESS_ID): str,
    vol.Required(CONF_ACCESS_SECRET): str,
    vol.Required(CONF_REGION, default="us"): vol.In(REGIONS),
    vol.Optional(CONF_ENDPOINT, default=""): str,   # leave blank normally
    vol.Optional(CONF_USER_CODE, default=""): str,  # from Tuya/Smart Life app
})

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    def __init__(self):
        self._cfg: Dict[str, Any] = {}
        self._devices: list[Dict[str, Any]] = []
        self._spec: Dict[str, Dict[str, Any]] = {}
        self._status: Dict[str, Any] = {}

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA)

        # Save basic config and authenticate
        self._cfg = user_input
        endpoint = resolve_endpoint(user_input.get(CONF_REGION), user_input.get(CONF_ENDPOINT) or None)

        try:
            api = await self.hass.async_add_executor_job(
                connect_sync, endpoint, user_input[CONF_ACCESS_ID], user_input[CONF_ACCESS_SECRET]
            )
            uc = (user_input.get(CONF_USER_CODE) or "").strip()
            if uc:
                auth_res = await self.hass.async_add_executor_job(authorized_login_sync, api, uc)
                if not auth_res or not auth_res.get("success"):
                    return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA,
                        errors={"base": "cannot_connect"})
            # Discover devices like LocalTuya’s cloud step
            self._devices = await self.hass.async_add_executor_job(get_user_devices_sync, api)
            # Cache the api client on hass for later steps if you want, or reconnect later
            self.hass.data.setdefault(DOMAIN, {})["api_seed"] = (endpoint, user_input[CONF_ACCESS_ID], user_input[CONF_ACCESS_SECRET], uc)
        except Exception:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA,
                                        errors={"base": "cannot_connect"})

        return await self.async_step_pick_device()

    async def async_step_pick_device(self, user_input=None) -> FlowResult:
        if not self._devices:
            # No devices — let user go back
            return self.async_abort(reason="no_devices")

        # Build choices: show name / product / id like LocalTuya
        choices = {}
        for d in self._devices:
            did = d.get("id") or d.get("device_id")
            name = d.get("name") or "Unnamed"
            prod = d.get("product_name") or d.get("category") or ""
            label = f"{name} · {prod} · {did}"
            if did:
                choices[label] = did

        schema = vol.Schema({
            vol.Required(CONF_DEVICE_ID): vol.In(choices)
        })

        if user_input is None:
            return self.async_show_form(step_id="pick_device", data_schema=schema)

        self._cfg[CONF_DEVICE_ID] = user_input[CONF_DEVICE_ID]
        # Prefetch spec/status for DP mapping
        endpoint, aid, sec, uc = self.hass.data[DOMAIN].get("api_seed")
        # Reconnect + (re)login to be safe
        try:
            from .api import connect_sync, authorized_login_sync, get_spec_sync, get_status_sync
            api = await self.hass.async_add_executor_job(connect_sync, endpoint, aid, sec)
            if uc:
                await self.hass.async_add_executor_job(authorized_login_sync, api, uc)
            self._spec = await self.hass.async_add_executor_job(get_spec_sync, api, self._cfg[CONF_DEVICE_ID])
            self._status = await self.hass.async_add_executor_job(get_status_sync, api, self._cfg[CONF_DEVICE_ID])
        except Exception:
            self._spec, self._status = {}, {}

        return await self.async_step_map_dp()

    def _build_dp_schema(self, defaults: Dict[str,str] | None = None):
        defaults = defaults or {}
        # Filter likely types from spec
        number = [c for c,v in self._spec.items() if (v.get("type") or "").lower() in ("integer","float","value")]
        boolean = [c for c,v in self._spec.items() if (v.get("type") or "").lower() == "bool"]
        enum = [c for c,v in self._spec.items() if (v.get("type") or "").lower() == "enum"]

        def label(c):
            t = self._spec.get(c,{}).get("type") or "?"
            cur = self._status.get(c, "—")
            return f"{c} (type:{t}, current:{cur})"

        map_num = {label(c): c for c in number}
        map_bool = {"(none)": ""} | {label(c): c for c in boolean}
        map_enum = {"(none)": ""} | {label(c): c for c in enum}

        return vol.Schema({
            vol.Required(CONF_SETPOINT_CODE, default=defaults.get(CONF_SETPOINT_CODE, next(iter(map_num.values()), ""))): vol.In(map_num),
            vol.Optional(CONF_CURTEMP_CODE, default=defaults.get(CONF_CURTEMP_CODE,"")): vol.In(map_num | {"(none)": ""}),
            vol.Optional(CONF_POWER_CODE, default=defaults.get(CONF_POWER_CODE,"")): vol.In(map_bool),
            vol.Optional(CONF_MODE_CODE,  default=defaults.get(CONF_MODE_CODE,"")):  vol.In(map_enum),
            vol.Optional(CONF_MIN_TEMP, default=5): vol.Coerce(float),
            vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
            vol.Optional(CONF_PRECISION, default=1.0): vol.In([0.5,1.0]),
        })

    async def async_step_map_dp(self, user_input=None) -> FlowResult:
        schema = self._build_dp_schema()
        if user_input is None:
            return self.async_show_form(step_id="map_dp", data_schema=schema)

        data = {**self._cfg, **user_input}
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
            api = await self.hass.async_add_executor_job(connect_sync, endpoint, data[CONF_ACCESS_ID], data[CONF_ACCESS_SECRET])
            uc = (data.get(CONF_USER_CODE) or "").strip()
            if uc:
                await self.hass.async_add_executor_job(authorized_login_sync, api, uc)
            self._spec = await self.hass.async_add_executor_job(get_spec_sync, api, data[CONF_DEVICE_ID])
            self._status = await self.hass.async_add_executor_job(get_status_sync, api, data[CONF_DEVICE_ID])
        except Exception:
            self._spec, self._status = {}, {}

        schema = TuyaCloudDPConfigFlow._build_dp_schema(self, {
            CONF_SETPOINT_CODE: data.get(CONF_SETPOINT_CODE,"temp_set"),
            CONF_CURTEMP_CODE: data.get(CONF_CURTEMP_CODE,""),
            CONF_POWER_CODE: data.get(CONF_POWER_CODE,""),
            CONF_MODE_CODE:  data.get(CONF_MODE_CODE,""),
        })

        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        new_opts = {**(self.entry.options or {}), **user_input}
        return self.async_create_entry(title="", data=new_opts)