"""Button tests for the Hyxi DMU integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import DATA_INSTANCE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hyxi_dmu.const import CONF_HOST, CONF_PASSWORD, DOMAIN
from custom_components.hyxi_dmu.coordinator import HyxiDmuCoordinator

HOST = "192.0.2.13"
PASSWORD = "secret"


@pytest.fixture(autouse=True)
def _enable_hyxi_custom_integration(enable_custom_integrations: None) -> None:
    """Allow loading ``custom_components`` when this file runs without conftest."""


async def test_restart_button_presses_c005(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Pressing the button calls the coordinator's restart helper."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator._async_update_data",
        AsyncMock(return_value=[]),
    )
    restart = AsyncMock()
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator.async_restart",
        restart,
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

    buttons = [
        state.entity_id
        for state in hass.states.async_all("button")
        if state.entity_id.endswith("_restart")
    ]
    assert len(buttons) == 1

    await hass.services.async_call(
        "button", "press", {"entity_id": buttons[0]}, blocking=True
    )

    restart.assert_awaited_once_with()


async def test_restart_retries_a_busy_333_service(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A single busy 333 attempt must not make the button fail."""
    coordinator = HyxiDmuCoordinator(hass, HOST)
    calls = 0

    async def _flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("busy")

    monkeypatch.setattr(coordinator, "_restart_once", _flaky)
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.asyncio.sleep",
        AsyncMock(return_value=None),
    )

    await coordinator.async_restart()

    assert calls == 2


class _FakeRecorder:
    def __init__(self) -> None:
        self.statistic_ids: list[str] | None = None

    def async_clear_statistics(self, statistic_ids: list[str]) -> None:
        self.statistic_ids = statistic_ids


async def test_reset_energy_button_clears_microinverter_statistics(
    hass: HomeAssistant, monkeypatch
) -> None:
    """The reset button asks the recorder to drop the polluted statistics."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator._async_update_data",
        AsyncMock(return_value=[]),
    )
    recorder = _FakeRecorder()
    monkeypatch.setitem(hass.data, DATA_INSTANCE, recorder)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Hyxi DMU ({HOST})",
        unique_id=HOST,
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    buttons = [
        state.entity_id
        for state in hass.states.async_all("button")
        if "reset_energy" in state.entity_id
    ]
    assert len(buttons) == 1

    await hass.services.async_call(
        "button", "press", {"entity_id": buttons[0]}, blocking=True
    )

    assert recorder.statistic_ids == ["sensor.hyxi_dmu_microinverter_energy"]
