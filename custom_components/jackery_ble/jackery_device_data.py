"""Jackery Device Data class.

This module is intentionally free of Home Assistant imports at runtime so it
can be reused by the standalone probe in ``tools/``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import Enum, IntEnum
from struct import unpack_from
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import SERVICE_UUID
from .jackery_crypto import SALT_RC4, crc16, crc16_checksum, rc4, xor_with_last_byte

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfo


class JackeryDeviceModelType(Enum):
    """Jackery Device Model Type."""

    PORTABLE = 0
    BOX = 1
    HOME = 2
    ACCESSORY = 3


@dataclass
class JackeryDeviceModelData:
    """Device Model Data not inferable."""

    name: str
    model_type: JackeryDeviceModelType


MODEL_DATA: dict[int, JackeryDeviceModelData] = {
    3002: JackeryDeviceModelData("SolarVault 3 Pro Max", JackeryDeviceModelType.HOME),
    4002: JackeryDeviceModelData("Smart Meter 1P", JackeryDeviceModelType.ACCESSORY),
}


class JackeryDeviceCategory(IntEnum):
    """Jackery Device Category."""

    JACKERY_DEVICE = 2
    JACKERY_ACCESSORY = 9


@dataclass
class JackeryDeviceData:
    """Jackery Device Data."""

    sn: str = ""
    guid: bytes = b""
    model_code: int = 0
    category: JackeryDeviceCategory = JackeryDeviceCategory.JACKERY_DEVICE
    electricity: int = 0
    reset_mark: int = 0
    encryption_key: bytes = b""

    def to_config_entry_device_data(self) -> dict[str, Any]:
        """Transform device data to config entry device data."""
        return {
            "sn": self.sn,
            "guid": self.guid.hex(),
            "model_code": self.model_code,
            "category": int(self.category),
            "electricity": self.electricity,
            "reset_mark": self.reset_mark,
            "encryption_key": self.encryption_key.hex(),
        }

    @classmethod
    def from_config_entry_device_data(cls, data: dict[str, Any]) -> JackeryDeviceData:
        """Create device data from config entry device data."""
        return cls(
            sn=data.get("sn", ""),
            guid=bytes.fromhex(data.get("guid", "")),
            model_code=int(data.get("model_code", 0)),
            category=JackeryDeviceCategory(
                int(data.get("category", JackeryDeviceCategory.JACKERY_DEVICE))
            ),
            electricity=int(data.get("electricity", 0)),
            reset_mark=int(data.get("reset_mark", 0)),
            encryption_key=bytes.fromhex(data.get("encryption_key", "")),
        )

    @property
    def is_accessory(self) -> bool:
        """Is the jackery device an accessory."""
        return self.model_type is JackeryDeviceModelType.ACCESSORY

    def _has_serial_number(self) -> bool:
        return self.sn != ""

    def _has_guid(self) -> bool:
        return len(self.guid) > 0

    def update(self, service_info: BluetoothServiceInfo) -> None:
        """Update device with service info."""
        if service_info.manufacturer_data and not self._has_serial_number():
            self._parse_manufacturer_data(service_info.manufacturer_data)

        if not service_info.service_data or not self._has_serial_number():
            return

        for uuid, value in service_info.service_data.items():
            if uuid.lower() == SERVICE_UUID.lower():
                self._parse_service_data(value)
                return

    def _parse_manufacturer_data(self, mfr_data: dict[int, bytes]) -> None:
        """Parse the Jackery manufacturer data.

        The 16-bit company id encodes both the first serial character (high
        byte, always ``0x48`` = ``"H"``) and the device category (low byte:
        ``2`` = device, ``9`` = accessory).
        """
        for raw_mfr_id, mfr_bytes in mfr_data.items():
            high = (raw_mfr_id >> 8) & 0xFF
            low = raw_mfr_id & 0xFF
            if high != 0x48 or low not in (
                JackeryDeviceCategory.JACKERY_DEVICE,
                JackeryDeviceCategory.JACKERY_ACCESSORY,
            ):
                continue

            try:
                serial = bytes([high]).decode("utf-8") + mfr_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue

            serial = serial.rstrip("\x00 ").strip()
            if len(serial) < 4:
                continue

            self.category = JackeryDeviceCategory(low)
            self.sn = serial
            return

    def _parse_service_data(self, service_data: bytes) -> None:
        """Parse the encrypted/obfuscated service data."""
        if not service_data:
            return

        decrypted_service_data = service_data

        if self.category is JackeryDeviceCategory.JACKERY_DEVICE:
            if len(self.sn) < 8:
                return
            rc4_key = self.sn[:3] + self.sn[-5:] + SALT_RC4
            decrypted_service_data = rc4(service_data, rc4_key.encode("utf-8"))

        data = crc16_checksum(decrypted_service_data)
        if data is None or len(data) < 2:
            return

        decoded = xor_with_last_byte(data)

        if self.category is JackeryDeviceCategory.JACKERY_DEVICE:
            if len(decoded) < 11:
                return
            self.model_code, self.guid, self.electricity, self.reset_mark = unpack_from(
                ">h6sbh", decoded
            )
        else:
            if len(decoded) < 10:
                return
            self.model_code, self.guid, self.reset_mark = unpack_from(">h6sh", decoded)

    @property
    def model_data(self) -> JackeryDeviceModelData | None:
        """Get the model data for the device."""
        return MODEL_DATA.get(self.model_code)

    @property
    def name(self) -> str:
        """Get the friendly model name for the device."""
        return self.model_data.name + f" ({self.sn})" if self.model_data else self.sn

    @property
    def model_type(self) -> JackeryDeviceModelType | None:
        """Get the model type of the device."""
        return self.model_data.model_type if self.model_data else None

    def supported(self, service_info: BluetoothServiceInfo) -> bool:
        """Determines if the device is a jackery supported device."""
        self.update(service_info)
        return self._has_serial_number()

    def is_missing_data(self) -> bool:
        """Are we missing data (might be missing some service_data)."""
        return not self._has_serial_number() or not self._has_guid()

    def set_encryption_key(self, key: bytes) -> None:
        """Set encryption key for this device."""
        self.encryption_key = key

    @staticmethod
    def _pad(data: bytes) -> bytes:
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        return padder.update(data) + padder.finalize()

    @staticmethod
    def _unpad(data: bytes) -> bytes:
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        return unpadder.update(data) + unpadder.finalize()

    def decrypt(self, encrypted_data: bytes) -> bytes | None:
        """Decrypt data with the device encryption key.

        Returns the full plaintext frame (header + body + trailer). The
        trailer is *not* validated here: live SolarVault notifications carry
        a 4-byte opaque trailer, while outbound frames carry a 2-byte
        security value plus a Modbus CRC16. Frame parsing uses the header
        length instead of assuming a checksum layout.

        Returns ``None`` when the payload is malformed, the key is invalid or
        the PKCS7 padding is wrong.
        """
        if len(self.encryption_key) not in (16, 24, 32):
            return None
        if len(encrypted_data) < 32 or (len(encrypted_data) - 16) % 16 != 0:
            return None

        try:
            decryptor = Cipher(
                algorithms.AES(self.encryption_key),
                modes.CBC(encrypted_data[:16]),
            ).decryptor()
            padded = decryptor.update(encrypted_data[16:]) + decryptor.finalize()
            return self._unpad(padded)
        except ValueError:
            return None

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt data with the device encryption key."""
        security = secrets.randbelow(0xFFFF) + 1
        data_with_suffix = data + security.to_bytes(2, "big")
        data_with_crc = data_with_suffix + crc16(data_with_suffix)
        iv = secrets.token_bytes(16)
        encryptor = Cipher(
            algorithms.AES(self.encryption_key),
            modes.CBC(iv),
        ).encryptor()
        encrypted = encryptor.update(self._pad(data_with_crc)) + encryptor.finalize()
        return iv + encrypted
