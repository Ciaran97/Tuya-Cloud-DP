from __future__ import annotations
import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode, ClimateEntityFeature
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DOMAIN,
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

# Tuya SDK (sync)
from tuya_iot import TuyaOpenAPI

ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "cn": "https://openapi.tuyacn.com",
}

async def async_added_to_hass(self):
    # Keep entity in sync with Options changes
    self.async_on_remove(
        self.hass.config_entries.async_update_listener(self._on_options_update)
    )

async def _on_options_update(self, entry):
    if entry.entry_id != self.platform.config_entry.entry_id:
        return
    # Refresh cfg from updated options
    new_cfg = {**entry.data, **(entry.options or {})}
    self.cfg = new_cfg
    self._set_code = new_cfg.get("setpoint_code", self._set_code)
    self._cur_code = new_cfg.get("curtemp_code", self._cur_code)
    self._power_code = new_cfg.get("power_code", self._power_code)
    self._mode_code = new_cfg.get("mode_code", self._mode_code)
    await self.async_update()
    self.async_write_ha_state()

def _resolve_endpoint(cfg: dict) -> str:
    region = (cfg.get(CONF_REGION) or "us").lower()
    return ENDPOINTS.get(region, ENDPOINTS["us"])


async def _tuya_api(hass, cfg):
    """Create SDK client and connect in executor (non-blocking for HA)."""
    endpoint = _resolve_endpoint(cfg)
    api = TuyaOpenAPI(endpoint, cfg[CONF_ACCESS_ID], cfg[CONF_ACCESS_SECRET])
    await hass.async_add_executor_job(api.connect)  # <— move blocking call off event loop
    return api


async def async_setup_entry(hass, entry, async_add_entities):
    cfg = {**entry.data, **(entry.options or {})}
    api = await _tuya_api(hass, cfg)
    async_add_entities([TuyaDPClimate(hass, api, cfg)], update_before_add=True)


class TuyaDPClimate(ClimateEntity):
    _attr_name = "Tuya Cloud DP Thermostat"
    _attr_should_poll = True
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]

    def __init__(self, hass, api: TuyaOpenAPI, cfg: dict):
        self.hass = hass
        self.api = api
        self.cfg = cfg

        self._device_id = cfg[CONF_DEVICE_ID]
        self._set_code = cfg[CONF_SETPOINT_CODE]
        self._cur_code = cfg.get(CONF_CURTEMP_CODE) or None
        self._mode_code = cfg.get(CONF_MODE_CODE) or None
        self._power_code = cfg.get(CONF_POWER_CODE) or None

        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_min_temp = float(cfg.get(CONF_MIN_TEMP, 5))
        self._attr_max_temp = float(cfg.get(CONF_MAX_TEMP, 35))
        self._attr_precision = float(cfg.get(CONF_PRECISION, 1.0))
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = None
        self._attr_current_temperature = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name="Tuya Cloud DP Thermostat",
            manufacturer="Tuya",
            model=self.cfg.get("model", "WBR3-HYWE_v3.0"),
            configuration_url="https://iot.tuya.com",
        )

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
        """Poll device status via Tuya Cloud in executor."""
        try:
            res = await self.hass.async_add_executor_job(
                self.api.get,
                f"/v1.0/iot-03/devices/{self._device_id}/status")
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
        """Send commands via Tuya Cloud in executor."""
        body = {"commands": commands}
        try:
            res = await self.hass.async_add_executor_job(
                self.api.post,
                f"/v1.0/iot-03/devices/{self._device_id}/commands", body)
            if not res or not res.get("success"):
                _LOGGER.error("Tuya command failed: %s", res)
        except Exception as e:
            _LOGGER.error("Tuya command exception: %s", e)