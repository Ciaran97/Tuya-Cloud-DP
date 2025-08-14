from __future__ import annotations
import asyncio, logging, time
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVAC_MODE_HEAT, HVAC_MODE_OFF, SUPPORT_TARGET_TEMPERATURE
from homeassistant.const import TEMP_CELSIUS, ATTR_TEMPERATURE
from .const import *
_LOGGER = logging.getLogger(__name__)

# Tuya SDK
from tuya_iot import TuyaOpenAPI

async def _tuya_api(cfg):
    api = TuyaOpenAPI(f"https://openapi.tuya{cfg[CONF_REGION]}.com", cfg[CONF_ACCESS_ID], cfg[CONF_ACCESS_SECRET])
    # Token creation is handled internally by TuyaOpenAPI.connect with username/password projects,
    # but for access_id/secret projects we use .token() first, then sign requests.
    # The SDK supports openapi access; connect(None, None) creates client; token() auto-refresh handled.
    api.connect()  # will request token
    return api

async def async_setup_entry(hass, entry, async_add_entities):
    cfg = {**entry.data, **(entry.options or {})}
    api = await _tuya_api(cfg)
    async_add_entities([TuyaDPClimate(api, cfg)])

class TuyaDPClimate(ClimateEntity):
    def __init__(self, api: TuyaOpenAPI, cfg: dict):
        self.api = api
        self.cfg = cfg
        self._attr_name = "Tuya Cloud DP Thermostat"
        self._attr_temperature_unit = TEMP_CELSIUS
        self._attr_hvac_modes = [HVAC_MODE_OFF, HVAC_MODE_HEAT]
        self._attr_supported_features = SUPPORT_TARGET_TEMPERATURE
        self._attr_min_temp = float(cfg.get(CONF_MIN_TEMP, 5))
        self._attr_max_temp = float(cfg.get(CONF_MAX_TEMP, 35))
        self._precision = float(cfg.get(CONF_PRECISION, 1.0))
        self._device_id = cfg[CONF_DEVICE_ID]
        self._set_code = cfg[CONF_SETPOINT_CODE]
        self._cur_code = cfg.get(CONF_CURTEMP_CODE) or None
        self._mode_code = cfg.get(CONF_MODE_CODE) or None
        self._power_code = cfg.get(CONF_POWER_CODE) or None
        self._target_temperature = None
        self._current_temperature = None
        self._hvac_mode = HVAC_MODE_OFF

    @property
    def precision(self): return self._precision
    @property
    def target_temperature(self): return self._target_temperature
    @property
    def current_temperature(self): return self._current_temperature
    @property
    def hvac_mode(self): return self._hvac_mode

    async def async_set_hvac_mode(self, hvac_mode):
        if self._power_code is None:
            self._hvac_mode = hvac_mode
            self.async_write_ha_state()
            return
        val = (hvac_mode != HVAC_MODE_OFF)
        await self._send([{ "code": self._power_code, "value": val }])
        self._hvac_mode = hvac_mode
        await self.async_update()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        if ATTR_TEMPERATURE not in kwargs: return
        t = kwargs[ATTR_TEMPERATURE]
        t = max(self._attr_min_temp, min(self._attr_max_temp, t))
        await self._send([{ "code": self._set_code, "value": t }])
        self._target_temperature = t
        await self.async_update()
        self.async_write_ha_state()

    async def async_update(self):
        try:
            res = await asyncio.get_event_loop().run_in_executor(None, self.api.get, f"/v1.0/iot-03/devices/{self._device_id}/status", None, None)
            if not res.get("success"): return
            status = {i["code"]: i.get("value") for i in res.get("result", []) if "code" in i}
            if self._cur_code and self._cur_code in status:
                self._current_temperature = status[self._cur_code]
            # fallbacks
            if self._set_code in status:
                self._target_temperature = status[self._set_code]
            if self._power_code and self._power_code in status:
                self._hvac_mode = HVAC_MODE_HEAT if status[self._power_code] else HVAC_MODE_OFF
        except Exception as e:
            _LOGGER.warning("Tuya Cloud DP status error: %s", e)

    async def _send(self, commands):
        body = { "commands": commands }
        try:
            res = await asyncio.get_event_loop().run_in_executor(None, self.api.post, f"/v1.0/iot-03/devices/{self._device_id}/commands", body, None)
            if not res.get("success"):
                _LOGGER.error("Tuya command failed: %s", res)
        except Exception as e:
            _LOGGER.error("Tuya command exception: %s", e)