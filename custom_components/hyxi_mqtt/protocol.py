"""PB framing helpers for the HYXi DMU cloud protocol."""

from __future__ import annotations

import time

OP_REGISTER = 0x1
OP_UPLOAD = 0x3
REPLY_MASK = 0x8000


def encode_varint(value: int) -> bytes:
    """Encode a protobuf varint."""
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out += bytes([byte | 0x80])
        else:
            return out + bytes([byte])


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a protobuf varint, returning (value, new offset)."""
    value = 0
    mult = 1
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) * mult
        if not byte & 0x80:
            return value, offset
        mult *= 128
    raise ValueError("truncated varint")


def field(number: int, wire: int, value: int | bytes) -> bytes:
    """Encode one protobuf field."""
    out = encode_varint((number << 3) | wire)
    if wire == 0:
        return out + encode_varint(int(value))
    if wire == 2:
        assert isinstance(value, bytes)
        return out + encode_varint(len(value)) + value
    raise ValueError(f"unsupported wire type {wire}")


def parse_message(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Parse a protobuf message into (field, wire, value) tuples."""
    out: list[tuple[int, int, int | bytes]] = []
    offset = 0
    while offset < len(data):
        key, offset = decode_varint(data, offset)
        number, wire = key >> 3, key & 0x07
        if wire == 0:
            value, offset = decode_varint(data, offset)
        elif wire == 2:
            size, offset = decode_varint(data, offset)
            value = data[offset : offset + size]
            offset += size
        elif wire == 1:
            value = data[offset : offset + 8]
            offset += 8
        elif wire == 5:
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((number, wire, value))
    return out


def crc16_modbus(data: bytes) -> int:
    """CRC16/MODBUS, the checksum the DMU uses."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def unwrap(request: bytes) -> bytes | None:
    """Validate a PB frame and return its protobuf payload."""
    if len(request) < 6 or request[:2] != b"PB":
        return None
    length = int.from_bytes(request[2:4], "big")
    crc = int.from_bytes(request[4:6], "big")
    body = request[6 : 6 + length]
    if len(body) != length or crc16_modbus(body) != crc:
        return None
    return body


def build_ack(request: bytes, now: float | None = None) -> tuple[int, bytes] | None:
    """Build the cloud-style ack frame for one DMU request frame.

    Returns (opcode, framed payload) or None when the frame is not a
    request that needs an ack.
    """
    body = unwrap(request)
    if body is None:
        return None
    try:
        fields = {num: value for num, wire, value in parse_message(body) if wire == 0}
    except ValueError:
        return None
    opcode = fields.get(3)
    if opcode is None or opcode & REPLY_MASK:
        return None

    moment = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))
    text = time.strftime("%Y-%m-%d", time.localtime(moment)) if opcode == 1 else stamp
    payload = (
        field(1, 0, fields.get(1, 0))
        + field(2, 0, fields.get(2, 0))
        + field(3, 0, opcode | REPLY_MASK)
        + field(5, 0, fields.get(5, 0))
        + field(6, 0, int(moment))
        + field(11, 2, field(1, 2, text.encode()))
    )
    frame = (
        b"PB"
        + len(payload).to_bytes(2, "big")
        + crc16_modbus(payload).to_bytes(2, "big")
        + payload
    )
    return opcode, frame
