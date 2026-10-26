"""HYX-DMU local protocol.

Frames on TCP 333 look like::

    "PB" | payload length (BE16) | CRC16/MODBUS(payload) (BE16) | payload

The payload is protobuf. Requests carry ``1=session, 2=channel, 3=opcode,
6=unix time, 11=inner request``. Replies use ``3=0x4000 | request``.

Everything here is pure Python so it can be tested without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

FRAME_MAGIC = b"PB"

OP_INFO = 0xC002
OP_LIST = 0xC003
OP_RESTART = 0xC005
OP_UPLOAD = 0x0003
OP_TIME_SYNC = 0xC020
OP_LOGIN = 0xC022

REPLY_MASK = 0x4000

CHANNEL_DMU = 1
CHANNEL_PV = 0x201

#: The DMU echoes this field but does not validate it.
SESSION_ID = 1

#: MPPT energy counters are in units of 10 Wh.
ENERGY_UNIT_KWH = 0.01

#: Wire power/voltage are tenths.
DECI = 10.0


class ProtocolError(Exception):
    """Raised when a frame cannot be decoded."""


@dataclass(frozen=True)
class MpptReading:
    """One MPPT input of a microinverter."""

    index: int
    power_w: float
    energy_kwh: float
    voltage_v: float | None = None


@dataclass(frozen=True)
class InverterReading:
    """One microinverter as reported by the DMU's device list."""

    sn: int
    mppts: tuple[MpptReading, ...] = field(default_factory=tuple)
    rated_w: int | None = None

    @property
    def power_w(self) -> float:
        """AC-side estimate: the sum of the MPPT DC powers."""
        return round(sum(mppt.power_w for mppt in self.mppts), 1)

    @property
    def energy_kwh(self) -> float:
        """Lifetime energy, summed over the MPPTs."""
        return round(sum(mppt.energy_kwh for mppt in self.mppts), 3)

    @property
    def short_sn(self) -> str:
        """Last six digits, enough to tell two units apart."""
        return str(self.sn)[-6:]


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ProtocolError("truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise ProtocolError("varint too long")


def _field(number: int, wire: int, payload: bytes | int) -> bytes:
    key = _encode_varint((number << 3) | wire)
    if wire == 0:
        return key + _encode_varint(int(payload))
    assert isinstance(payload, bytes)
    return key + _encode_varint(len(payload)) + payload


def crc16_modbus(data: bytes) -> int:
    """CRC16/MODBUS, the checksum the DMU uses."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_request(
    opcode: int,
    channel: int = CHANNEL_DMU,
    *,
    now: datetime | None = None,
    session: int = SESSION_ID,
) -> bytes:
    """Build one request frame. Reads do not need a login or the real SN."""
    moment = now or datetime.now()
    inner = (
        _field(1, 2, moment.strftime("%Y-%m-%d %H:%M").encode())
        + _field(2, 0, 0)
        + _field(3, 0, 0)
    )
    payload = (
        _field(1, 0, session)
        + _field(2, 0, channel)
        + _field(3, 0, opcode)
        + _field(6, 0, int(moment.timestamp()))
        + _field(11, 2, inner)
    )
    return (
        FRAME_MAGIC
        + len(payload).to_bytes(2, "big")
        + crc16_modbus(payload).to_bytes(2, "big")
        + payload
    )


def build_login(
    password: str,
    *,
    now: datetime | None = None,
    session: int = SESSION_ID,
) -> bytes:
    """Build the login handshake channel 0x201 expects.

    The DMU answers regardless of the password value, but the handshake must be
    sent on every new connection before the device list is readable.
    """
    moment = now or datetime.now()
    inner = _field(
        1,
        2,
        _field(
            11,
            2,
            _field(1, 0, 1)
            + _field(2, 0, 2)
            + _field(3, 2, password.encode()),
        ),
    )
    payload = (
        _field(1, 0, session)
        + _field(2, 0, CHANNEL_DMU)
        + _field(3, 0, OP_LOGIN)
        + _field(6, 0, int(moment.timestamp()))
        + _field(11, 2, inner)
    )
    return (
        FRAME_MAGIC
        + len(payload).to_bytes(2, "big")
        + crc16_modbus(payload).to_bytes(2, "big")
        + payload
    )


def build_restart(
    *,
    now: datetime | None = None,
    session: int = SESSION_ID,
) -> bytes:
    """Build the ``0xC005`` reboot request.

    The inner body is the captured ``restart`` command: type 1, mode 12.
    """
    moment = now or datetime.now()
    inner = (
        _field(1, 0, 1)
        + _field(2, 0, 12)
        + _field(3, 0, 1)
        + _field(4, 0, 0)
    )
    payload = (
        _field(1, 0, session)
        + _field(2, 0, CHANNEL_DMU)
        + _field(3, 0, OP_RESTART)
        + _field(6, 0, int(moment.timestamp()))
        + _field(11, 2, inner)
    )
    return (
        FRAME_MAGIC
        + len(payload).to_bytes(2, "big")
        + crc16_modbus(payload).to_bytes(2, "big")
        + payload
    )


def extract_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split complete frames off ``buffer``, returning their payloads."""
    frames: list[bytes] = []
    while True:
        start = buffer.find(FRAME_MAGIC)
        if start < 0:
            return frames, b""
        buffer = buffer[start:]
        if len(buffer) < 6:
            return frames, buffer

        length = int.from_bytes(buffer[2:4], "big")
        if len(buffer) < 6 + length:
            return frames, buffer

        payload = buffer[6:6 + length]
        checksum = int.from_bytes(buffer[4:6], "big")
        if crc16_modbus(payload) != checksum:
            raise ProtocolError("bad CRC")
        frames.append(payload)
        buffer = buffer[6 + length:]


def parse_message(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode protobuf into ``(field, wire_type, value)`` tuples."""
    fields: list[tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        key, offset = _decode_varint(data, offset)
        number = key >> 3
        wire = key & 0x07
        if wire == 0:
            value, offset = _decode_varint(data, offset)
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
        elif wire == 2:
            size, offset = _decode_varint(data, offset)
            value = data[offset:offset + size]
            offset += size
        elif wire == 5:
            value = data[offset:offset + 4]
            offset += 4
        else:
            raise ProtocolError(f"unsupported wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _varint_field(fields: list[tuple[int, int, Any]], number: int):
    for field_number, wire, value in fields:
        if field_number == number and wire == 0:
            return value
    return None


def _bytes_field(fields: list[tuple[int, int, Any]], number: int):
    for field_number, wire, value in fields:
        if field_number == number and wire == 2:
            return value
    return None


def opcode_of(payload: bytes) -> int | None:
    """Return the reply/request opcode carried in field 3."""
    return _varint_field(parse_message(payload), 3)


def parse_upload_frame(frame: bytes) -> list[InverterReading]:
    """Decode one ``uploadDataReq`` PB frame into inverter readings.

    The cloud upload carries the same field-11 body as a local ``0x4003`` list
    reply.  Heartbeats and other upload types simply yield an empty list.
    """
    frames, _ = extract_frames(frame)
    for payload in frames:
        readings = parse_list_reply(payload)
        if readings:
            return readings
    return []


def parse_list_reply(payload: bytes) -> list[InverterReading]:
    """Decode a ``0x4003`` list reply into inverter readings.

    Layout (observed)::

        11: { 7: { 1: {1: SN, 7: rated W, ...}
                   3: {1: idx, 2: volts*10, 4: watts*10, 5: energy}
                   3: { ... } }
              7: { ... } }
    """
    body = _bytes_field(parse_message(payload), 11)
    if body is None:
        return []

    readings: list[InverterReading] = []
    for number, wire, entry_bytes in parse_message(body):
        if number != 7 or wire != 2:
            continue

        entry = parse_message(entry_bytes)
        info_bytes = _bytes_field(entry, 1)
        if info_bytes is None:
            continue

        info = parse_message(info_bytes)
        sn = _varint_field(info, 1)
        if sn is None:
            continue

        mppts: list[MpptReading] = []
        for field_number, mppt_wire, mppt_bytes in entry:
            if field_number != 3 or mppt_wire != 2:
                continue
            mppt = parse_message(mppt_bytes)
            raw_volts = _varint_field(mppt, 2)
            mppts.append(
                MpptReading(
                    index=_varint_field(mppt, 1) or 0,
                    power_w=(_varint_field(mppt, 4) or 0) / DECI,
                    energy_kwh=round(
                        (_varint_field(mppt, 5) or 0) * ENERGY_UNIT_KWH, 3
                    ),
                    voltage_v=(
                        round(raw_volts / DECI, 1)
                        if raw_volts is not None
                        else None
                    ),
                )
            )

        readings.append(
            InverterReading(
                sn=sn,
                mppts=tuple(mppts),
                rated_w=_varint_field(info, 7),
            )
        )

    return readings
