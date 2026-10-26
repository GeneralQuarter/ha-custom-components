"""Jackery Crypto Helpers."""

# Fixed vendor salt used to derive the CT advertisement RC4 key from its SN.
# This is protocol material, not a per-device BLE credential.
SALT_RC4 = "LYx*G!6u9#"


def crc16(data: bytes) -> bytes:
    """CRC16 symetric encryption."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, byteorder="little")


def crc16_checksum(data: bytes, length: int = 2):
    """CRC16 check on data (taking last length bytes as checksum). Returns data without the checksum."""
    returned_data = data[:-length]
    checksum = data[-length:]

    return returned_data if crc16(returned_data) == checksum else None


def rc4(data: bytes, key: bytes) -> bytes:
    """RC4 symetric encryption."""
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 255
        S[i], S[j] = S[j], S[i]
    i = j = 0
    result = bytearray(len(data))
    for k in range(len(data)):
        i = (i + 1) & 255
        j = (j + S[i]) & 255
        S[i], S[j] = S[j], S[i]
        result[k] = data[k] ^ S[(S[i] + S[j]) & 255]
    return bytes(result)


def xor_with_last_byte(data: bytes) -> bytes:
    """XOR data with last byte."""
    _data = data[:-1]
    xor_byte = data[-1]
    result = bytearray()
    for b in _data:
        result.append(b ^ xor_byte)
    return bytes(result)
