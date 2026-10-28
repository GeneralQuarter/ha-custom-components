"""Button entities for the Hyxi DMU integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.recorder import DATA_INSTANCE

from .const import DOMAIN
from .coordinator import HyxiDmuCoordinator


class _DmuButton(ButtonEntity):
    """Base for buttons attached to the DMU device."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Constructor."""
        self._entry = entry
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Hyxi DMU",
            "manufacturer": "Hyxi Power",
            "model": "HYX-DMU-W",
        }
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"


class DmuRestartButton(_DmuButton):
    """Reboot the DMU via its local C005 command."""

    _attr_translation_key = "restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_icon = "mdi:restart"

    def __init__(
        self, coordinator: HyxiDmuCoordinator, entry: ConfigEntry
    ) -> None:
        """Constructor."""
        super().__init__(entry)
        self.coordinator = coordinator

    async def async_press(self) -> None:
        """Send the restart command."""
        await self.coordinator.async_restart()


class DmuResetEnergyButton(_DmuButton):
    """Delete the microinverter energy's long-term statistics."""

    _attr_translation_key = "reset_energy_stats"
    _attr_icon = "mdi:delete-sweep"

    async def async_press(self) -> None:
        """Clear the lifetime energy statistics so the dashboard can rebuild."""
        recorder = self.hass.data.get(DATA_INSTANCE)
        if recorder is None:
            raise HomeAssistantError("Recorder is not available")

        registry = er.async_get(self.hass)
        coordinator: HyxiDmuCoordinator = self._entry.runtime_data
        unique_ids = {f"{self._entry.entry_id}_microinverter_energy"}
        unique_ids.update(
            f"{inverter.sn}_energy" for inverter in coordinator.data or []
        )
        statistic_ids = [
            entity_id
            for unique_id in unique_ids
            if (
                entity_id := registry.async_get_entity_id(
                    "sensor", DOMAIN, unique_id
                )
            )
        ]
        if not statistic_ids:
            raise HomeAssistantError("No energy sensors are registered")
        recorder.async_clear_statistics(statistic_ids)


async def async_setup_entry(
    _: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Create the DMU maintenance buttons."""
    coordinator: HyxiDmuCoordinator = entry.runtime_data
    add_entities(
        [
            DmuRestartButton(coordinator, entry),
            DmuResetEnergyButton(entry),
        ]
    )
