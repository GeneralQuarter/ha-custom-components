"""Advertising payload, config flow and entity mapping tests.

The advertising payloads here are synthesised, not captured: the helpers below
reverse the exact wire transformation the integration applies, so a payload
that passes the parser is structurally identical to a real broadcast. That
keeps the tests deterministic while still exercising the CRC, RC4 and XOR
layers.
"""

from __future__ import annotations

import base64
from struct import pack

import pytest
from homeassistant.components.bluetooth import BluetoothServiceInfo
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResultType

from custom_components.jackery_ble.config_flow import JackeryBleConfigFlow
from custom_components.jackery_ble.const import (
    CONF_DEVICE_DATA,
    CONF_ENCRYPTION_KEY,
    DOMAIN,
    SERVICE_UUID,
)
from custom_components.jackery_ble.jackery_crypto import SALT_RC4, crc16, rc4
from custom_components.jackery_ble.jackery_device_data import (
    JackeryDeviceCategory,
    JackeryDeviceData,
)
from tests.conftest import CT_ADDRESS, CT_SN, HOME_ADDRESS, HOME_SN

SOURCE = "test"


def _encode_service_data(decoded: bytes, *, sn: str, category: JackeryDeviceCategory):
    """Turn decoded device fields into the broadcast service payload."""
    marker = 0xA5
    obfuscated = bytes(byte ^ marker for byte in decoded) + bytes([marker])
    payload = obfuscated + crc16(obfuscated)
    if category is JackeryDeviceCategory.JACKERY_DEVICE:
        payload = rc4(payload, (sn[:3] + sn[-5:] + SALT_RC4).encode())
    return payload


def vault_advertisement(*, with_service_data: bool = True) -> BluetoothServiceInfo:
    """Build a SolarVault advertisement."""
    decoded = pack(">h6sbh", 3002, bytes.fromhex(HOME_ADDRESS.replace(":", "")), 1, 0)
    service_data = {}
    if with_service_data:
        service_data[SERVICE_UUID] = _encode_service_data(
            decoded, sn=HOME_SN, category=JackeryDeviceCategory.JACKERY_DEVICE
        )
    return BluetoothServiceInfo(
        name=None,
        address=HOME_ADDRESS,
        rssi=-90,
        manufacturer_data={0x4802: HOME_SN[1:].encode()},
        service_data=service_data,
        service_uuids=[SERVICE_UUID],
        source=SOURCE,
    )


def ct_advertisement(
    *, with_service_data: bool = True, model_code: int = 4002
) -> BluetoothServiceInfo:
    """Build a Smart Meter advertisement."""
    decoded = pack(">h6sh", model_code, bytes.fromhex(CT_ADDRESS.replace(":", "")), 0)
    service_data = {}
    if with_service_data:
        service_data[SERVICE_UUID] = _encode_service_data(
            decoded, sn=CT_SN, category=JackeryDeviceCategory.JACKERY_ACCESSORY
        )
    return BluetoothServiceInfo(
        name=None,
        address=CT_ADDRESS,
        rssi=-77,
        manufacturer_data={0x4809: CT_SN[1:].encode()},
        service_data=service_data,
        service_uuids=[SERVICE_UUID],
        source=SOURCE,
    )


def test_vault_advertisement_is_fully_decoded() -> None:
    """Manufacturer data carries the serial, service data the model and GUID."""
    device = JackeryDeviceData()

    assert device.supported(vault_advertisement())
    assert device.sn == HOME_SN
    assert device.category is JackeryDeviceCategory.JACKERY_DEVICE
    assert device.model_code == 3002
    assert device.guid == bytes.fromhex(HOME_ADDRESS.replace(":", ""))
    assert not device.is_missing_data()


def test_ct_advertisement_is_fully_decoded() -> None:
    """Accessories skip the RC4 layer but keep CRC and XOR."""
    device = JackeryDeviceData()

    assert device.supported(ct_advertisement())
    assert device.sn == CT_SN
    assert device.category is JackeryDeviceCategory.JACKERY_ACCESSORY
    assert device.model_code == 4002
    assert device.guid == bytes.fromhex(CT_ADDRESS.replace(":", ""))
    assert not device.is_missing_data()


def test_advertisement_without_service_data_is_incomplete() -> None:
    """A serial alone is enough to list a device, not to drive it."""
    device = JackeryDeviceData()

    assert device.supported(ct_advertisement(with_service_data=False))
    assert device.is_missing_data()


def test_foreign_company_id_is_not_a_jackery_device() -> None:
    """Other manufacturers' advertisements must be ignored."""
    device = JackeryDeviceData()

    assert not device.supported(
        BluetoothServiceInfo(
            name="Other",
            address="AA:BB:CC:DD:EE:FF",
            rssi=-60,
            manufacturer_data={0x0499: b"not-jackery"},
            service_data={},
            service_uuids=[],
            source=SOURCE,
        )
    )


async def test_config_flow_without_devices_aborts(hass, monkeypatch) -> None:
    """Nothing discovered is a clean abort, not an error."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_config_flow_lists_a_discovered_device(hass, monkeypatch) -> None:
    """A discovery shows the address picker."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement()],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_config_flow_asks_for_the_key_once_advertisement_is_complete(
    hass, monkeypatch
) -> None:
    """A full advertisement goes straight to the key prompt."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement()],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "encryption_key"


async def test_config_flow_stores_the_key_as_hex(hass, monkeypatch) -> None:
    """The key is the only secret and must round-trip into the entry data."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement()],
    )
    raw_key = bytes(range(24))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENCRYPTION_KEY: base64.b64encode(raw_key).decode()},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Smart Meter 1P ({CT_SN})"
    device_data = result["data"][CONF_DEVICE_DATA]
    assert device_data["encryption_key"] == raw_key.hex()
    assert device_data["sn"] == CT_SN
    assert device_data["model_code"] == 4002


async def test_config_flow_aborts_when_no_full_advertisement_arrives(
    hass, monkeypatch
) -> None:
    """A serial-only advertisement would configure an unusable entry."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement(with_service_data=False)],
    )

    async def _never_complete(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_process_advertisements",
        _never_complete,
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


def test_config_flow_exposes_the_bluetooth_step() -> None:
    """manifest.json advertises bluetooth discovery, so the step must exist."""
    assert JackeryBleConfigFlow.VERSION == 1
    assert hasattr(JackeryBleConfigFlow, "async_step_bluetooth")


@pytest.mark.parametrize(
    ("advertisement", "expected_model"),
    [(ct_advertisement, 4002), (vault_advertisement, 3002)],
)
def test_advertisement_model_codes(advertisement, expected_model: int) -> None:
    """Both devices are recognisable from a single advertisement."""
    device = JackeryDeviceData()
    device.supported(advertisement())
    assert device.model_code == expected_model


async def test_options_flow_offers_the_supervisor_defaults(
    hass, home_config_entry
) -> None:
    """The supervisor is opt-in, so the form must default to off."""
    from custom_components.jackery_ble.const import (
        CONF_RESERVE_PERCENT,
        CONF_SUPERVISOR_ENABLED,
    )

    home_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(
        home_config_entry.entry_id
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SUPERVISOR_ENABLED: False,
        CONF_RESERVE_PERCENT: 2,
    }


async def test_options_flow_stores_the_supervisor_settings(
    hass, home_config_entry
) -> None:
    """Saved options replace the defaults on the next open."""
    from custom_components.jackery_ble.const import (
        CONF_RESERVE_PERCENT,
        CONF_SUPERVISOR_ENABLED,
    )

    home_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(
        home_config_entry.entry_id
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SUPERVISOR_ENABLED: True,
            CONF_RESERVE_PERCENT: 3,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert home_config_entry.options[CONF_SUPERVISOR_ENABLED] is True
    assert home_config_entry.options[CONF_RESERVE_PERCENT] == 3


async def test_config_flow_rejects_a_key_that_is_not_base64(
    hass, monkeypatch
) -> None:
    """A pasted key that cannot be decoded must not create an entry."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement()],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENCRYPTION_KEY: "definitely not base64 !!"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "encryption_key"
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_key"}


async def test_config_flow_rejects_a_key_of_the_wrong_length(
    hass, monkeypatch
) -> None:
    """Base64 that decodes to 8 bytes cannot be an AES key."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement()],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENCRYPTION_KEY: base64.b64encode(b"12345678").decode()},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENCRYPTION_KEY: "invalid_key"}


async def test_config_flow_accepts_unpadded_base64(hass, monkeypatch) -> None:
    """Keys pasted without trailing '=' must still work."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement()],
    )
    raw_key = bytes(range(16))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENCRYPTION_KEY: base64.b64encode(raw_key).decode().rstrip("=")},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_DATA]["encryption_key"] == raw_key.hex()


async def test_config_flow_aborts_on_an_unknown_model(hass, monkeypatch) -> None:
    """A Jackery serial is not enough: the model must be supported."""
    monkeypatch.setattr(
        "custom_components.jackery_ble.config_flow.async_discovered_service_info",
        lambda _hass, _connectable: [ct_advertisement(model_code=9999)],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: CT_ADDRESS}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"
