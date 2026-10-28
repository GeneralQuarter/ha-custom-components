"""Push-path tests for the Hyxi DMU integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hyxi_dmu import protocol as p
from custom_components.hyxi_dmu.const import (
    CONF_HOST,
    CONF_PASSWORD,
    DOMAIN,
    EVENT_PUSH,
)

HOST = "192.0.2.13"
PASSWORD = "secret"
SN = 12345678901234567


@pytest.fixture(autouse=True)
def _enable_hyxi_custom_integration(enable_custom_integrations: None) -> None:
    """Allow loading ``custom_components`` when this file runs without conftest."""


def _upload_frame(energy: int = 61479, power: int = 665) -> bytes:
    entry = p._field(
        1, 2, p._field(1, 0, SN) + p._field(7, 0, 1000)
    ) + p._field(
        3,
        2,
        p._field(1, 0, 1)
        + p._field(2, 0, 320)
        + p._field(4, 0, power)
        + p._field(5, 0, energy),
    )
    body = p._field(7, 2, entry)
    payload = (
        p._field(1, 0, 11111111111111111)
        + p._field(2, 0, 1)
        + p._field(3, 0, p.OP_UPLOAD)
        + p._field(6, 0, 1793018100)
        + p._field(11, 2, body)
    )
    return (
        b"PB"
        + len(payload).to_bytes(2, "big")
        + p.crc16_modbus(payload).to_bytes(2, "big")
        + payload
    )


async def test_push_event_updates_the_coordinator(
    hass: HomeAssistant, monkeypatch
) -> None:
    """An uploadDataReq forwarded by the broker replaces the snapshot."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator._async_update_data",
        AsyncMock(return_value=[]),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Hyxi DMU ({HOST})",
        unique_id=HOST,
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # No snapshot yet: energy must be unknown, not a fake 0 that the Energy
    # dashboard would treat as a counter reset.
    assert hass.states.get("sensor.hyxi_dmu_microinverter_energy").state == "unknown"

    hass.bus.async_fire(
        EVENT_PUSH,
        {"sn": "60101252400000", "payload": _upload_frame().hex()},
    )
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert [reading.sn for reading in coordinator.data] == [SN]
    assert coordinator.data[0].power_w == 66.5
    assert coordinator.data[0].energy_kwh == 614.79
    assert hass.states.get("sensor.hyxi_dmu_microinverter_energy").state == "614.79"


async def test_sleeping_inverter_zero_does_not_reset_lifetime_energy(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The DMU reports energy 0 at night; that must not look like a reset."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator._async_update_data",
        AsyncMock(return_value=[]),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Hyxi DMU ({HOST})",
        unique_id=HOST,
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.bus.async_fire(
        EVENT_PUSH,
        {"sn": "60101252400000", "payload": _upload_frame().hex()},
    )
    await hass.async_block_till_done()

    inverter_energy = next(
        state.entity_id
        for state in hass.states.async_all("sensor")
        if "234567" in state.entity_id and state.entity_id.endswith("_energy")
    )
    assert hass.states.get(inverter_energy).state == "614.79"

    hass.bus.async_fire(
        EVENT_PUSH,
        {
            "sn": "60101252400000",
            "payload": _upload_frame(energy=0, power=0).hex(),
        },
    )
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert coordinator.data[0].power_w == 0.0
    assert coordinator.data[0].energy_kwh == 614.79
    assert hass.states.get("sensor.hyxi_dmu_microinverter_energy").state == "614.79"
    assert hass.states.get(inverter_energy).state == "614.79"


async def test_malformed_push_is_ignored(hass: HomeAssistant, monkeypatch) -> None:
    """A non-hex or corrupt payload leaves the last good snapshot in place."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator._async_update_data",
        AsyncMock(return_value=[]),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Hyxi DMU ({HOST})",
        unique_id=HOST,
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    before = coordinator.data
    hass.bus.async_fire(EVENT_PUSH, {"payload": "not-hex"})
    await hass.async_block_till_done()

    assert coordinator.data is before
