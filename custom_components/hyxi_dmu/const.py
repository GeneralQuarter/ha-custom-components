"""Constants for the Hyxi DMU integration."""

DOMAIN = "hyxi_dmu"

#: Event fired by ``hyxi_mqtt`` for every ``uploadDataReq`` frame.
EVENT_PUSH = "hyxi_dmu_push"

CONF_HOST = "host"
CONF_PASSWORD = "password"

#: Factory local password; the DMU currently accepts any value.
DEFAULT_PASSWORD = "hyxi0607"

DEFAULT_PORT = 333
DEFAULT_SCAN_INTERVAL = 15
REQUEST_TIMEOUT = 12.0
