"""Shared fixtures for the Jackery BLE test suite."""

from __future__ import annotations

import logging
import socket

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jackery_ble.const import CONF_DEVICE_DATA, DOMAIN
from custom_components.jackery_ble.jackery_device_data import (
    JackeryDeviceCategory,
    JackeryDeviceData,
)

pytest_plugins = ["pytest_homeassistant_custom_component"]

# ``pytest-socket`` blocks ``socket.socket`` for every test, but on Windows
# ``asyncio`` builds its event-loop self-pipe from ``socket.socketpair()``,
# which resolves the (now guarded) ``socket.socket`` name internally. Without
# this shim every test fails during event-loop creation, before it even runs.
# Only the socketpair path is unguarded; everything else stays blocked.
_TRUE_SOCKETPAIR = socket.socketpair


def _socketpair_bypassing_guard(*args, **kwargs):
    """Create an event-loop self-pipe while keeping ``pytest-socket`` active."""
    from pytest_socket import _true_socket

    guarded_socket = socket.socket
    socket.socket = _true_socket
    try:
        return _TRUE_SOCKETPAIR(*args, **kwargs)
    finally:
        socket.socket = guarded_socket


socket.socketpair = _socketpair_bypassing_guard

HOME_SN = "HR2C000000001"
HOME_ADDRESS = "AA:BB:CC:DD:EE:01"
CT_SN = "HW2C000000002"
CT_ADDRESS = "AA:BB:CC:DD:EE:02"

# Anything works as a key in tests; keep the length the devices use.
HOME_KEY = bytes(range(24))
CT_KEY = bytes(range(16, 32))

LOGGER = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load integrations from ``custom_components`` for every test."""


@pytest.fixture(autouse=True)
def stub_adapter_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the local adapter history scan when the bluetooth component starts.

    ``pytest-homeassistant-custom-component`` mocks the Linux adapter list but
    not the D-Bus history lookup behind it, so component setup fails on any
    host without BlueZ. The integration only talks to a remote proxy and never
    reads local adapter history.
    """
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.manager.async_load_history_from_system",
        lambda *_args, **_kwargs: ({}, {}),
    )


def build_device_data(
    *,
    sn: str,
    address: str,
    model_code: int,
    category: JackeryDeviceCategory,
    key: bytes,
) -> JackeryDeviceData:
    """Build the device payload that is persisted in a config entry."""
    return JackeryDeviceData(
        sn=sn,
        guid=bytes.fromhex(address.replace(":", "")),
        model_code=model_code,
        category=category,
        encryption_key=key,
    )


@pytest.fixture
def home_device_data() -> JackeryDeviceData:
    """Device data for the SolarVault."""
    return build_device_data(
        sn=HOME_SN,
        address=HOME_ADDRESS,
        model_code=3002,
        category=JackeryDeviceCategory.JACKERY_DEVICE,
        key=HOME_KEY,
    )


@pytest.fixture
def ct_device_data() -> JackeryDeviceData:
    """Device data for the Smart Meter 1P."""
    return build_device_data(
        sn=CT_SN,
        address=CT_ADDRESS,
        model_code=4002,
        category=JackeryDeviceCategory.JACKERY_ACCESSORY,
        key=CT_KEY,
    )


def build_config_entry(
    device_data: JackeryDeviceData, address: str
) -> MockConfigEntry:
    """Build the config entry the integration stores for a device."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=device_data.name,
        unique_id=address,
        data={CONF_DEVICE_DATA: device_data.to_config_entry_device_data()},
    )


@pytest.fixture
def home_config_entry(home_device_data: JackeryDeviceData) -> MockConfigEntry:
    """Config entry for the SolarVault."""
    return build_config_entry(home_device_data, HOME_ADDRESS)


@pytest.fixture
def ct_config_entry(ct_device_data: JackeryDeviceData) -> MockConfigEntry:
    """Config entry for the Smart Meter 1P."""
    return build_config_entry(ct_device_data, CT_ADDRESS)
