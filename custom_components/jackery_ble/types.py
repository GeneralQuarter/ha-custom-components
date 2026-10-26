"""Support for jackery ble."""

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import JackeryBleCoordinator

type JackeryBleConfigEntry = ConfigEntry[JackeryBleCoordinator]
