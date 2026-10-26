"""Constants for the Jackery BLE integration."""

DOMAIN = "jackery_ble"
CONF_ENCRYPTION_KEY = "device_encryption_key"
CONF_DEVICE_DATA = "device_data"

# Follow-meter supervisor options.
CONF_SUPERVISOR_ENABLED = "supervisor_enabled"
CONF_RESERVE_PERCENT = "reserve_percent"

# The supervisor stays off until the user turns it on.
DEFAULT_SUPERVISOR_ENABLED = False

SERVICE_UUID = "0000bdee-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_DATA_WRITE = "0000ee01-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_DATA_NOTIFY = "0000ee02-0000-1000-8000-00805f9b34fb"
