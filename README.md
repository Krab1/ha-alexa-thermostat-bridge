<p align="center"><img src="logo.svg" width="96" height="96" alt="Alexa Thermostat Bridge logo"></p>

# Alexa Thermostat Bridge

Home Assistant custom integration that wraps an Amazon Smart Thermostat as a real `climate` entity, including a dual-setpoint Auto range.

Amazon's thermostat has no local API and isn't supported by any HA-native integration. This bridges it by:

- **Reading** current temperature/humidity/mode/setpoints by polling Alexa's device-state API every 5 minutes, reusing the authenticated session from [alexa_media_player](https://github.com/alandtse/alexa_media_player) (HACS) — falls back to a plain temperature `sensor` if no Alexa entity ID is configured.
- **Writing** by relaying commands to an Echo via `media_player.play_media` (`media_content_type: custom`), the same mechanism as typing a command in the Alexa app — Alexa's own voice NLU routes it to the thermostat, so there's no private API to reverse-engineer. Dial-drag commands are debounced 8s before sending.

Target temperature, range, and HVAC mode persist across HA restarts via `RestoreEntity`. No YAML, no MQTT, no helper entities — setup is entirely through the UI.

## Requirements

- [alexa_media_player](https://github.com/alandtse/alexa_media_player) installed and logged in, exposing your thermostat as a temperature `sensor`.
- An Echo device (`media_player` entity) within earshot/routing range of the thermostat.

## Install

### Via HACS
1. HACS → Integrations → ⋮ → Custom repositories → add this repo URL, category "Integration".
2. Install "Alexa Thermostat Bridge", restart Home Assistant.

### Manual
Copy `custom_components/alexa_thermostat_bridge/` into your HA `config/custom_components/` folder, restart.

## Setup

Settings → Devices & Services → Add Integration → **Alexa Thermostat Bridge** → fill in:

- Name
- Temperature sensor (the `sensor.*` entity alexa_media_player created for the thermostat)
- Echo entity to relay commands through
- Min/max temperature

A `climate` entity appears immediately — add a **Thermostat** card for it.

## Known limitations

- Commands are fire-and-forget on send; confirmation only arrives on the next poll cycle (up to 5 minutes later) unless an Alexa entity ID is configured for polling.
- Without an Alexa entity ID configured, there's no readback of mode/setpoints/humidity at all — only the plain temperature sensor.
