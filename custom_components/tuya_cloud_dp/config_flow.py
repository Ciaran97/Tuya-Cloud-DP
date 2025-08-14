from __future__ import annotations

from typing import Any, Dict
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

# One tiny schema so we can prove the handler loads
STEP1_SCHEMA = vol.Schema({
    vol.Required("test_field", default="ok"): str,
})

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP1_SCHEMA)
        # Create a dummy entry so we know the flow works
        await self.async_set_unique_id("tuya_cloud_dp_dummy")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Tuya Cloud DP (test)", data=user_input)

    @callback
    def async_get_options_flow(self, entry: config_entries.ConfigEntry):
        return TuyaCloudDPOptionsFlow(entry)

class TuyaCloudDPOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=STEP1_SCHEMA)
        return self.async_create_entry(title="", data=user_input)