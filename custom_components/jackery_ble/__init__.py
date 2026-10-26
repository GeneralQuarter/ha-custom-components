"""The Jackery BLE integration.

Home Assistant imports are intentionally deferred so the protocol and probe
tooling can be imported without a Home Assistant installation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .supervisor import FollowMeterSupervisor
    from .types import JackeryBleConfigEntry

PLATFORMS = ["sensor", "number"]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Jackery BLE from a config entry."""
    from .const import CONF_DEVICE_DATA
    from .coordinator import JackeryBleCoordinator

    address = entry.unique_id
    assert address is not None

    device_data = entry.data.get(CONF_DEVICE_DATA)
    assert device_data is not None

    coordinator = JackeryBleCoordinator(
        hass,
        _LOGGER,
        address,
        entry,
    )

    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()

    supervisor = _async_create_supervisor(hass, entry, coordinator)
    if supervisor is not None:
        await supervisor.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _async_create_supervisor(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
) -> FollowMeterSupervisor | None:
    """Build the follow-meter supervisor when the vault entry enables it."""
    from .const import (
        CONF_RESERVE_PERCENT,
        CONF_SUPERVISOR_ENABLED,
        DEFAULT_SUPERVISOR_ENABLED,
    )
    from .control import DEFAULT_RESERVE_PERCENT
    from .supervisor import FollowMeterSupervisor

    if coordinator.device_data.is_accessory:
        return None

    options = entry.options
    if not options.get(CONF_SUPERVISOR_ENABLED, DEFAULT_SUPERVISOR_ENABLED):
        return None

    supervisor = FollowMeterSupervisor(
        hass,
        coordinator,
        reserve_percent=int(
            options.get(CONF_RESERVE_PERCENT, DEFAULT_RESERVE_PERCENT)
        ),
    )
    coordinator.supervisor = supervisor
    return supervisor


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so supervisor options take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JackeryBleConfigEntry) -> bool:
    """Unload a config entry."""
    supervisor = getattr(entry.runtime_data, "supervisor", None)
    if supervisor is not None:
        await supervisor.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_shutdown()
    return unload_ok
