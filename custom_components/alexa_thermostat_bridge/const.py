DOMAIN = "alexa_thermostat_bridge"

CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_ECHO_ENTITY = "echo_entity"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_ALEXA_ENTITY_ID = "alexa_entity_id"
CONF_POLL_INTERVAL = "poll_interval"

DEFAULT_MIN_TEMP = 60
DEFAULT_MAX_TEMP = 85

# Seconds between reads of Alexa's device state. This is the only thing that
# bounds how stale HA can be after a change made in the Alexa app or by
# voice - there's no push - so the default is deliberately tight.
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 15
MAX_POLL_INTERVAL = 3600
