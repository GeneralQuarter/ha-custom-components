"""Protocol tests for the Hyxi DMU integration (pure Python)."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.hyxi_dmu import protocol as p


def _reply_payload(devices) -> bytes:
    """Build a synthetic 0x4003 payload: [(sn, [(volts, watts, energy), ...])]."""
    entries = b""
    for sn, mppts in devices:
        info = p._field(1, 0, sn) + p._field(7, 0, 1000)
        entry = p._field(1, 2, info)
        for index, (volts, watts, energy) in enumerate(mppts, start=1):
            mppt = (
                p._field(1, 0, index)
                + p._field(2, 0, volts)
                + p._field(4, 0, watts)
                + p._field(5, 0, energy)
            )
            entry += p._field(3, 2, mppt)
        entries += p._field(7, 2, entry)
    return p._field(11, 2, entries)


def test_request_round_trip() -> None:
    """A built request survives framing and carries the right opcode."""
    frame = p.build_request(
        p.OP_LIST, p.CHANNEL_PV, now=datetime(2026, 10, 25, 12, 0)
    )

    frames, rest = p.extract_frames(frame)

    assert rest == b""
    assert len(frames) == 1
    fields = p.parse_message(frames[0])
    assert p.opcode_of(frames[0]) == p.OP_LIST
    assert p._varint_field(fields, 2) == p.CHANNEL_PV
    inner = p._bytes_field(fields, 11)
    assert p._bytes_field(p.parse_message(inner), 1) == b"2026-10-25 12:00"


def test_login_request_carries_the_password() -> None:
    """The handshake is what unlocks channel 0x201."""
    frames, _ = p.extract_frames(p.build_login("secret"))
    top = p.parse_message(frames[0])

    assert p.opcode_of(frames[0]) == p.OP_LOGIN
    level1 = p.parse_message(p._bytes_field(top, 11))
    level2 = p.parse_message(p._bytes_field(level1, 1))
    level3 = p.parse_message(p._bytes_field(level2, 11))

    assert p._varint_field(level3, 1) == 1
    assert p._varint_field(level3, 2) == 2
    assert p._bytes_field(level3, 3) == b"secret"


def test_restart_request_carries_the_c005_command() -> None:
    """The restart frame carries type 1 / mode 12 in its inner body."""
    frame = p.build_restart(now=datetime(2026, 10, 26, 14, 0))
    frames, rest = p.extract_frames(frame)

    assert rest == b""
    assert len(frames) == 1
    assert p.opcode_of(frames[0]) == p.OP_RESTART
    fields = p.parse_message(frames[0])
    inner = p.parse_message(p._bytes_field(fields, 11))
    assert p._varint_field(inner, 1) == 1
    assert p._varint_field(inner, 2) == 12
    assert p._varint_field(inner, 3) == 1
    assert p._varint_field(inner, 4) == 0


def test_crc_rejects_corruption() -> None:
    """A flipped byte must not be accepted as a frame."""
    frame = bytearray(p.build_request(p.OP_LIST))
    frame[8] ^= 0xFF

    with pytest.raises(p.ProtocolError):
        p.extract_frames(bytes(frame))


def test_extract_frames_tolerates_partial_and_multiple() -> None:
    """Frames split across reads and two back-to-back frames both parse."""
    one = p.build_request(p.OP_INFO)
    two = p.build_request(p.OP_LIST)

    frames, rest = p.extract_frames(one + two)

    assert len(frames) == 2
    assert rest == b""
    assert p.opcode_of(frames[0]) == p.OP_INFO
    assert p.opcode_of(frames[1]) == p.OP_LIST

    frames, rest = p.extract_frames(one + two[:4])
    assert len(frames) == 1
    assert rest == two[:4]


def test_parse_list_reply_scales_the_values() -> None:
    """Power is tenths of a watt, voltage tenths of a volt, energy 10 Wh."""
    payload = _reply_payload(
        [
            (13546061932007477, [(300, 4077, 61444), (301, 3946, 60996)]),
            (13546061723336743, [(305, 4243, 58597), (303, 4101, 56890)]),
        ]
    )

    readings = p.parse_list_reply(payload)

    assert len(readings) == 2
    first = readings[0]
    assert first.sn == 13546061932007477
    assert first.short_sn == "007477"
    assert first.rated_w == 1000
    assert first.power_w == 802.3
    assert first.energy_kwh == 1224.4
    assert first.mppts[0].voltage_v == 30.0


def test_missing_power_field_means_zero() -> None:
    """The DMU omits the field when an MPPT is producing nothing."""
    info = p._field(1, 0, 42) + p._field(7, 0, 800)
    entry = p._field(1, 2, info)
    for index, energy in ((1, 100), (2, 90)):
        entry += p._field(
            3, 2, p._field(1, 0, index) + p._field(2, 0, 310) + p._field(5, 0, energy)
        )
    payload = p._field(11, 2, p._field(7, 2, entry))

    [reading] = p.parse_list_reply(payload)

    assert reading.power_w == 0
    assert reading.energy_kwh == 1.9
    assert reading.mppts[0].voltage_v == 31.0


def test_empty_list() -> None:
    """An empty device list parses to an empty result, not an error."""
    assert p.parse_list_reply(p._field(11, 2, b"")) == []


def _upload_frame(body: bytes, opcode: int = p.OP_UPLOAD) -> bytes:
    payload = (
        p._field(1, 0, 11111111111111111)
        + p._field(2, 0, 1)
        + p._field(3, 0, opcode)
        + p._field(6, 0, 1793018100)
        + p._field(11, 2, body)
    )
    return (
        b"PB"
        + len(payload).to_bytes(2, "big")
        + p.crc16_modbus(payload).to_bytes(2, "big")
        + payload
    )


def test_parse_upload_frame_extracts_inverter_readings() -> None:
    """An uploadDataReq frame carries the same list body as 0x4003."""
    body = p._field(
        7,
        2,
        p._field(
            1,
            2,
            p._field(1, 0, 13546061932007477) + p._field(7, 0, 1000),
        )
        + p._field(
            3,
            2,
            p._field(1, 0, 1)
            + p._field(2, 0, 320)
            + p._field(4, 0, 665)
            + p._field(5, 0, 61479),
        ),
    )

    readings = p.parse_upload_frame(_upload_frame(body))

    assert len(readings) == 1
    assert readings[0].sn == 13546061932007477
    assert readings[0].power_w == 66.5
    assert readings[0].energy_kwh == 614.79
    assert readings[0].mppts[0].voltage_v == 32.0


def test_parse_upload_frame_ignores_heartbeat() -> None:
    """A heartbeat upload arrives on the same topic but has no inverter list."""
    body = p._field(1, 0, 1) + p._field(2, 2, b"2026-10-26 13:35:30")

    assert p.parse_upload_frame(_upload_frame(body, opcode=0x1)) == []
