"""Number entity tests: the SOC window and its paired write."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.jackery_ble.coordinator import JackeryBleCoordinator
from custom_components.jackery_ble.number import (
    NUMBER_DESCRIPTIONS,
    JackeryBleSocLimit,
)
from tests.conftest import HOME_ADDRESS, LOGGER


def _number(coordinator: JackeryBleCoordinator, key: str) -> JackeryBleSocLimit:
    description = next(item for item in NUMBER_DESCRIPTIONS if item.key == key)
    return JackeryBleSocLimit(coordinator, description)


async def _coordinator(hass, config_entry) -> JackeryBleCoordinator:
    return JackeryBleCoordinator(hass, LOGGER, HOME_ADDRESS, config_entry)


@pytest.fixture
def recorder(monkeypatch):
    """Record the commands the entity writes."""

    def _install(coordinator):
        sent = []

        async def _send(command) -> None:
            sent.append(command)

        monkeypatch.setattr(coordinator, "async_send_command", _send)
        return sent

    return _install


async def test_limits_come_from_the_device(hass, home_config_entry) -> None:
    """Both ends of the window are read straight from the merged push data."""
    coordinator = await _coordinator(hass, home_config_entry)
    coordinator.data = {"socChgLimit": 80, "socDischgLimit": 33}

    assert _number(coordinator, "socChgLimit").native_value == 80
    assert _number(coordinator, "socDischgLimit").native_value == 33


async def test_missing_limit_reads_none(hass, home_config_entry) -> None:
    """A field the device has not reported must not look like a real value."""
    coordinator = await _coordinator(hass, home_config_entry)
    coordinator.data = {"socDischgLimit": 33}

    assert _number(coordinator, "socChgLimit").native_value is None


async def test_setting_the_ceiling_writes_both_limits(
    hass, home_config_entry, recorder
) -> None:
    """The device takes the pair in one frame, then we ask for the read-back."""
    coordinator = await _coordinator(hass, home_config_entry)
    coordinator.data = {"socChgLimit": 80, "socDischgLimit": 33}
    sent = recorder(coordinator)

    entity = _number(coordinator, "socChgLimit")
    await entity.async_set_native_value(90)

    assert sent[0].action_id == 3028
    assert sent[0].body["socChgLimit"] == 90
    assert sent[0].body["socDischgLimit"] == 33
    assert sent[1].action_id == 3011
    assert sent[1].ble_msg_type == 106
    assert coordinator.data["socChgLimit"] == 90
    assert entity.native_value == 90


async def test_setting_the_floor_keeps_the_ceiling(
    hass, home_config_entry, recorder
) -> None:
    """Changing the floor must resend the unchanged ceiling with it."""
    coordinator = await _coordinator(hass, home_config_entry)
    coordinator.data = {"socChgLimit": 80, "socDischgLimit": 33}
    sent = recorder(coordinator)

    await _number(coordinator, "socDischgLimit").async_set_native_value(25)

    assert sent[0].body["socChgLimit"] == 80
    assert sent[0].body["socDischgLimit"] == 25
    assert coordinator.data["socDischgLimit"] == 25


async def test_a_crossed_window_is_refused(
    hass, home_config_entry, recorder
) -> None:
    """Writing a floor at or above the ceiling is rejected before the BLE write."""
    coordinator = await _coordinator(hass, home_config_entry)
    coordinator.data = {"socChgLimit": 80, "socDischgLimit": 33}
    sent = recorder(coordinator)

    with pytest.raises(HomeAssistantError):
        await _number(coordinator, "socDischgLimit").async_set_native_value(80)
    with pytest.raises(HomeAssistantError):
        await _number(coordinator, "socChgLimit").async_set_native_value(33)

    assert sent == []


async def test_an_unknown_companion_is_refused(
    hass, home_config_entry, recorder
) -> None:
    """Half a window cannot be written: the device needs both values."""
    coordinator = await _coordinator(hass, home_config_entry)
    coordinator.data = {"socChgLimit": 80}
    sent = recorder(coordinator)

    with pytest.raises(HomeAssistantError):
        await _number(coordinator, "socChgLimit").async_set_native_value(90)

    assert sent == []


def test_slider_bounds_match_the_app() -> None:
    """The app allows 5-49 % for the floor and 50-100 % for the ceiling."""
    bounds = {
        item.key: (item.native_min_value, item.native_max_value)
        for item in NUMBER_DESCRIPTIONS
    }

    assert bounds["socDischgLimit"] == (5, 49)
    assert bounds["socChgLimit"] == (50, 100)
