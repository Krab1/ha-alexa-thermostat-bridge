from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

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
    CONF_POLL_INTERVAL,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

# HEAT_COOL, not AUTO, is HA's mode for a user-adjustable heat/cool range.
MODES = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]

ALEXA_MODE_TO_HVAC = {
    "OFF": HVACMode.OFF,
    "HEAT": HVACMode.HEAT,
    "COOL": HVACMode.COOL,
    "AUTO": HVACMode.HEAT_COOL,
}
HVAC_TO_ALEXA_MODE = {v: k for k, v in ALEXA_MODE_TO_HVAC.items()}

# Polling is the only way to notice a change made in the Alexa app or by
# voice - there's no push from Alexa - so the configured interval is what
# actually bounds "how stale can HA be". Set per config entry; see
# DEFAULT_POLL_INTERVAL in const.py.

# The round dial fires set_temperature on every drag step, not just on
# release - without this, each intermediate value became a real spoken
# Alexa command and they raced each other.
COMMAND_DEBOUNCE = 4

# After a mode switch, poll every MODE_CONFIRM_RETRY_DELAY seconds until
# Alexa reports the mode we just set, up to MODE_CONFIRM_MAX_ATTEMPTS times
# (~20s ceiling) - Alexa's backend doesn't apply the command instantly, so
# a single immediate poll can still read the old mode back.
MODE_CONFIRM_RETRY_DELAY = 2.5
MODE_CONFIRM_MAX_ATTEMPTS = 8

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
        # Clamped rather than trusted: the number selector bounds the UI, but
        # an entry hand-edited in .storage would otherwise be able to set a
        # 1-second interval and hammer Alexa's API.
        poll_seconds = config.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        try:
            poll_seconds = int(poll_seconds)
        except (TypeError, ValueError):
            poll_seconds = DEFAULT_POLL_INTERVAL
        self._poll_interval = timedelta(
            seconds=min(max(poll_seconds, MIN_POLL_INTERVAL), MAX_POLL_INTERVAL)
        )
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
        # Shrinking hvac_modes during a switch (see _lock_interactions)
        # only stops the *card* from offering other modes - it doesn't
        # stop a second async_set_hvac_mode call that's already in flight
        # (e.g. a click that landed before the locked state reached the
        # frontend). This actually rejects overlapping calls.
        self._mode_change_lock = asyncio.Lock()
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
                    self.hass, self._async_poll_alexa_state, self._poll_interval
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

    def _lock_interactions(self) -> None:
        # Shrinking hvac_modes to just the current mode removes every other
        # mode option from the selector, and clearing supported_features
        # drops the temperature dial - between them the card has nothing
        # left to interact with, but current_temperature/hvac_action still
        # render normally (unlike _attr_available = False, which blanks
        # the whole card). Instance attrs shadow the class-level defaults
        # set on AlexaBridgeClimate, so `del` in _unlock_interactions
        # cleanly restores them without duplicating the original values.
        self._attr_icon = "mdi:sync"
        self._attr_supported_features = ClimateEntityFeature(0)
        self._attr_hvac_modes = [self._attr_hvac_mode]
        self._attr_extra_state_attributes = {
            **self._attr_extra_state_attributes,
            "status": "updating",
        }

    def _unlock_interactions(self) -> None:
        del self._attr_icon
        del self._attr_supported_features
        del self._attr_hvac_modes
        self._attr_extra_state_attributes = {
            k: v
            for k, v in self._attr_extra_state_attributes.items()
            if k != "status"
        }

    def _local_sensor_temperature(self) -> float | None:
        state = self.hass.states.get(self._sensor_entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            # "unknown"/"unavailable" land here - treated as no reading.
            return None

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

    def _get_alexa_login(self) -> Any | None:
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

    async def _fetch_alexa_capabilities(self) -> dict[tuple[str, str], Any] | None:
        login = self._get_alexa_login()
        if login is None:
            _LOGGER.debug(
                "Alexa Thermostat Bridge: alexa_media login not found, skipping poll"
            )
            return None

        try:
            from alexapy import AlexaAPI
        except ImportError:
            _LOGGER.warning(
                "Alexa Thermostat Bridge: alexapy not importable - is"
                " alexa_media_player installed?"
            )
            return None

        try:
            response = await AlexaAPI.get_entity_state(
                login, entity_ids=[self._alexa_entity_id]
            )
        except Exception:  # noqa: BLE001 - third-party API, shape of failures unknown
            _LOGGER.debug("Alexa Thermostat Bridge: poll failed", exc_info=True)
            return None

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

        return capabilities or None

    def _apply_capabilities(
        self, capabilities: dict[tuple[str, str], Any], update_mode: bool = True
    ) -> None:
        mode_raw = capabilities.get(("Alexa.ThermostatController", "thermostatMode"))
        if update_mode and mode_raw in ALEXA_MODE_TO_HVAC:
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

        # The configured sensor wins: it's pushed on every change, while this
        # poll only runs every POLL_INTERVAL, so taking Alexa's copy
        # unconditionally would replace a fresh reading with a stale one.
        # Only fall back to Alexa's when that sensor has no usable value.
        precise_temp = self._fahrenheit(
            capabilities.get(("Alexa.TemperatureSensor", "preciseTemperature"))
        )
        local_temp = self._local_sensor_temperature()
        if local_temp is not None:
            self._attr_current_temperature = local_temp
        elif precise_temp is not None:
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
            # Merge, don't replace - this runs during the mode-confirm loop
            # too, and a wholesale reassign would drop the "status" key that
            # _lock_interactions set.
            self._attr_extra_state_attributes = {
                **self._attr_extra_state_attributes,
                "temperature_source": sensor_mode,
            }

        self._sync_target_attrs()

    async def _async_poll_alexa_state(
        self, now: datetime | None = None, update_mode: bool = True
    ) -> None:
        if self._mode_change_lock.locked():
            # A mode switch is mid-confirm. Alexa may not have applied it
            # yet, so this poll's mode would be the pre-change one and would
            # stomp the mode we're actively waiting on - the confirm loop is
            # already polling, let it own the state until it's done.
            return
        capabilities = await self._fetch_alexa_capabilities()
        if capabilities is None:
            return
        self._apply_capabilities(capabilities, update_mode=update_mode)
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
        if self._mode_change_lock.locked():
            _LOGGER.debug(
                "Alexa Thermostat Bridge: mode change already in progress,"
                " ignoring request for %s",
                hvac_mode,
            )
            return
        async with self._mode_change_lock:
            await self._async_set_hvac_mode(hvac_mode)

    async def _async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        previous_mode = self._attr_hvac_mode
        self._attr_hvac_mode = hvac_mode
        self._sync_target_attrs()
        self.async_write_ha_state()
        if hvac_mode == HVACMode.OFF:
            mode_word = "off"
        elif hvac_mode == HVACMode.HEAT_COOL:
            mode_word = "auto"
        else:
            mode_word = hvac_mode.value

        if not self._alexa_entity_id:
            # Nothing configured to confirm against - fire and forget, no
            # artificial wait.
            try:
                await self._send_command(f"set {self._attr_name} to {mode_word}")
            except Exception:
                # The optimistic mode above was never actually sent - don't
                # leave the card claiming a mode the thermostat never got.
                self._attr_hvac_mode = previous_mode
                self._sync_target_attrs()
                self.async_write_ha_state()
                raise
            return

        expected_mode = HVAC_TO_ALEXA_MODE[hvac_mode]

        # Lock the card for the confirm loop rather than going fully
        # "Unavailable" - the current temperature/mode stay visible, but
        # supported_features/hvac_modes shrink to nothing-selectable so it
        # can't be prodded again with a command already in flight. try/
        # finally is load-bearing - if Alexa never confirms or a call
        # raises, this must still restore both or the card is stuck locked
        # until the next HA restart.
        self._lock_interactions()
        self.async_write_ha_state()
        try:
            try:
                await self._send_command(f"set {self._attr_name} to {mode_word}")
            except Exception:
                self._attr_hvac_mode = previous_mode
                self._sync_target_attrs()
                raise

            last_seen_mode = None
            for _attempt in range(MODE_CONFIRM_MAX_ATTEMPTS):
                await asyncio.sleep(MODE_CONFIRM_RETRY_DELAY)
                capabilities = await self._fetch_alexa_capabilities()
                if capabilities is None:
                    continue
                last_seen_mode = capabilities.get(
                    ("Alexa.ThermostatController", "thermostatMode")
                )
                # Take the setpoints/humidity/action from every attempt, not
                # just the matching one - if the user changed something in
                # the Alexa app while we were waiting, that data is real and
                # discarding it would leave HA stale until the next poll.
                # update_mode stays False here: until Alexa reports our
                # requested mode it's still reporting the pre-change one,
                # which would stomp the mode we're waiting on.
                self._apply_capabilities(capabilities, update_mode=False)
                if last_seen_mode == expected_mode:
                    break
            else:
                # Never confirmed. If Alexa is consistently reporting some
                # other mode, it's telling us the switch didn't take (the
                # user may have changed it themselves mid-flight) - believe
                # Alexa over our optimistic value rather than displaying a
                # mode the thermostat never had.
                actual_mode = ALEXA_MODE_TO_HVAC.get(last_seen_mode)
                if actual_mode is not None and actual_mode != hvac_mode:
                    _LOGGER.warning(
                        "Alexa Thermostat Bridge: mode change to %s not"
                        " confirmed after %s attempts - Alexa reports %s,"
                        " using that",
                        hvac_mode,
                        MODE_CONFIRM_MAX_ATTEMPTS,
                        actual_mode,
                    )
                    self._attr_hvac_mode = actual_mode
                    self._sync_target_attrs()
                else:
                    _LOGGER.warning(
                        "Alexa Thermostat Bridge: mode change to %s not"
                        " confirmed after %s attempts (no usable mode read"
                        " back) - keeping the locally-set mode",
                        hvac_mode,
                        MODE_CONFIRM_MAX_ATTEMPTS,
                    )
        finally:
            self._unlock_interactions()
            self.async_write_ha_state()
