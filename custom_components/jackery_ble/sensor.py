"""Jackery Sensor."""

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .control import vault_output_pw
from .coordinator import JackeryBleCoordinator
from .types import JackeryBleConfigEntry


def _divide_by(divisor: float):
    def _f(value: Any) -> float | None:
        try:
            return float(value) / divisor
        except (TypeError, ValueError):
            return None

    return _f


# Vault live energy counters are hundredths of one kWh; the CT counters are Wh.
LIVE_ENERGY_UNITS_PER_KWH = 100
CT_ENERGY_UNITS_PER_KWH = 1000


class JackeryBleSensorEntityDescription(SensorEntityDescription, frozen_or_thawed=True):
    """Jackery Ble Sensor Entity Description."""

    transform: Callable[[Any], Any] = lambda value: value

    #: Compute the value from the whole merged payload instead of a single
    #: field. Used for values the device does not report (e.g. vault output).
    value_fn: Callable[[dict[str, Any]], Any] | None = None


def _energy_sensor(
    key: str, units_per_kwh: int = LIVE_ENERGY_UNITS_PER_KWH, icon: str | None = None
) -> JackeryBleSensorEntityDescription:
    """Build a lifetime energy counter description."""
    return JackeryBleSensorEntityDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        transform=_divide_by(units_per_kwh),
        icon=icon,
    )


CT_SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    JackeryBleSensorEntityDescription(
        key="volt",
        translation_key="volt",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    JackeryBleSensorEntityDescription(
        key="curr",
        translation_key="curr",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        # Wire value is deciamperes: 25 -> 2.5 A (matches ap/volt ratio).
        transform=_divide_by(10),
    ),
    JackeryBleSensorEntityDescription(
        key="power",
        translation_key="power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    JackeryBleSensorEntityDescription(
        key="rep",
        translation_key="rep",
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        device_class=SensorDeviceClass.REACTIVE_POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Observed values (~-104) do not reconcile with sqrt(ap^2 - power^2)
        # (~320 var), so keep this out of the way until the firmware definition
        # is confirmed.
        entity_registry_enabled_default=False,
    ),
    JackeryBleSensorEntityDescription(
        key="freq",
        translation_key="freq",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    JackeryBleSensorEntityDescription(
        key="fact",
        translation_key="fact",
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        # Wire value is thousandths: 857 -> 0.857.
        transform=_divide_by(1000),
    ),
    JackeryBleSensorEntityDescription(
        key="ap",
        translation_key="ap",
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        device_class=SensorDeviceClass.APPARENT_POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
]

HOME_SENSOR_DESCRIPTIONS: list[SensorEntityDescription] = [
    JackeryBleSensorEntityDescription(
        key="soc",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    # The app's live SOC is ``soc``. ``batSoc`` is a separately offset BMS
    # value used for calibration/configuration, not the user-facing charge.
    JackeryBleSensorEntityDescription(
        key="batSoc",
        translation_key="batSoc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    JackeryBleSensorEntityDescription(
        key="swEpsInPw",
        translation_key="swEpsInPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    JackeryBleSensorEntityDescription(
        key="swEpsOutPw",
        translation_key="swEpsOutPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    JackeryBleSensorEntityDescription(
        key="cellTemp",
        translation_key="cellTemp",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        transform=_divide_by(10),
    ),
    # Only the grid-side pair is a real measurement. ``inOngridPw`` /
    # ``outOngridPw`` / ``gridOutPw`` are plan-derived and were observed
    # contradicting these values in the same second, so they are not exposed.
    JackeryBleSensorEntityDescription(
        key="inGridSidePw",
        translation_key="inGridSidePw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:transmission-tower-import",
    ),
    JackeryBleSensorEntityDescription(
        key="outGridSidePw",
        translation_key="outGridSidePw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:transmission-tower-export",
    ),
    JackeryBleSensorEntityDescription(
        key="batInPw",
        translation_key="batInPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:battery-arrow-up",
    ),
    JackeryBleSensorEntityDescription(
        key="batOutPw",
        translation_key="batOutPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:battery-arrow-down",
    ),
    JackeryBleSensorEntityDescription(
        key="stackInPw",
        translation_key="stackInPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    JackeryBleSensorEntityDescription(
        key="stackOutPw",
        translation_key="stackOutPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    JackeryBleSensorEntityDescription(
        key="pvPw",
        translation_key="pvPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel-large",
    ),
    JackeryBleSensorEntityDescription(
        key="pv1_pvPw",
        translation_key="pv1_pvPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    JackeryBleSensorEntityDescription(
        key="pv2_pvPw",
        translation_key="pv2_pvPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    JackeryBleSensorEntityDescription(
        key="pv3_pvPw",
        translation_key="pv3_pvPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    JackeryBleSensorEntityDescription(
        key="pv4_pvPw",
        translation_key="pv4_pvPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:solar-panel",
    ),
    JackeryBleSensorEntityDescription(
        key="vaultOutputPw",
        translation_key="vaultOutputPw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:home-lightning-bolt",
        # PV + discharge - charge. The grid-side fields are derived from the
        # plan when no Smart Meter is bound, so they cannot be used to work out
        # the house load. This is the vault's own contribution, a lower bound.
        value_fn=lambda data: vault_output_pw(
            data.get("pvPw"),
            data.get("batInPw"),
            data.get("batOutPw"),
        ),
    ),
    # Supervisor setpoints: what the loop currently publishes, and the floor it
    # is not allowed to cross. Diagnostic rather than measurements.
    JackeryBleSensorEntityDescription(
        key="energyPlanPw",
        translation_key="energyPlanPw",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:gauge",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    JackeryBleSensorEntityDescription(
        key="socDischgLimit",
        translation_key="socDischgLimit",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery-lock",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _energy_sensor("batChgEgy"),
    _energy_sensor("batDisChgEgy"),
    _energy_sensor("inOngridEgy"),
    _energy_sensor("outOngridEgy"),
    _energy_sensor("inEpsEgy"),
    _energy_sensor("outEpsEgy"),
    _energy_sensor("pvEgy", icon="mdi:solar-panel"),
    _energy_sensor("pv1Egy", icon="mdi:solar-panel"),
    _energy_sensor("pv2Egy", icon="mdi:solar-panel"),
    _energy_sensor("pv3Egy", icon="mdi:solar-panel"),
    _energy_sensor("pv4Egy", icon="mdi:solar-panel"),
    _energy_sensor("acOtBatEgy"),
    _energy_sensor("acOtOngridEgy"),
    _energy_sensor("pvOtBatEgy"),
    _energy_sensor("pvOtAcEgy"),
    _energy_sensor("pvOtOngridEgy"),
    _energy_sensor("ongridOtAcLoadEgy"),
    _energy_sensor("batOtAcEgy"),
    _energy_sensor("batOtGridEgy"),
    _energy_sensor("ongridOtBatEgy"),
]


class JackeryBleSensor(CoordinatorEntity[JackeryBleCoordinator], SensorEntity):
    """Jackery Ble Sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JackeryBleCoordinator,
        description: JackeryBleSensorEntityDescription,
    ) -> None:
        """Constructor."""
        super().__init__(coordinator)

        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_data.sn}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.device_data.sn)},
            "name": coordinator.device_data.sn,
            "manufacturer": "Jackery",
            "model": coordinator.device_data.model_data.name,
        }

    @property
    def native_value(self):
        """Get native value."""
        data = self.coordinator.data

        if not data:
            return None

        if self.entity_description.value_fn is not None:
            return self.entity_description.transform(
                self.entity_description.value_fn(data)
            )

        key = self.entity_description.key
        subKey = None

        if "_" in key:
            key, subKey = key.split("_")

        if subKey is None:
            return self.entity_description.transform(data.get(key, None))

        return self.entity_description.transform(data.get(key, {}).get(subKey, None))

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available()


async def async_setup_entry(
    _: HomeAssistant, entry: JackeryBleConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Setup Jackery BLE sensors."""
    coordinator = entry.runtime_data

    entities = [
        JackeryBleSensor(coordinator, description)
        for description in (
            CT_SENSOR_DESCRIPTIONS
            if coordinator.device_data.is_accessory
            else HOME_SENSOR_DESCRIPTIONS
        )
    ]
    add_entities(entities)
