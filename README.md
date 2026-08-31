<p align="center"><img src="logo.svg" width="96" height="96" alt="Alexa Thermostat Bridge logo"></p>

# Alexa Thermostat Bridge

Home Assistant custom integration that wraps an Amazon Smart Thermostat as a real `climate` entity, including a dual-setpoint Auto range.

Amazon's thermostat has no local API and isn't supported by any HA-native integration. This bridges it by:

- **Reading** current temperature/humidity/mode/setpoints by polling Alexa's device-state API (every 60s by default, configurable), reusing the authenticated session from [alexa_media_player](https://github.com/alandtse/alexa_media_player) (HACS) — falls back to a plain temperature `sensor` if no Alexa entity ID is configured.
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
- Alexa entity ID (optional, but needed for any readback — see below)
- Poll interval, default 60s (15–3600)

A `climate` entity appears immediately — add a **Thermostat** card for it. All
of the above can be changed later via **Configure** on the integration.

### Poll interval

Alexa doesn't push state changes, so polling is the only way HA notices a
change you made in the Alexa app or by voice — the interval is effectively
"how stale can HA be". 60s is a good default; raise it if you'd rather Alexa's
API be hit less often, at the cost of slower pickup of external changes.

## Known limitations

- Setpoint changes are fire-and-forget on send; if Alexa rejects one, HA keeps showing the requested value until the next poll corrects it. Mode changes are confirmed explicitly (HA polls until Alexa reports the new mode, then unlocks the card).
- Without an Alexa entity ID configured, there's no readback of mode/setpoints/humidity at all — only the plain temperature sensor, and changes made in the Alexa app never reach HA.
- Changing a setpoint in the Alexa app during HA's few-second send delay will be overwritten by HA's pending value — last local edit wins.
