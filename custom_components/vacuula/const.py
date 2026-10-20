"""Constants for the Vacuula integration."""

from homeassistant.const import Platform

DOMAIN = "vacuula"
CONF_URL = "url"
# Vacuula's own default app port (standalone and Home Assistant app) is 80.
DEFAULT_URL = "http://vacuula.local"
GEN3 = "neato.gen3"
GEN4 = "neato.gen4"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VACUUM,
]
