from __future__ import annotations

import json
import logging
from datetime import timedelta

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ALEXA_ENTITY_ID,
    CONF_ECHO_ENTITY,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_TEMPERATURE_SENSOR,
)

# HEAT_COOL, not AUTO, is HA's mode for a user-adjustable heat/cool range.
MODES = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]

ALEXA_MODE_TO_HVAC = {
    "OFF": HVACMode.OFF,
    "HEAT": HVACMode.HEAT,
    "COOL": HVACMode.COOL,
    "AUTO": HVACMode.HEAT_COOL,
}

# alexa_media_player's own coordinator refreshes on a much longer interval;
# this only needs to catch up occasionally, not chase it.
POLL_INTERVAL = timedelta(minutes=5)

# The round dial fires set_temperature on every drag step, not just on
# release - without this, each intermediate value became a real spoken
# Alexa command and they raced each other.
COMMAND_DEBOUNCE = 8

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AlexaBridgeClimate(hass, entry)])


class AlexaBridgeClimate(ClimateEntity, RestoreEntity):
    _attr_hvac_modes = MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_should_poll = False
    _attr_translation_key = "alexa_bridge_thermostat"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        config = {**entry.data, **entry.options}
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.data["name"]
        self._sensor_entity_id = config[CONF_TEMPERATURE_SENSOR]
        self._echo_entity_id = config[CONF_ECHO_ENTITY]
        self._attr_min_temp = config[CONF_MIN_TEMP]
        self._attr_max_temp = config[CONF_MAX_TEMP]
        self._alexa_entity_id = config.get(CONF_ALEXA_ENTITY_ID) or None
        self._attr_hvac_mode = HVACMode.OFF

        # Remembered independently of hvac_mode - only one pair gets
        # projected onto the entity's actual attrs at a time (see
        # _sync_target_attrs), but both must survive a mode round-trip.
        mid = (self._attr_min_temp + self._attr_max_temp) / 2
        self._single_target = mid
        self._range_low = mid - 2
        self._range_high = mid + 2

        self._attr_target_temperature = None
        self._attr_target_temperature_low = None
        self._attr_target_temperature_high = None
        self._attr_current_temperature = None
        self._attr_current_humidity = None
        self._attr_hvac_action = None
        self._attr_extra_state_attributes = {}
        self._pending_commands: dict[str, CALLBACK_TYPE] = {}
        self._sync_target_attrs()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self.async_on_remove(self._cancel_pending_commands)

        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.state in MODES:
                self._attr_hvac_mode = HVACMode(last_state.state)
            elif last_state.state == "auto":
                # Pre-1.1.0 stored HVACMode.AUTO ("auto"); that value isn't
                # in MODES anymore since range setpoints need HEAT_COOL.
                self._attr_hvac_mode = HVACMode.HEAT_COOL
            last_target = last_state.attributes.get(ATTR_TEMPERATURE)
            if last_target is not None:
                self._single_target = last_target
            last_low = last_state.attributes.get(ATTR_TARGET_TEMP_LOW)
            if last_low is not None:
                self._range_low = last_low
            last_high = last_state.attributes.get(ATTR_TARGET_TEMP_HIGH)
            if last_high is not None:
                self._range_high = last_high
            self._sync_target_attrs()

        sensor_state = self.hass.states.get(self._sensor_entity_id)
        if sensor_state is not None:
            self._update_current_temperature(sensor_state)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._sensor_entity_id], self._handle_sensor_change
            )
        )

        if self._alexa_entity_id:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._async_poll_alexa_state, POLL_INTERVAL
                )
            )
            await self._async_poll_alexa_state()

    @callback
    def _handle_sensor_change(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is not None:
            self._update_current_temperature(new_state)
            self.async_write_ha_state()

    def _sync_target_attrs(self) -> None:
        # HA's frontend picks single- vs dual-dial by which of these is
        # non-None, not by hvac_mode - so only the mode-appropriate pair
        # may be populated, but the *other* pair's value must be kept
        # around in _single_target/_range_low/_range_high so switching
        # back doesn't leave both None (blank read-only ring). In OFF,
        # both stay None so the dial disappears/greys out - Alexa isn't
        # going to act on a temperature while the thermostat is off.
        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_target_temperature = None
            self._attr_target_temperature_low = None
            self._attr_target_temperature_high = None
        elif self._attr_hvac_mode == HVACMode.HEAT_COOL:
            self._attr_target_temperature = None
            self._attr_target_temperature_low = self._range_low
            self._attr_target_temperature_high = self._range_high
        else:
            self._attr_target_temperature = self._single_target
            self._attr_target_temperature_low = None
            self._attr_target_temperature_high = None

    def _update_current_temperature(self, state: State) -> None:
        try:
            self._attr_current_temperature = float(state.state)
        except ValueError:
            self._attr_current_temperature = None

    @callback
    def _cancel_pending_commands(self) -> None:
        # Debounced commands scheduled via async_call_later outlive a single
        # HA update cycle - if the entity is torn down mid-debounce, self.hass
        # is cleared and the eventual _fire() would crash on _send_command.
        for cancel in self._pending_commands.values():
            cancel()
        self._pending_commands.clear()

    def _schedule_command(self, key: str, text: str) -> None:
        cancel = self._pending_commands.pop(key, None)
        if cancel is not None:
            cancel()

        async def _fire(_now):
            self._pending_commands.pop(key, None)
            await self._send_command(text)

        self._pending_commands[key] = async_call_later(
            self.hass, COMMAND_DEBOUNCE, _fire
        )

    async def _send_command(self, text: str) -> None:
        await self.hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": self._echo_entity_id,
                "media_content_id": text,
                "media_content_type": "custom",
            },
            blocking=True,
        )

    def _get_alexa_login(self):
        accounts = self.hass.data.get("alexa_media", {}).get("accounts", {})
        for account in accounts.values():
            login = account.get("login_obj")
            if login is not None:
                return login
        return None

    @staticmethod
    def _fahrenheit(payload) -> float | None:
        if not isinstance(payload, dict) or "value" not in payload:
            return None
        value = payload["value"]
        if payload.get("scale") == "CELSIUS":
            return value * 9 / 5 + 32
        return value

    async def _async_poll_alexa_state(self, now=None) -> None:
        login = self._get_alexa_login()
        if login is None:
            _LOGGER.debug(
                "Alexa Thermostat Bridge: alexa_media login not found, skipping poll"
            )
            return

        try:
            from alexapy import AlexaAPI
        except ImportError:
            _LOGGER.warning(
                "Alexa Thermostat Bridge: alexapy not importable - is"
                " alexa_media_player installed?"
            )
            return

        try:
            response = await AlexaAPI.get_entity_state(
                login, entity_ids=[self._alexa_entity_id]
            )
        except Exception:  # noqa: BLE001 - third-party API, shape of failures unknown
            _LOGGER.debug("Alexa Thermostat Bridge: poll failed", exc_info=True)
            return

        device_states = (response or {}).get("deviceStates", [])
        capabilities = {}
        for device_state in device_states:
            if device_state.get("entity", {}).get("entityId") != self._alexa_entity_id:
                continue
            for raw in device_state.get("capabilityStates", []):
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                capabilities[(parsed.get("namespace"), parsed.get("name"))] = parsed.get(
                    "value"
                )
            break

        if not capabilities:
            return

        mode_raw = capabilities.get(("Alexa.ThermostatController", "thermostatMode"))
        if mode_raw in ALEXA_MODE_TO_HVAC:
            self._attr_hvac_mode = ALEXA_MODE_TO_HVAC[mode_raw]

        target = self._fahrenheit(
            capabilities.get(("Alexa.ThermostatController", "targetSetpoint"))
        )
        if target is not None:
            self._single_target = target

        low = self._fahrenheit(
            capabilities.get(("Alexa.ThermostatController", "lowerSetpoint"))
        )
        if low is not None:
            self._range_low = low

        high = self._fahrenheit(
            capabilities.get(("Alexa.ThermostatController", "upperSetpoint"))
        )
        if high is not None:
            self._range_high = high

        precise_temp = self._fahrenheit(
            capabilities.get(("Alexa.TemperatureSensor", "preciseTemperature"))
        )
        if precise_temp is not None:
            self._attr_current_temperature = precise_temp

        humidity = capabilities.get(("Alexa.HumiditySensor", "relativeHumidity"))
        if humidity is not None:
            self._attr_current_humidity = round(humidity)

        heater_on = capabilities.get(
            ("Alexa.ThermostatController.HVAC.Components", "primaryHeaterOperation")
        )
        cooler_on = capabilities.get(
            ("Alexa.ThermostatController.HVAC.Components", "coolerOperation")
        )
        fan_on = capabilities.get(
            ("Alexa.ThermostatController.HVAC.Components", "fanOperation")
        )
        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF
        elif heater_on == "ON":
            self._attr_hvac_action = HVACAction.HEATING
        elif cooler_on == "ON":
            self._attr_hvac_action = HVACAction.COOLING
        elif fan_on == "ON":
            self._attr_hvac_action = HVACAction.FAN
        else:
            self._attr_hvac_action = HVACAction.IDLE

        sensor_mode = capabilities.get(
            ("Alexa.ThermostatController.ExternalTemperatureSensor", "mode")
        )
        if sensor_mode is not None:
            self._attr_extra_state_attributes = {"temperature_source": sensor_mode}

        self._sync_target_attrs()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        if self._attr_hvac_mode == HVACMode.OFF:
            # No dial is shown while off (see _sync_target_attrs), but guard
            # the service call directly too - Alexa won't act on it either.
            return

        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            self._single_target = temperature
            self._sync_target_attrs()
            self.async_write_ha_state()
            self._schedule_command(
                "single", f"set {self._attr_name} to {round(temperature)} degrees"
            )
            return

        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if low is not None:
            self._range_low = low
        if high is not None:
            self._range_high = high
        self._sync_target_attrs()
        self.async_write_ha_state()
        # Always resend the full range, even if only one handle moved -
        # Alexa needs both endpoints together in one combined command.
        self._schedule_command(
            "range",
            f"set {self._attr_name} range between"
            f" {round(self._range_low)} and {round(self._range_high)} degrees",
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self._sync_target_attrs()
        self.async_write_ha_state()
        if hvac_mode == HVACMode.OFF:
            mode_word = "off"
        elif hvac_mode == HVACMode.HEAT_COOL:
            mode_word = "auto"
        else:
            mode_word = hvac_mode.value
        await self._send_command(f"set {self._attr_name} to {mode_word}")

        # The remembered target/range is whatever we last saw or set - it
        # can be stale if the real setpoint was last changed outside HA.
        # Mode switching doesn't touch the setpoint itself, so pull the
        # real value now instead of waiting up to POLL_INTERVAL for it.
        if self._alexa_entity_id and hvac_mode != HVACMode.OFF:
            await self._async_poll_alexa_state()
