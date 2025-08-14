from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import *

DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_ACCESS_ID): str,
    vol.Required(CONF_ACCESS_SECRET): str,
    vol.Required(CONF_REGION, default="au"): vol.In(["eu","us","in","au"]),
    vol.Required(CONF_DEVICE_ID): str,
    vol.Required(CONF_SETPOINT_CODE, default="temp_set"): str,
    vol.Optional(CONF_CURTEMP_CODE, default=""): str,
    vol.Optional(CONF_MODE_CODE, default="Mode"): str,
    vol.Optional(CONF_POWER_CODE, default="Power"): str,
    vol.Optional(CONF_MIN_TEMP, default=5): vol.Coerce(float),
    vol.Optional(CONF_MAX_TEMP, default=35): vol.Coerce(float),
    vol.Optional(CONF_PRECISION, default=1.0): vol.In([0.5,1.0])
})

class TuyaCloudDPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="Tuya Cloud DP", data=user_input)
        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA)

    @callback
    def async_get_options_flow(self, config_entry):
        return TuyaCloudDPOptionsFlowHandler(config_entry)

class TuyaCloudDPOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry): self.config_entry = config_entry
    async def async_step_init(self, user_input=None):
        return self.async_show_form(step_id="init", data_schema=DATA_SCHEMA)