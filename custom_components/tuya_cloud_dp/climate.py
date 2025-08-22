from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Dict, Optional
import time

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

from .cloud_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tuya_cloud_dp"

SCAN_INTERVAL = timedelta(seconds=10)
TUYA_TOKEN_ERRS = {"1010", "1011", "1004"}  # invalid/expired


# Config-entry keys we expect (ensure your config_flow stores these exact keys)
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


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up Tuya Cloud DP climate from a config entry."""
    cfg = entry.data
    api = TuyaCloudApi(hass, cfg[CONF_REGION], cfg[CONF_ACCESS_ID], cfg[CONF_ACCESS_SECRET])
    coordinator = TuyaCoordinator(hass, api, cfg[CONF_DEVICE_ID])

    try:
        await coordinator.async_config_entry_first_refresh()
        #_LOGGER.debug("Initial refresh OK for %s", cfg[CONF_DEVICE_ID])
    except Exception as e:
        _LOGGER.warning(
            "Initial refresh failed for %s: %s; adding entity unavailable",
            cfg[CONF_DEVICE_ID],
            e,
        )

    entity = TuyaCloudDPClimate(hass, entry, coordinator)
    async_add_entities([entity], update_before_add=False)


class TuyaCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
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
            code = str(j.get("code")) if isinstance(j, dict) else "http"
            msg = j.get("msg") if isinstance(j, dict) else str(j)
            raise UpdateFailed(f"{code} {msg}")
        out: Dict[str, Any] = {}
        for item in j.get("result", []):
            c = item.get("code")
            if c is not None:
                out[c] = item.get("value")
        return out

    async def _async_update_data(self) -> Dict[str, Any]:
        try:
            return await self._fetch_status_once()
        except UpdateFailed as e:
            txt = str(e)
            if any(err in txt for err in TUYA_TOKEN_ERRS):
                #_LOGGER.debug("Token issue detected (%s), refreshing", txt)
                self._token_ready = False
                await asyncio.sleep(0)
                return await self._fetch_status_once()
            raise


class TuyaCloudDPClimate(CoordinatorEntity[TuyaCoordinator], ClimateEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry, coordinator: TuyaCoordinator) -> None:
        super().__init__(coordinator)
        d = entry.data
        self._entry = entry
        self._hass = hass
        self._pending: dict[str, tuple[Any, float]] = {}  # code -> (value, expires_at_monotonic)

        self._device_id: str = d[CONF_DEVICE_ID]
        self._api = coordinator._api

        # Codes selected in the flow
        self._code_set: str = d.get(CONF_SETPOINT_CODE) or "temp_set"
        self._code_cur: Optional[str] = d.get(CONF_CURTEMP_CODE) or "temp_current"
        self._code_power: Optional[str] = d.get(CONF_POWER_CODE) or "Power"
        self._code_mode: Optional[str] = d.get(CONF_MODE_CODE) or "Mode"

        # Defaults before spec lands
        self._scale = 1   # scale=1 -> 0.1°C increments
        self._step_raw = 1
        self._min_raw = 5
        self._max_raw = 350

        # UI basics early to avoid "None" name or missing attrs
        self._attr_name = f"Tuya Climate {self._device_id[-6:]}"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
        self._hvac_modes_supported = {HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO}
        self._attr_hvac_modes = list(self._hvac_modes_supported)

        pr = d.get(CONF_PRECISION)
        self._attr_precision = float(pr) if pr is not None else 0.1
        # Safe defaults; refined after spec fetch
        self._attr_min_temp = 5.0
        self._attr_max_temp = 35.0
        self._min_temp_override = d.get(CONF_MIN_TEMP)
        self._max_temp_override = d.get(CONF_MAX_TEMP)

        # fire and forget: load specifications to refine ranges/modes
        hass.async_create_task(self._async_load_spec())

    # ---- scale helpers ----
    @property
    def _scale_factor(self) -> float:
        return 10.0 if self._scale == 1 else (10.0 ** self._scale if self._scale > 1 else 1.0)

    def _to_device_raw(self, celsius: float) -> int:
        return int(round(celsius * self._scale_factor))

    def _from_device_raw(self, raw: Any) -> Optional[float]:
        try:
            return float(raw) / self._scale_factor
        except (TypeError, ValueError):
            return None

    def _set_pending(self, code: str, value: Any, ttl: float = 3.0) -> None:
        """Hold a short-lived optimistic value to avoid UI flicker."""
        self._pending[code] = (value, time.monotonic() + ttl)

    def _get_effective(self, code: Optional[str]) -> Any:
        """Return the pending value (if not expired) else coordinator value."""
        if not code:
            return None
        pending = self._pending.get(code)
        if pending:
            val, exp = pending
            if time.monotonic() < exp:
                return val
            # expired; drop it
            self._pending.pop(code, None)
        return self.coordinator.data.get(code)

    async def _async_load_spec(self) -> None:
        """Fetch specifications/functions and apply ranges & supported modes."""
        try:
            await self.coordinator._ensure_token()
            fx = await self._api.device_functions(self._device_id)
            spec = await self._api.device_spec(self._device_id)

            # prefer fx; fallback to spec
            funcs = []
            if fx and fx.get("success"):
                funcs = (fx.get("result") or {}).get("functions") or []
            if not funcs and spec and spec.get("success"):
                funcs = (spec.get("result") or {}).get("functions") or []

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
                        _LOGGER.warning("parse temp_set values failed: %s", e)
                    break

            min_c = self._from_device_raw(self._min_raw) or 5.0
            max_c = self._from_device_raw(self._max_raw) or 70.0
            self._attr_min_temp = (
                float(self._min_temp_override)
                if self._min_temp_override is not None
                else float(min_c)
            )
            self._attr_max_temp = (
                float(self._max_temp_override)
                if self._max_temp_override is not None
                else float(max_c)
            )

            if self._entry.data.get(CONF_PRECISION) is None:
                step_c = self._from_device_raw(self._step_raw) or 0.5
                self._attr_precision = 0.1 if step_c <= 0.1 else (0.5 if step_c <= 0.5 else 1.0)

            # Supported modes via Mode enum, if present
            modes_avail = set()
            for f in funcs:
                if f.get("code") == (self._code_mode or "") and isinstance(f.get("values"), str):
                    try:
                        rng = json.loads(f["values"]).get("range") or []
                        modes_avail.update(rng)
                    except Exception:
                        pass
            supported = {HVACMode.OFF}
            if "Manual" in modes_avail or self._code_power:
                supported.add(HVACMode.HEAT)
            if "Program" in modes_avail or "TempProg" in modes_avail:
                supported.add(HVACMode.AUTO)
            self._hvac_modes_supported = supported or {HVACMode.OFF, HVACMode.HEAT}
            self._attr_hvac_modes = list(self._hvac_modes_supported)

            self.async_write_ha_state()
        except Exception as e:
            _LOGGER.warning("spec fetch failed: %s", e)

    # ---- HA required ----
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

    # ---- State mapping ----
    @property
    def current_temperature(self) -> Optional[float]:
        if not self._code_cur:
            return None
        raw = self._get_effective(self._code_cur)
        return self._from_device_raw(raw)

    @property
    def target_temperature(self) -> Optional[float]:
        raw = self._get_effective(self._code_set)
        return self._from_device_raw(raw)

    @property
    def hvac_action(self) -> Optional[HVACAction]:
        heating = self._get_effective("Heating_state")
        power = self._get_effective(self._code_power) if self._code_power else True
        if power is False:
            return HVACAction.OFF
        if heating is True:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode:
        power = self._get_effective(self._code_power) if self._code_power else True
        if power is False:
            return HVACMode.OFF
        mode = (self._get_effective(self._code_mode) or "").lower()
        #_LOGGER.debug("Mode: %s", mode)
        if mode in ("program", "tempprog"):
            return HVACMode.AUTO
        return HVACMode.HEAT

    # ---- Commands ----
    async def _send(self, code: str, value: Any) -> None:
        async def _once() -> Dict[str, Any]:
            await self.coordinator._ensure_token()
            j = await self._api.send_command(self._device_id, [{"code": code, "value": value}])
            return j

        j = await _once()
        if j and not j.get("success") and str(j.get("code")) in TUYA_TOKEN_ERRS:
            self.coordinator._token_ready = False
            j = await _once()
        if not j or not j.get("success"):
            raise RuntimeError(f"command failed: {j}")

        # Optimistic overlay to prevent flicker
        self._set_pending(code, value)
        self.async_write_ha_state()

        def _refresh_later(delay: float):
            self._hass.loop.call_later(
                delay, lambda: self._hass.async_create_task(self.coordinator.async_request_refresh())
            )

        _refresh_later(0.4)
        _refresh_later(2.0)

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self._send(self._code_set, self._to_device_raw(float(temp)))
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            if self._code_power:
                await self._send(self._code_power, False)
            elif self._code_mode:
                await self._send(self._code_mode, "Holiday")
            await self.coordinator.async_request_refresh()
            return

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
            await self._send(self._code_mode, "Holiday")
        await self.coordinator.async_request_refresh()

    # ---- Ranges / availability ----
    @property
    def supported_features(self) -> int:
        return self._attr_supported_features

    @property
    def min_temp(self) -> float:
        return float(getattr(self, "_attr_min_temp", 5.0))

    @property
    def max_temp(self) -> float:
        return float(getattr(self, "_attr_max_temp", 35.0))

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success