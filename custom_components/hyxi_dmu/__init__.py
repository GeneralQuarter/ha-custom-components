"""Hyxi DMU integration: MQTT push with one local startup snapshot."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_HOST, CONF_PASSWORD, DEFAULT_PASSWORD, EVENT_PUSH
from .coordinator import HyxiDmuCoordinator
from .protocol import ProtocolError, parse_upload_frame

PLATFORMS = ["button", "sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a DMU from a config entry."""
    coordinator = HyxiDmuCoordinator(
        hass,
        entry.data[CONF_HOST],
        entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD),
        config_entry=entry,
    )

    @callback
    def _handle_push(event: Event) -> None:
        """Apply one uploadDataReq frame forwarded by the local broker."""
        payload = event.data.get("payload")
        if not isinstance(payload, str):
            return
        try:
            readings = parse_upload_frame(bytes.fromhex(payload))
        except (ProtocolError, ValueError) as err:
            _LOGGER.debug("Ignoring malformed MQTT push: %s", err)
            return
        if readings:
            coordinator.async_apply_push(readings)
            _LOGGER.debug("Applied MQTT push with %d inverter(s)", len(readings))

    entry.async_on_unload(hass.bus.async_listen(EVENT_PUSH, _handle_push))

    # One snapshot makes the entities useful immediately.  Do not schedule
    # more: local 333 requests suppress the DMU's own MQTT uploads.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        _LOGGER.warning("Initial DMU poll failed; waiting for MQTT push")
        coordinator.async_set_updated_data([])

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
