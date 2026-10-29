"""Sensors for the Hyxi DMU integration."""

from __future__ import annotations

import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HyxiDmuCoordinator
from .protocol import InverterReading

#: At startup the DMU can send one inverter first and the rest a moment later.
#: Publishing that partial total would look like a huge lifetime increase.
ENERGY_SETTLE_SECONDS = 120.0


def _monotonic_energy(
    last: float | None, value: float | None
) -> tuple[float | None, float | None]:
    """Return a stable lifetime value and the value to remember next time.

    A lifetime counter is only allowed to stay flat or increase.  The DMU
    reports ``0`` for sleeping inverters, so treating that as a real reading
    makes the Energy dashboard add the whole lifetime total the next morning.
    """
    if value is None or value <= 0:
        return last, last
    if last is not None and value < last:
        return last, last
    return value, value


class _InverterSensor(CoordinatorEntity[HyxiDmuCoordinator], SensorEntity):
    """Base class for sensors that belong to one microinverter."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HyxiDmuCoordinator, sn: int) -> None:
        """Constructor."""
        super().__init__(coordinator)
        self._sn = sn
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(sn))},
            "name": f"Hyxi inverter {str(sn)[-6:]}",
            "manufacturer": "Hyxi Power",
            "model": "Microinverter",
        }

    def _reading(self) -> InverterReading | None:
        """Current reading for this inverter, if the DMU still lists it."""
        for inverter in self.coordinator.data or []:
            if inverter.sn == self._sn:
                return inverter
        return None

    @property
    def available(self) -> bool:
        """Available only while the inverter is in the DMU's list."""
        return super().available and self._reading() is not None


class InverterPowerSensor(_InverterSensor):
    """Live DC power for one microinverter (sum of its MPPTs)."""

    _attr_translation_key = "power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: HyxiDmuCoordinator, sn: int) -> None:
        """Constructor."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_power"

    @property
    def native_value(self) -> float | None:
        """Sum of the inverter's MPPT powers."""
        reading = self._reading()
        return None if reading is None else reading.power_w


class InverterEnergySensor(_InverterSensor):
    """Lifetime energy for one microinverter."""

    _attr_translation_key = "energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: HyxiDmuCoordinator, sn: int) -> None:
        """Constructor."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_energy"
        self._last_energy_kwh: float | None = None

    @property
    def native_value(self) -> float | None:
        """Lifetime energy, summed over the MPPTs."""
        reading = self._reading()
        if reading is None:
            return None
        value, self._last_energy_kwh = _monotonic_energy(
            self._last_energy_kwh, reading.energy_kwh
        )
        return value


class _DmuSensor(CoordinatorEntity[HyxiDmuCoordinator], SensorEntity):
    """Base class for the site-wide sensors on the DMU device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: HyxiDmuCoordinator, entry: ConfigEntry
    ) -> None:
        """Constructor."""
        super().__init__(coordinator)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Hyxi DMU",
            "manufacturer": "Hyxi Power",
            "model": "HYX-DMU-W",
        }
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"


class DmuPowerSensor(_DmuSensor):
    """Combined power of every inverter on the DMU."""

    _attr_translation_key = "microinverter_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:solar-panel-large"

    @property
    def native_value(self) -> float:
        """Sum of the inverters' MPPT powers."""
        return round(
            sum(inverter.power_w for inverter in self.coordinator.data or []), 1
        )


class DmuEnergySensor(_DmuSensor):
    """Combined lifetime energy of every inverter on the DMU."""

    _attr_translation_key = "microinverter_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: HyxiDmuCoordinator, entry: ConfigEntry
    ) -> None:
        """Constructor."""
        super().__init__(coordinator, entry)
        self._last_energy_kwh: float | None = None
        self._first_data_at: float | None = None

    @property
    def native_value(self) -> float | None:
        """Sum the inverters' lifetime energy, never falling back to zero.

        A temporary ``0`` here looks like a counter reset to the Energy
        dashboard, which then adds the whole lifetime total again on the next
        push.  The DMU reports exactly that for sleeping inverters.
        """
        if not self.coordinator.data:
            return None

        now = time.monotonic()
        if self._first_data_at is None:
            self._first_data_at = now
        if now - self._first_data_at < ENERGY_SETTLE_SECONDS:
            # Wait for the DMU to list every inverter before publishing the
            # aggregate; a one-inverter total would otherwise be a huge jump.
            return self._last_energy_kwh

        total = round(
            sum(inverter.energy_kwh for inverter in self.coordinator.data),
            3,
        )
        value, self._last_energy_kwh = _monotonic_energy(
            self._last_energy_kwh, total
        )
        return value


async def async_setup_entry(
    _: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Create the DMU sensors and one pair per inverter, now or when it appears."""
    coordinator: HyxiDmuCoordinator = entry.runtime_data
    known: set[int] = set()

    def add_missing() -> None:
        """Add entities for inverters the DMU has reported since the last scan."""
        new_entities: list[SensorEntity] = []
        for inverter in coordinator.data or []:
            if inverter.sn in known:
                continue
            known.add(inverter.sn)
            new_entities.append(InverterPowerSensor(coordinator, inverter.sn))
            new_entities.append(InverterEnergySensor(coordinator, inverter.sn))
        if new_entities:
            add_entities(new_entities)

    add_entities(
        [DmuPowerSensor(coordinator, entry), DmuEnergySensor(coordinator, entry)]
    )
    add_missing()
    entry.async_on_unload(coordinator.async_add_listener(add_missing))
