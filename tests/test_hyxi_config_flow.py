"""Reconfigure flow tests for the Hyxi DMU integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hyxi_dmu.const import CONF_HOST, CONF_PASSWORD, DOMAIN

OLD_HOST = "192.0.2.13"
NEW_HOST = "192.0.2.14"
PASSWORD = "secret"


@pytest.fixture(autouse=True)
def _enable_hyxi_custom_integration(enable_custom_integrations: None) -> None:
    """Allow loading ``custom_components`` when this file runs without conftest."""


@pytest.fixture(autouse=True)
def stub_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the entry reload away from the network."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.coordinator.HyxiDmuCoordinator._async_update_data",
        AsyncMock(return_value=[]),
    )


@pytest.fixture
def dmu_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry pointing at the old address."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Hyxi DMU ({OLD_HOST})",
        unique_id=OLD_HOST,
        data={CONF_HOST: OLD_HOST, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    return entry


async def _start_reconfigure(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )


async def test_reconfigure_moves_the_entry_to_a_new_host(
    hass: HomeAssistant, dmu_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new address is probed, stored and the entry is reloaded."""
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr("custom_components.hyxi_dmu.config_flow.async_probe", probe)

    result = await _start_reconfigure(hass, dmu_entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: NEW_HOST, CONF_PASSWORD: PASSWORD},
    )

    probe.assert_awaited_once_with(NEW_HOST, PASSWORD)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dmu_entry.data[CONF_HOST] == NEW_HOST
    assert dmu_entry.data[CONF_PASSWORD] == PASSWORD
    assert dmu_entry.unique_id == NEW_HOST
    assert dmu_entry.title == f"Hyxi DMU ({NEW_HOST})"


async def test_reconfigure_keeps_the_entry_when_the_probe_fails(
    hass: HomeAssistant, dmu_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable address must not change the stored entry."""
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.config_flow.async_probe",
        AsyncMock(side_effect=OSError),
    )

    result = await _start_reconfigure(hass, dmu_entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: NEW_HOST, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}
    assert dmu_entry.data[CONF_HOST] == OLD_HOST
    assert dmu_entry.unique_id == OLD_HOST


async def test_reconfigure_rejects_a_host_owned_by_another_entry(
    hass: HomeAssistant, dmu_entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two entries must not claim the same address."""
    other = MockConfigEntry(
        domain=DOMAIN,
        title=f"Hyxi DMU ({NEW_HOST})",
        unique_id=NEW_HOST,
        data={CONF_HOST: NEW_HOST, CONF_PASSWORD: PASSWORD},
    )
    other.add_to_hass(hass)
    monkeypatch.setattr(
        "custom_components.hyxi_dmu.config_flow.async_probe",
        AsyncMock(return_value=None),
    )

    result = await _start_reconfigure(hass, dmu_entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: NEW_HOST, CONF_PASSWORD: PASSWORD},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dmu_entry.data[CONF_HOST] == OLD_HOST
