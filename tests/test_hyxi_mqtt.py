"""Tests for the HYXi DMU local MQTT ack builder."""

from __future__ import annotations

import time

from custom_components.hyxi_mqtt import topic_matches
from custom_components.hyxi_mqtt.protocol import (
    build_ack,
    crc16_modbus,
    field,
    parse_message,
)

DEVICE_ID = 11111111111111111


def _request(opcode: int, *, seq: int = 7, mutate=None) -> bytes:
    body = (
        field(1, 0, DEVICE_ID)
        + field(2, 0, 1)
        + field(3, 0, opcode)
        + field(5, 0, seq)
        + field(6, 0, 1792969000)
        + field(11, 2, b"\x0a\x03abc")
    )
    if mutate:
        body = mutate(body)
    return (
        b"PB"
        + len(body).to_bytes(2, "big")
        + crc16_modbus(body).to_bytes(2, "big")
        + body
    )


def test_ack_mirrors_opcode_and_sequence() -> None:
    """The cloud ack is the request opcode with the reply bit set."""
    opcode, frame = build_ack(_request(0x2), now=1792969001.0)

    assert opcode == 0x2
    body = frame[6:]
    assert body and frame[:2] == b"PB"
    fields = {num: value for num, wire, value in parse_message(body) if wire == 0}
    assert fields[1] == DEVICE_ID
    assert fields[2] == 1
    assert fields[3] == 0x8002
    assert fields[5] == 7
    assert fields[6] == 1792969001


def test_ack_carries_a_time_string() -> None:
    """The DMU gets its clock from the ack body."""
    _, frame = build_ack(_request(0x3), now=1792969001.0)
    text = next(
        value
        for num, wire, value in parse_message(frame[6:])
        if num == 11 and wire == 2
    )
    inner = {num: value for num, wire, value in parse_message(text)}
    stamp = inner[1].decode()
    assert stamp.startswith(time.strftime("%Y-%m-%d", time.localtime(1792969001.0)))


def test_register_ack_uses_a_date_only() -> None:
    """Opcode 1 (register) is acked with a date, matching the cloud."""
    _, frame = build_ack(_request(0x1), now=1792969001.0)
    text = next(
        value
        for num, wire, value in parse_message(frame[6:])
        if num == 11 and wire == 2
    )
    stamp = {num: value for num, wire, value in parse_message(text)}[1].decode()
    assert len(stamp) == 10


def test_bad_crc_is_ignored() -> None:
    """A corrupt frame must not be acked."""
    frame = bytearray(_request(0x2))
    frame[-1] ^= 0xFF
    assert build_ack(bytes(frame)) is None


def test_reply_frames_are_ignored() -> None:
    """Frames that already carry the reply bit are not re-acked."""
    assert build_ack(_request(0x8002)) is None



def test_topic_filter_exact_match() -> None:
    """Exact subscriptions match only themselves."""
    assert topic_matches("device/commonSvrRsp/1", "device/commonSvrRsp/1")
    assert not topic_matches("device/commonSvrRsp/1", "device/commonSvrRsp/2")


def test_topic_filter_plus() -> None:
    """A single-level wildcard matches one level."""
    assert topic_matches("device/+/1", "device/registerReq/1")
    assert not topic_matches("device/+/1", "device/registerReq/2")


def test_topic_filter_hash() -> None:
    """The multi-level wildcard matches the parent and everything below."""
    assert topic_matches("device/#", "device/registerReq/1")
    assert topic_matches("device/#", "device")


def test_topic_filter_length() -> None:
    """A filter must not match a longer topic."""
    assert not topic_matches("a/b", "a/b/c")


