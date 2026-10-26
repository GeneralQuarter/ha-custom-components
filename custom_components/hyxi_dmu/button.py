"""Button entities for the Hyxi DMU integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.recorder import DATA_INSTANCE

from .const import DOMAIN
from .coordinator import HyxiDmuCoordinator

TOTAL_ENERGY = "sensor.hyxi_dmu_microinverter_energy"


class _DmuButton(ButtonEntity):
    """Base for buttons attached to the DMU device."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Constructor."""
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
        """Clear the microinverter energy statistics so the dashboard can rebuild."""
        recorder = self.hass.data.get(DATA_INSTANCE)
        if recorder is None:
            raise HomeAssistantError("Recorder is not available")
        recorder.async_clear_statistics([TOTAL_ENERGY])


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
