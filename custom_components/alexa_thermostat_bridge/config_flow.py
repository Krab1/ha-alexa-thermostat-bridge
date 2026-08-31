from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALEXA_ENTITY_ID,
    CONF_ECHO_ENTITY,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_POLL_INTERVAL,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

POLL_INTERVAL_SELECTOR = selector.selector(
    {
        "number": {
            "min": MIN_POLL_INTERVAL,
            "max": MAX_POLL_INTERVAL,
            "step": 5,
            "unit_of_measurement": "seconds",
            "mode": "box",
        }
    }
)


def _min_max_errors(user_input: dict) -> dict:
    if user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
        return {CONF_MIN_TEMP: "min_max_invalid"}
    return {}


def _user_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("name", default=defaults.get("name", "Thermostat")): str,
            vol.Required(
                CONF_TEMPERATURE_SENSOR,
                default=defaults.get(CONF_TEMPERATURE_SENSOR, vol.UNDEFINED),
            ): selector.selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                CONF_ECHO_ENTITY,
                default=defaults.get(CONF_ECHO_ENTITY, vol.UNDEFINED),
            ): selector.selector({"entity": {"domain": "media_player"}}),
            vol.Required(
                CONF_MIN_TEMP, default=defaults.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_TEMP, default=defaults.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP)
            ): vol.Coerce(float),
            # Optional: the internal Alexa entityId (a UUID) for this thermostat,
            # found in alexa_media debug logs in a get_entity_state response.
            # Enables periodic polling of setpoints/mode/hvac action/humidity.
            vol.Optional(
                CONF_ALEXA_ENTITY_ID, default=defaults.get(CONF_ALEXA_ENTITY_ID, "")
            ): str,
            vol.Required(
                CONF_POLL_INTERVAL,
                default=defaults.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): POLL_INTERVAL_SELECTOR,
        }
    )


class AlexaThermostatBridgeConfigFlow(
    config_entries.ConfigFlow, domain=DOMAIN
):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AlexaThermostatBridgeOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            errors = _min_max_errors(user_input)
            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_TEMPERATURE_SENSOR]}_{user_input[CONF_ECHO_ENTITY]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input["name"], data=user_input
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )


class AlexaThermostatBridgeOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            errors = _min_max_errors(user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TEMPERATURE_SENSOR,
                    default=current[CONF_TEMPERATURE_SENSOR],
                ): selector.selector({"entity": {"domain": "sensor"}}),
                vol.Required(
                    CONF_ECHO_ENTITY, default=current[CONF_ECHO_ENTITY]
                ): selector.selector({"entity": {"domain": "media_player"}}),
                vol.Required(
                    CONF_MIN_TEMP, default=current[CONF_MIN_TEMP]
                ): vol.Coerce(float),
                vol.Required(
                    CONF_MAX_TEMP, default=current[CONF_MAX_TEMP]
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_ALEXA_ENTITY_ID,
                    default=current.get(CONF_ALEXA_ENTITY_ID, ""),
                ): str,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): POLL_INTERVAL_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
