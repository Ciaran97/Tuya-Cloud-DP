from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
    UpdateFailed,
)

from .const import DOMAIN
from .cloud_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=20)
TUYA_TOKEN_ERRS = {"1010", "1011"}  # invalid/expired

# Entry keys (from your config flow)
CONF_REGION = "region"
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_USER_ID = "user_id"
CONF_DEVICE_ID = "device_id"

CONF_SETPOINT_CODE = "setpoint_code"
CONF_CURTEMP_CODE = "curtemp_code"
CONF_POWER_CODE = "power_code"
CONF_MODE_CODE = "mode_code"

CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_PRECISION = "precision"

PLATFORM = "climate"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tuya Cloud DP climate from a config entry."""
    cfg = entry.data
    region = cfg[CONF_REGION]
    aid = cfg[CONF_ACCESS_ID]
    sec = cfg[CONF_ACCESS_SECRET]
    device_id = cfg[CONF_DEVICE_ID]

    api = TuyaCloudApi(hass, region, aid, sec)

    coordinator = TuyaCoordinator(hass, api, device_id)
    await coordinator.async_config_entry_first_refresh()

    entity = TuyaCloudDPClimate(hass, entry, coordinator)
    async_add_entities([entity])


class TuyaCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Coordinator to poll Tuya device status."""

    def __init__(self, hass: HomeAssistant, api: TuyaCloudApi, device_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"tuya_cloud_dp_{device_id}",
            update_interval=SCAN_INTERVAL,
        )
        self._api = api
        self._device_id = device_id
        self._token_ready = False

    async def _ensure_token(self) -> None:
        if not self._token_ready:
            res = await self._api.grant_type_1()
            if res != "ok":
                raise UpdateFailed(f"token: {res}")
            self._token_ready = True

    async def _fetch_status_once(self) -> Dict[str, Any]:
        await self._ensure_token()
        j = await self._api.device_status(self._device_id)
        if not j or not j.get("success"):
            # Surface Tuya code if present
            code = str(j.get("code")) if isinstance(j, dict) else "http"
            msg = j.get("msg") if isinstance(j, dict) else str(j)
            raise UpdateFailed(f"status: {code} {msg}")
        # Normalize into code->value
        out: Dict[str, Any] = {}
        for item in j.get("result", []):
            c = item.get("code")
            if c is not None:
                out[c] = item.get("value")
        return out

    async def _async_update_data(self) -> Dict[str, Any]:
        """Refresh device status. Refresh token once if needed."""
        try:
            return await self._fetch_status_once()
        except UpdateFailed as e:
            # If token invalid/expired -> refresh once then retry
            txt = str(e)
            if any(err in txt for err in TUYA_TOKEN_ERRS):
                _LOGGER.debug("Token issue detected (%s), refreshing", txt)
                self._token_ready = False
                await asyncio.sleep(0)  # yield
                return await self._fetch_status_once()
            raise


class TuyaCloudDPClimate(CoordinatorEntity[TuyaCoordinator], ClimateEntity):
    """HA Climate entity backed by Tuya Cloud /commands & /status."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry, coordinator: TuyaCoordinator) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._entry = entry
        d = entry.data

        self._device_id: str = d[CONF_DEVICE_ID]

        # Codes chosen in the flow
        self._code_set: str = d.get(CONF_SETPOINT_CODE) or "temp_set"
        self._code_cur: Optional[str] = d.get(CONF_CURTEMP_CODE) or "temp_current"
        self._code_power: Optional[str] = d.get(CONF_POWER_CODE) or "Power"
        self._code_mode: Optional[str] = d.get(CONF_MODE_CODE) or "Mode"

        # Defaults; will refine from specifications when available
        self._scale = 1  # temp_set scale (10 means 0.1°C)
        self._step_raw = 1  # integer step in scaled units
        self._min_raw = 5
        self._max_raw = 700

        self._attr_name = f"Tuya Climate {self._device_id[-6:]}"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

        # precision: prefer entry override; else from scale
        pr = d.get(CONF_PRECISION)
        self._attr_precision = float(pr) if pr is not None else 0.1

        # apply min/max overrides from entry (in °C), else from spec later
        self._min_temp_override = d.get(CONF_MIN_TEMP)
        self._max_temp_override = d.get(CONF_MAX_TEMP)

        # Prepare runtime API (same as coordinator’s)
        self._api = coordinator._api  # reuse underlying client

        self._hvac_modes_supported = {HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO}
        self._attr_hvac_modes = list(self._hvac_modes_supported)

        # kick off one-time spec fetch in background
        self._spec_fetched = False
        hass.async_create_task(self._async_load_spec())

    # ---------- Helpers: spec & scaling ----------

    @property
    def _scale_factor(self) -> float:
        # scale=1 → values are in tenths (divide by 10)
        return 10.0 if self._scale == 1 else (10.0 ** self._scale if self._scale > 1 else 1.0)

    def _to_device_raw(self, celsius: float) -> int:
        return int(round(celsius * self._scale_factor))

    def _from_device_raw(self, raw: Any) -> Optional[float]:
        try:
            return float(raw) / self._scale_factor
        except (TypeError, ValueError):
            return None

    async def _async_load_spec(self) -> None:
        """Fetch specifications and adjust min/max/scale/step + supported modes."""
        try:
            await self.coordinator._ensure_token()
            spec = await self._api.device_spec(self._device_id)
            if not spec or not spec.get("success"):
                _LOGGER.debug("spec not available: %s", spec)
                return
            res = spec.get("result") or {}
            funcs = res.get("functions") or []
            # pull temp_set constraints
            for f in funcs:
                if f.get("code") == self._code_set:
                    try:
                        values = f.get("values") or "{}"
                        if isinstance(values, str):
                            values = json.loads(values)
                        self._scale = int(values.get("scale", self._scale))
                        self._step_raw = int(values.get("step", self._step_raw))
                        self._min_raw = int(values.get("min", self._min_raw))
                        self._max_raw = int(values.get("max", self._max_raw))
                    except Exception as e:
                        _LOGGER.debug("parse temp_set values failed: %s", e)
                    break

            # Fill HA ranges (°C); prefer explicit overrides if present
            min_c = self._from_device_raw(self._min_raw) or 5.0
            max_c = self._from_device_raw(self._max_raw) or 70.0
            self._attr_min_temp = float(self._min_temp_override) if self._min_temp_override is not None else float(min_c)
            self._attr_max_temp = float(self._max_temp_override) if self._max_temp_override is not None else float(max_c)

            # Precision: derive from step if not overridden
            if self._entry.data.get(CONF_PRECISION) is None:
                step_c = self._from_device_raw(self._step_raw) or 0.5
                # HA precision only allows a few values; clamp to 0.1 or 0.5 or 1.0
                self._attr_precision = 0.1 if step_c <= 0.1 else (0.5 if step_c <= 0.5 else 1.0)

            # Supported hvac modes from enums if available
            modes_available = set()
            for f in funcs:
                if f.get("code") == (self._code_mode or "") and isinstance(f.get("values"), str):
                    try:
                        rng = json.loads(f["values"]).get("range") or []
                        modes_available.update(rng)
                    except Exception:
                        pass
            supported = {HVACMode.OFF}
            if "Manual" in modes_available or self._code_power:
                supported.add(HVACMode.HEAT)  # use Manual/Power as "heat"
            if "Program" in modes_available or "TempProg" in modes_available:
                supported.add(HVACMode.AUTO)
            self._hvac_modes_supported = supported or {HVACMode.OFF, HVACMode.HEAT}
            self._attr_hvac_modes = list(self._hvac_modes_supported)

            self._spec_fetched = True
            self.async_write_ha_state()
        except Exception as e:
            _LOGGER.debug("spec fetch failed: %s", e)

    # ---------- Entity basics ----------

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self._device_id}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self.name,
            "manufacturer": "Tuya",
            "model": "Cloud DP Climate",
            "via_device": (DOMAIN, "cloud"),
        }

    # ---------- State from coordinator ----------

    @property
    def current_temperature(self) -> Optional[float]:
        if not self._code_cur:
            return None
        raw = self.coordinator.data.get(self._code_cur)
        return self._from_device_raw(raw)

    @property
    def target_temperature(self) -> Optional[float]:
        raw = self.coordinator.data.get(self._code_set)
        return self._from_device_raw(raw)

    @property
    def hvac_action(self) -> Optional[HVACAction]:
        # Prefer explicit Heating_state if present
        heating = self.coordinator.data.get("Heating_state")
        power = self.coordinator.data.get(self._code_power) if self._code_power else True
        if power is False:
            return HVACAction.OFF
        if heating is True:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode:
        power = self.coordinator.data.get(self._code_power) if self._code_power else True
        if power is False:
            return HVACMode.OFF
        mode = (self.coordinator.data.get(self._code_mode) or "").lower()
        if mode in ("program", "tempprog"):
            return HVACMode.AUTO
        # With these thermostats, "Manual" means on/heat
        return HVACMode.HEAT

    # ---------- Commands ----------

    async def _send(self, code: str, value: Any) -> None:
        """Send a single Tuya /commands write; refresh token on 1010/1011."""
        async def _once() -> Dict[str, Any]:
            body = {"commands": [{"code": code, "value": value}]}
            await self.coordinator._ensure_token()
            resp = await self._api._req("POST", f"/v1.0/iot-03/devices/{self._device_id}/commands", body_obj=body)
            try:
                j = resp.json()
            except Exception:
                j = {"success": False, "code": "http", "msg": f"HTTP {resp.status_code}"}
            return j

        j = await _once()
        if j and not j.get("success") and str(j.get("code")) in TUYA_TOKEN_ERRS:
            # refresh token and retry once
            self.coordinator._token_ready = False
            j = await _once()
        if not j or not j.get("success"):
            code_s = j.get("code") if isinstance(j, dict) else "http"
            msg = j.get("msg") if isinstance(j, dict) else str(j)
            raise RuntimeError(f"command failed: {code_s} {msg}")

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        raw = self._to_device_raw(float(temp))
        await self._send(self._code_set, raw)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            # Prefer power code if available; else map to Mode if supports off
            if self._code_power:
                await self._send(self._code_power, False)
            elif self._code_mode:
                await self._send(self._code_mode, "Holiday")  # safest non-heating placeholder
            await self.coordinator.async_request_refresh()
            return

        # Turning ON → Manual heat by default
        if self._code_power:
            await self._send(self._code_power, True)

        if self._code_mode:
            if hvac_mode == HVACMode.AUTO:
                await self._send(self._code_mode, "Program")
            else:
                await self._send(self._code_mode, "Manual")

        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        if self._code_power:
            await self._send(self._code_power, True)
        elif self._code_mode:
            await self._send(self._code_mode, "Manual")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        if self._code_power:
            await self._send(self._code_power, False)
        elif self._code_mode:
            # Some devices support an explicit off via mode enum; if not, fallback to "Holiday"
            await self._send(self._code_mode, "Holiday")
        await self.coordinator.async_request_refresh()

    # ---------- Supported features / ranges ----------

    @property
    def supported_features(self) -> int:
        return self._attr_supported_features

    @property
    def min_temp(self) -> float:
        return float(self._attr_min_temp or 5.0)

    @property
    def max_temp(self) -> float:
        return float(self._attr_max_temp or 70.0)

    # ---------- Availability ----------

    @property
    def available(self) -> bool:
        # Consider entity available if last update succeeded
        return self.coordinator.last_update_success