"""Sensor entity tests: unit scaling, nested keys and device wiring."""

from __future__ import annotations

import pytest

from custom_components.jackery_ble.const import DOMAIN
from custom_components.jackery_ble.coordinator import JackeryBleCoordinator
from custom_components.jackery_ble.sensor import (
    CT_SENSOR_DESCRIPTIONS,
    HOME_SENSOR_DESCRIPTIONS,
    JackeryBleSensor,
    JackeryBleSensorEntityDescription,
    async_setup_entry,
)
from tests.conftest import CT_ADDRESS, CT_SN, HOME_ADDRESS, HOME_SN, LOGGER


def _description(
    descriptions: list[JackeryBleSensorEntityDescription], key: str
) -> JackeryBleSensorEntityDescription:
    return next(description for description in descriptions if description.key == key)


def _sensor(
    coordinator: JackeryBleCoordinator,
    descriptions: list[JackeryBleSensorEntityDescription],
    key: str,
) -> JackeryBleSensor:
    return JackeryBleSensor(coordinator, _description(descriptions, key))


async def _coordinator(hass, config_entry, address: str) -> JackeryBleCoordinator:
    return JackeryBleCoordinator(hass, LOGGER, address, config_entry)


async def test_home_values_are_passed_through(hass, home_config_entry) -> None:
    """Fields the device already reports in SI units are untouched."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {"soc": 32, "pvPw": 1290, "inGridSidePw": 728}

    assert _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "soc").native_value == 32
    assert _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "pvPw").native_value == 1290
    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "outGridSidePw").native_value
        is None
    )


async def test_vault_output_is_pv_plus_discharge_minus_charge(
    hass, home_config_entry
) -> None:
    """The published figure comes only from the vault's own measurements."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {"pvPw": 192, "batInPw": 0, "batOutPw": 885}

    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "vaultOutputPw").native_value
        == 1077
    )


async def test_vault_output_ignores_derived_grid_and_plan_fields(
    hass, home_config_entry
) -> None:
    """`otherLoadPw` and the grid pair mirror the plan, not a measurement."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {
        "otherLoadPw": 1010,
        "pvPw": 400,
        "batInPw": 50,
        "batOutPw": 200,
        "inGridSidePw": 900,
        "outGridSidePw": 0,
    }

    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "vaultOutputPw").native_value
        == 550
    )


async def test_vault_output_is_none_when_a_term_is_missing(
    hass, home_config_entry
) -> None:
    """A partial frame must not produce a plausible-looking number."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {"pvPw": 192, "batOutPw": 885}

    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "vaultOutputPw").native_value
        is None
    )


async def test_grid_side_values_are_passed_through(hass, home_config_entry) -> None:
    """The measured pair is exposed verbatim, in both directions."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {"inGridSidePw": 670, "outGridSidePw": 0}

    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "inGridSidePw").native_value
        == 670
    )
    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "outGridSidePw").native_value
        == 0
    )


@pytest.mark.parametrize(
    ("key", "wire_value", "expected"),
    [
        ("cellTemp", 173, 17.3),
        ("batDisChgEgy", 12019, 120.19),
        ("pvEgy", 123456, 1234.56),
    ],
)
async def test_home_values_are_scaled(
    hass, home_config_entry, key: str, wire_value: int, expected: float
) -> None:
    """Temperatures are deci-degrees and energy counters are hundredths of a kWh."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {key: wire_value}

    assert _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, key).native_value == expected


async def test_nested_pv_channel_is_resolved(hass, home_config_entry) -> None:
    """Per-panel power lives under its own sub-object."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {"pv1": {"pvPw": 544}}

    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "pv1_pvPw").native_value == 544
    )


async def test_missing_and_null_values_become_none(hass, home_config_entry) -> None:
    """A partial payload must not raise or invent a reading."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator.data = {"soc": None}

    assert _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "soc").native_value is None
    assert _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "pvPw").native_value is None
    assert (
        _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "pv1_pvPw").native_value is None
    )


async def test_no_data_yields_none(hass, home_config_entry) -> None:
    """Before the first frame every entity is empty."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    assert _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "soc").native_value is None


@pytest.mark.parametrize(
    ("key", "wire_value", "expected"),
    [
        ("curr", 25, 2.5),
        ("fact", 857, 0.857),
        ("power", 780, 780),
        ("volt", 231, 231),
    ],
)
async def test_ct_values_are_scaled(
    hass, ct_config_entry, key: str, wire_value: int, expected: float
) -> None:
    """The meter reports deciamperes and thousandths of a power factor."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)
    coordinator.data = {key: wire_value}

    assert _sensor(coordinator, CT_SENSOR_DESCRIPTIONS, key).native_value == expected


async def test_reactive_power_is_disabled_by_default(hass, ct_config_entry) -> None:
    """The reported value does not reconcile with the other phases yet."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)

    entity = _sensor(coordinator, CT_SENSOR_DESCRIPTIONS, "rep")

    assert entity.entity_registry_enabled_default is False


async def test_entity_identity_uses_the_serial(hass, home_config_entry) -> None:
    """Unique IDs and device identifiers are derived from the serial number."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    entity = _sensor(coordinator, HOME_SENSOR_DESCRIPTIONS, "soc")

    assert entity.unique_id == f"{HOME_SN}_soc"
    assert entity.device_info["identifiers"] == {(DOMAIN, HOME_SN)}
    assert entity.device_info["model"] == "SolarVault 3 Pro Max"
    assert entity.has_entity_name is True


async def test_entities_are_unavailable_without_a_connection(
    hass, ct_config_entry
) -> None:
    """A coordinator that never connected must not publish stale state."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)
    coordinator.data = {"power": 100}

    assert _sensor(coordinator, CT_SENSOR_DESCRIPTIONS, "power").available is False


async def test_sensor_platform_branches_on_device_family(hass, home_config_entry, ct_config_entry) -> None:
    """The vault and the meter expose different entity sets."""
    home_coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    ct_coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)
    home_config_entry.runtime_data = home_coordinator
    ct_config_entry.runtime_data = ct_coordinator

    home_entities: list[JackeryBleSensor] = []
    ct_entities: list[JackeryBleSensor] = []
    await async_setup_entry(hass, home_config_entry, home_entities.extend)
    await async_setup_entry(hass, ct_config_entry, ct_entities.extend)

    assert len(home_entities) == len(HOME_SENSOR_DESCRIPTIONS)
    assert len(ct_entities) == len(CT_SENSOR_DESCRIPTIONS)
    assert {entity.unique_id for entity in ct_entities} == {
        f"{CT_SN}_{description.key}" for description in CT_SENSOR_DESCRIPTIONS
    }
