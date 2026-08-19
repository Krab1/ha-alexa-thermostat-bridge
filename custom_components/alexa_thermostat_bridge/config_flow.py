from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_ECHO_ENTITY,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DOMAIN,
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required("name", default="Thermostat"): str,
        vol.Required(CONF_TEMPERATURE_SENSOR): selector.selector(
            {"entity": {"domain": "sensor"}}
        ),
        vol.Required(CONF_ECHO_ENTITY): selector.selector(
            {"entity": {"domain": "media_player"}}
        ),
        vol.Required(CONF_MIN_TEMP, default=DEFAULT_MIN_TEMP): vol.Coerce(float),
        vol.Required(CONF_MAX_TEMP, default=DEFAULT_MAX_TEMP): vol.Coerce(float),
    }
)


class AlexaThermostatBridgeConfigFlow(
    config_entries.ConfigFlow, domain=DOMAIN
):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_TEMPERATURE_SENSOR]}_{user_input[CONF_ECHO_ENTITY]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input["name"], data=user_input
            )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )
