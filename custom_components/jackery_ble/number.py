"""Jackery BLE number entities: the battery SOC window.

The vault enforces a charge ceiling and a discharge floor on its own; both are
the sliders in the Jackery app. They are written together in one frame
(``3028/121``) and echoed back in ``3011/106``. The discharge floor is
exclusive: discharge stops when ``soc`` reaches it.
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import JackeryBleCoordinator
from .types import JackeryBleConfigEntry

#: Slider ranges taken from the app (apktool: ``activity_diy_ele_limit.xml``
#: sets ``smallValue=49`` / ``bigValue=50`` and the limit hint string states
#: the discharge lower limit is 5%-49%; the widget draws its scale out of 100).
MIN_DISCHARGE_SOC = 5
MAX_DISCHARGE_SOC = 49
MIN_CHARGE_SOC = 50
MAX_CHARGE_SOC = 100


class JackeryBleNumberEntityDescription(
    NumberEntityDescription, frozen_or_thawed=True
):
    """Number description carrying the companion limit key."""

    #: The other limit, which must be re-sent alongside this one.
    companion_key: str = ""


NUMBER_DESCRIPTIONS: list[JackeryBleNumberEntityDescription] = [
    JackeryBleNumberEntityDescription(
        key="socChgLimit",
        translation_key="socChgLimit",
        icon="mdi:battery-arrow-up",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=MIN_CHARGE_SOC,
        native_max_value=MAX_CHARGE_SOC,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        companion_key="socDischgLimit",
    ),
    JackeryBleNumberEntityDescription(
        key="socDischgLimit",
        translation_key="socDischgLimit",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=MIN_DISCHARGE_SOC,
        native_max_value=MAX_DISCHARGE_SOC,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        companion_key="socChgLimit",
    ),
]


class JackeryBleSocLimit(CoordinatorEntity[JackeryBleCoordinator], NumberEntity):
    """One end of the battery SOC window."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JackeryBleCoordinator,
        description: JackeryBleNumberEntityDescription,
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
    def native_value(self) -> float | None:
        """Return the device's current limit, or None when not reported yet."""
        value = (self.coordinator.data or {}).get(self.entity_description.key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Return True while the BLE link is healthy."""
        return self.coordinator.available()

    async def async_set_native_value(self, value: float) -> None:
        """Write both limits, keeping the window the right way round."""
        key = self.entity_description.key
        other_key = self.entity_description.companion_key
        new = round(value)

        data = self.coordinator.data or {}
        try:
            other = int(data[other_key])
        except (KeyError, TypeError, ValueError):
            raise HomeAssistantError(
                f"Cannot change {key}: the device has not reported "
                f"{other_key} yet, so the pair cannot be written safely"
            ) from None

        if key == "socChgLimit" and new <= other:
            raise HomeAssistantError(
                f"Maximum state of charge must stay above the minimum ({other} %)"
            )
        if key == "socDischgLimit" and new >= other:
            raise HomeAssistantError(
                f"Minimum state of charge must stay below the maximum ({other} %)"
            )

        charge, discharge = (new, other) if key == "socChgLimit" else (other, new)
        try:
            await self.coordinator.async_send_command(
                self.coordinator.commands.set_soc_limits(charge, discharge)
            )
            # 3011/106 is the only frame that echoes the limits, so ask for it
            # instead of waiting for the next scheduled read.
            await self.coordinator.async_send_command(
                self.coordinator.commands.read_device_info()
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Could not set {key} to {new} %: {err}"
            ) from err

        # Show the new value now; the 3011 reply replaces it with whatever the
        # device actually stored.
        current = dict(self.coordinator.data or {})
        current[key] = new
        self.coordinator.async_set_updated_data(current)


async def async_setup_entry(
    _: HomeAssistant, entry: JackeryBleConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Set up the SOC limit numbers for the vault (not the CT)."""
    coordinator = entry.runtime_data
    if coordinator.device_data.is_accessory:
        return

    add_entities(
        JackeryBleSocLimit(coordinator, description)
        for description in NUMBER_DESCRIPTIONS
    )
