from __future__ import annotations

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ECHO_ENTITY, CONF_MAX_TEMP, CONF_MIN_TEMP, CONF_TEMPERATURE_SENSOR

# HEAT_COOL, not AUTO, is HA's mode for a user-adjustable heat/cool range.
MODES = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AlexaBridgeClimate(hass, entry)])


class AlexaBridgeClimate(ClimateEntity, RestoreEntity):
    _attr_hvac_modes = MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    )
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_should_poll = False

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
        self._attr_hvac_mode = HVACMode.OFF
        mid = (self._attr_min_temp + self._attr_max_temp) / 2
        self._attr_target_temperature = mid
        self._attr_target_temperature_low = mid - 2
        self._attr_target_temperature_high = mid + 2
        self._attr_current_temperature = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            if last_state.state in MODES:
                self._attr_hvac_mode = HVACMode(last_state.state)
            last_target = last_state.attributes.get(ATTR_TEMPERATURE)
            if last_target is not None:
                self._attr_target_temperature = last_target
            last_low = last_state.attributes.get(ATTR_TARGET_TEMP_LOW)
            if last_low is not None:
                self._attr_target_temperature_low = last_low
            last_high = last_state.attributes.get(ATTR_TARGET_TEMP_HIGH)
            if last_high is not None:
                self._attr_target_temperature_high = last_high

        sensor_state = self.hass.states.get(self._sensor_entity_id)
        if sensor_state is not None:
            self._update_current_temperature(sensor_state)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._sensor_entity_id], self._handle_sensor_change
            )
        )

    @callback
    def _handle_sensor_change(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is not None:
            self._update_current_temperature(new_state)
            self.async_write_ha_state()

    def _update_current_temperature(self, state: State) -> None:
        try:
            self._attr_current_temperature = float(state.state)
        except ValueError:
            self._attr_current_temperature = None

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

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            self._attr_target_temperature = temperature
            self.async_write_ha_state()
            await self._send_command(
                f"set {self._attr_name} to {round(temperature)} degrees"
            )
            return

        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if low is not None:
            self._attr_target_temperature_low = low
        if high is not None:
            self._attr_target_temperature_high = high
        self.async_write_ha_state()
        # Untested phrasing - Alexa's range-setpoint voice grammar isn't
        # documented; adjust these two strings if the thermostat ignores them.
        if low is not None:
            await self._send_command(f"set {self._attr_name} heat to {round(low)} degrees")
        if high is not None:
            await self._send_command(f"set {self._attr_name} cool to {round(high)} degrees")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()
        if hvac_mode == HVACMode.OFF:
            mode_word = "off"
        elif hvac_mode == HVACMode.HEAT_COOL:
            mode_word = "auto"
        else:
            mode_word = hvac_mode.value
        await self._send_command(f"set {self._attr_name} to {mode_word}")
