from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ECHO_ENTITY


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AlexaCommandTester(hass, entry)])


class AlexaCommandTester(TextEntity):
    """Type any phrase here to send it verbatim to the Echo as a custom
    text command - for testing which voice phrasing Alexa actually accepts,
    without going through Developer Tools each time."""

    _attr_should_poll = False
    _attr_native_max = 255
    _attr_mode = TextMode.TEXT
    _attr_icon = "mdi:message-text-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        config = {**entry.data, **entry.options}
        self._echo_entity_id = config[CONF_ECHO_ENTITY]
        self._attr_unique_id = f"{entry.entry_id}_command_tester"
        self._attr_name = f"{entry.data['name']} Command Tester"
        self._attr_native_value = ""

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        await self.hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": self._echo_entity_id,
                "media_content_id": value,
                "media_content_type": "custom",
            },
            blocking=True,
        )
