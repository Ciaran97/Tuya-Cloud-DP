from __future__ import annotations
import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACMode,
    ClimateEntityFeature,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_REGION,
    CONF_DEVICE_ID,
    CONF_SETPOINT_CODE,
    CONF_MODE_CODE,
    CONF_POWER_CODE,
    CONF_CURTEMP_CODE,
    CONF_MIN_TEMP,
    CONF_MAX_TEMP,
    CONF_PRECISION,
)

_LOGGER = logging.getLogger(__name__)

# Tuya SDK
from tuya_iot import TuyaOpenAPI


async def _tuya_api(cfg):
    api = TuyaOpenAPI(
        f"https://openapi.tuya{cfg[CONF_REGION]}.com",
        cfg[CONF_ACCESS_ID],
        cfg[CONF_ACCESS_SECRET],
    )
    # token lifecycle handled by SDK
    api.connect()
    return api


async def async_setup_entry(hass, entry, async_add_entities):
    cfg = {**entry.data, **(entry.options or {})}
    api = await _tuya_api(cfg)
    async_add_entities([TuyaDPClimate(api, cfg)])


class TuyaDPClimate(ClimateEntity):
    _attr_name = "Tuya Cloud DP Thermostat"

    def __init__(self, api: TuyaOpenAPI, cfg: dict):
        self.api = api
        self.cfg = cfg

        self._device_id = cfg[CONF_DEVICE_ID]
        self._set_code = cfg[CONF_SETPOINT_CODE]
        self._cur_code = cfg.get(CONF_CURTEMP_CODE) or None
        self._mode_code = cfg.get(CONF_MODE_CODE) or None
        self._power_code = cfg.get(CONF_POWER_CODE) or None

        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        self._attr_min_temp = float(cfg.get(CONF_MIN_TEMP, 5))
        self._attr_max_temp = float(cfg.get(CONF_MAX_TEMP, 35))
        self._attr_precision = float(cfg.get(CONF_PRECISION, 1.0))

        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = None
        self._attr_current_temperature = None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if self._power_code is None:
            self._attr_hvac_mode = hvac_mode
            self.async_write_ha_state()
            return

        val = hvac_mode != HVACMode.OFF
        await self._send([{"code": self._power_code, "value": val}])
        self._attr_hvac_mode = hvac_mode
        await self.async_update()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        if ATTR_TEMPERATURE not in kwargs:
            return
        t = float(kwargs[ATTR_TEMPERATURE])
        t = max(self.min_temp, min(self.max_temp, t))
        await self._send([{"code": self._set_code, "value": t}])
        self._attr_target_temperature = t
        await self.async_update()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        # Poll device status from Tuya Cloud
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None,
                self.api.get,
                f"/v1.0/iot-03/devices/{self._device_id}/status",
                None,
                None,
            )
            if not res or not res.get("success"):
                return
            status = {
                i.get("code"): i.get("value")
                for i in (res.get("result") or [])
                if isinstance(i, dict) and "code" in i
            }

            if self._cur_code and self._cur_code in status:
                self._attr_current_temperature = status[self._cur_code]

            if self._set_code in status:
                self._attr_target_temperature = status[self._set_code]

            if self._power_code and self._power_code in status:
                self._attr_hvac_mode = (
                    HVACMode.HEAT if bool(status[self._power_code]) else HVACMode.OFF
                )
        except Exception as e:
            _LOGGER.warning("Tuya Cloud DP status error: %s", e)

    async def _send(self, commands):
        body = {"commands": commands}
        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None,
                self.api.post,
                f"/v1.0/iot-03/devices/{self._device_id}/commands",
                body,
                None,
            )
            if not res or not res.get("success"):
                _LOGGER.error("Tuya command failed: %s", res)
        except Exception as e:
            _LOGGER.error("Tuya command exception: %s", e)