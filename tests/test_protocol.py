"""Protocol-level tests.

These exercise the pure protocol layer only: no Home Assistant runtime, no
Bluetooth stack, no device. They are the fastest signal when the frame format
or the command bodies change.
"""

from __future__ import annotations

import json

import pytest

from custom_components.jackery_ble.jackery_ble_protocol import (
    SCHEDULE_ACTION_ADD,
    SCHEDULE_ACTION_DELETE,
    SCHEDULE_ACTION_READ,
    SCHEDULE_ACTION_UPDATE,
    JackeryBleCommand,
    JackeryBleCommands,
    JackeryBlePacketBuilder,
    JackeryBlePacketGroup,
    ScheduleTask,
)
from custom_components.jackery_ble.jackery_crypto import crc16, crc16_checksum, rc4
from custom_components.jackery_ble.jackery_device_data import (
    JackeryDeviceCategory,
    JackeryDeviceData,
    JackeryDeviceModelType,
)

HOME_SN = "HR2C000000001"
CT_SN = "HW2C000000002"


def _home_device() -> JackeryDeviceData:
    return JackeryDeviceData(
        sn=HOME_SN,
        guid=b"\x01\x02\x03\x04\x05\x06",
        model_code=3002,
        category=JackeryDeviceCategory.JACKERY_DEVICE,
        encryption_key=bytes(range(24)),
    )


def _ct_device() -> JackeryDeviceData:
    return JackeryDeviceData(
        sn=CT_SN,
        guid=b"\x0a\x0b\x0c\x0d\x0e\x0f",
        model_code=4002,
        category=JackeryDeviceCategory.JACKERY_ACCESSORY,
        encryption_key=bytes(range(16)),
    )


def test_home_frame_header_is_sixteen_bytes() -> None:
    """The SolarVault frame header carries two extra fields over the CT."""
    assert JackeryBlePacketBuilder.home().header_struct.size == 16
    assert JackeryBlePacketBuilder.accessory().header_struct.size == 12


def test_packet_bytes_round_trip() -> None:
    """A built packet must parse back to the same header and body."""
    builder = JackeryBlePacketBuilder.home()
    command = JackeryBleCommands(_home_device()).set_max_feed_grid(700)

    packets = builder.command_to_packets(command, chunk_size=200)
    assert len(packets) == 1

    wire = builder.to_bytes(packets[0])
    reparsed = builder.to_packet(wire)

    assert reparsed.header.action_id == 3029
    assert reparsed.header.ble_msg_type == 121
    assert json.loads(reparsed.body) == {"cmd": 121, "maxFeedGrid": 700}


def test_soc_limits_share_one_frame() -> None:
    """Both battery limits travel together, as the app writes them (3028/121)."""
    command = JackeryBleCommands(_home_device()).set_soc_limits(85, 30)

    assert command.action_id == 3028
    assert command.ble_msg_type == 121
    assert command.body == {"cmd": 121, "socChgLimit": 85, "socDischgLimit": 30}


def test_command_chunking_respects_count_and_pages() -> None:
    """Long bodies are split, numbered from page 1, and reassemble losslessly."""
    builder = JackeryBlePacketBuilder.home()
    body = {"cmd": 112, "payload": "x" * 250}
    command = JackeryBleCommand(action_id=3017, ble_msg_type=112, body=body)

    packets = builder.command_to_packets(command, chunk_size=40)
    expected = json.dumps(body, separators=(",", ":")).encode()

    assert len(packets) > 1
    assert [p.header.page for p in packets] == list(range(1, len(packets) + 1))
    assert all(p.header.count == len(packets) for p in packets)
    assert b"".join(p.body for p in packets) == expected
    assert [p.header.length for p in packets] == [len(p.body) for p in packets]


def test_command_chunking_rejects_zero_chunk_size() -> None:
    """A zero chunk size would loop forever, so it must be rejected."""
    command = JackeryBleCommand(action_id=3019, ble_msg_type=120, body={})

    with pytest.raises(ValueError):
        JackeryBlePacketBuilder.home().command_to_packets(command, chunk_size=0)


def test_cmd_is_injected_from_message_type() -> None:
    """The schedule family carries its sub-command inside the JSON body."""
    command = JackeryBleCommand(action_id=3017, ble_msg_type=112, body={"pw": 1010})
    assert command.body["cmd"] == 112

    untouched = JackeryBleCommand(action_id=3038, ble_msg_type=0, body={"pw": 1010})
    assert "cmd" not in untouched.body


def test_raw_body_trims_using_header_length() -> None:
    """Inbound frames carry a random suffix that the header length excludes."""
    builder = JackeryBlePacketBuilder.home()
    packets = builder.command_to_packets(
        JackeryBleCommand(action_id=3019, ble_msg_type=120, body={"a": 1, "b": 2}),
        chunk_size=12,
    )
    assert len(packets) == 2

    first, second = packets
    # Simulate the opaque trailer the device appends to each notification.
    first.body += b"\xde\xad"
    second.body += b"\xbe\xef"

    group = JackeryBlePacketGroup(first)
    group.add_packet(second)

    assert group.complete
    assert group.raw_body == json.dumps(
        {"a": 1, "b": 2, "cmd": 120}, separators=(",", ":")
    ).encode()


def test_raw_body_falls_back_to_stripping_two_bytes() -> None:
    """Devices that leave the length field at zero still parse."""
    builder = JackeryBlePacketBuilder.home()
    packet = builder.command_to_packets(
        JackeryBleCommand(action_id=3019, ble_msg_type=120, body={"a": 1}),
        chunk_size=500,
    )[0]
    header = packet.header._replace(length=0)
    packet = type(packet)(header=header, body=packet.body + b"\x00\x00")

    group = JackeryBlePacketGroup(packet)
    assert group.body == {"cmd": 120, "a": 1}


def test_incomplete_group_has_no_body() -> None:
    """A partially received frame must not be decoded."""
    builder = JackeryBlePacketBuilder.home()
    packets = builder.command_to_packets(
        JackeryBleCommand(action_id=3019, ble_msg_type=120, body={"k": "v" * 100}),
        chunk_size=20,
    )

    group = JackeryBlePacketGroup(packets[0])
    assert not group.complete
    assert group.body is None

    for packet in packets[1:]:
        group.add_packet(packet)
    assert group.complete
    assert group.body == {"cmd": 120, "k": "v" * 100}


def test_group_ignores_foreign_action_and_bad_page() -> None:
    """Out-of-order frames from other actions must not corrupt a group."""
    builder = JackeryBlePacketBuilder.home()
    group_packets = builder.command_to_packets(
        JackeryBleCommand(action_id=3019, ble_msg_type=120, body={"k": "v" * 60}),
        chunk_size=20,
    )
    foreign = builder.command_to_packets(
        JackeryBleCommand(action_id=3011, ble_msg_type=106, body={"k": "v" * 60}),
        chunk_size=20,
    )[0]

    group = JackeryBlePacketGroup(group_packets[0])
    group.add_packet(foreign)
    bad_page = group_packets[1]
    group.add_packet(
        type(bad_page)(
            header=bad_page.header._replace(page=99),
            body=bad_page.body,
        )
    )

    assert not group.complete


@pytest.mark.parametrize("family", ["home", "accessory"])
def test_group_completes_in_any_order(family: str) -> None:
    """Notifications can arrive out of order and still reassemble."""
    factory = (
        JackeryBlePacketBuilder.home
        if family == "home"
        else JackeryBlePacketBuilder.accessory
    )
    frame_builder = factory()
    payload = "v" * 80
    packets = frame_builder.command_to_packets(
        JackeryBleCommand(action_id=3019, ble_msg_type=120, body={"k": payload}),
        chunk_size=30,
    )
    assert len(packets) > 2

    group = JackeryBlePacketGroup(packets[-1])
    for packet in reversed(packets[:-1]):
        group.add_packet(packet)

    assert group.complete
    assert group.body == {"k": payload, "cmd": 120}


def test_encryption_round_trip_keeps_payload_and_crc() -> None:
    """Encrypt/decrypt must be lossless and append security + CRC16."""
    device = _home_device()
    payload = b"\xdf\xed\x00\x01" + b"payload"

    decrypted = device.decrypt(device.encrypt(payload))

    assert decrypted is not None
    assert decrypted.startswith(payload)
    assert len(decrypted) == len(payload) + 4
    assert crc16_checksum(decrypted) == payload + decrypted[len(payload) : len(payload) + 2]


def test_decrypt_requires_a_key_of_a_known_length() -> None:
    """An empty or truncated key must not raise, the frame is simply dropped."""
    device = JackeryDeviceData(sn=HOME_SN, encryption_key=b"\x00" * 7)

    assert device.decrypt(b"\x00" * 48) is None
    assert device.decrypt(b"") is None
    assert device.decrypt(b"\x00" * 31) is None


def test_decrypt_rejects_wrong_key_material() -> None:
    """Garbage input must return None rather than raise across the BLE callback."""
    device = _home_device()

    assert device.decrypt(b"\x11" * 48) is None


def test_rc4_is_symmetric_and_crc16_detects_flips() -> None:
    """Both helpers are used to unwrap CT advertisements, so pin their behaviour."""
    key = b"0123456789abcdef01234567"
    assert rc4(rc4(b"hello world", key), key) == b"hello world"

    framed = b"abc" + crc16(b"abc")
    assert crc16_checksum(framed) == b"abc"
    assert crc16_checksum(b"abc" + b"\x00\x00") is None


def test_accessory_commands_are_restricted_to_the_vault() -> None:
    """CT-side code must not attempt vault-only writes."""
    accessory_commands = JackeryBleCommands(_ct_device())
    vault_commands = JackeryBleCommands(_home_device())

    assert accessory_commands.read_device_info().action_id == 4005
    assert accessory_commands.read_system_info() is None
    assert vault_commands.read_system_info().action_id == 3019

    with pytest.raises(ValueError):
        accessory_commands.set_work_model(2)


def test_time_zone_command_matches_device_family() -> None:
    """Accessories expect action 4003 where the vault expects 3003."""
    vault = JackeryBleCommands(_home_device()).send_time_zone()
    accessory = JackeryBleCommands(_ct_device()).send_time_zone()

    assert vault.action_id == 3003
    assert accessory.action_id == 4003
    # The message envelope always carries its own cmd, like every other write.
    assert vault.body["cmd"] == 3
    assert {"uo", "ts", "timezone"} <= set(vault.body)


def test_model_lookup_drives_the_frame_family() -> None:
    """Model code 4002 must select the 12-byte accessory header."""
    assert _home_device().model_type is JackeryDeviceModelType.HOME
    assert _ct_device().model_type is JackeryDeviceModelType.ACCESSORY
    assert _home_device().model_data.name == "SolarVault 3 Pro Max"
    assert _ct_device().name == f"Smart Meter 1P ({CT_SN})"


def test_config_entry_round_trip_preserves_device_data() -> None:
    """The config entry payload is the only place the key is persisted."""
    device = _home_device()

    restored = JackeryDeviceData.from_config_entry_device_data(
        device.to_config_entry_device_data()
    )

    assert restored == device
    assert restored.encryption_key == bytes(range(24))


# ---------------------------------------------------------------------------
# Custom-mode schedule commands (cmd 112)
# ---------------------------------------------------------------------------

_ACTIVE_PLAN = {
    "tid": "1778312607",
    "loops": "1111111",
    "start": "08:00",
    "end": "19:00",
    "mode": 2,
    "pw": 1010,
    "sysSwitch": 1,
}


def test_update_schedule_task_matches_the_captured_frame() -> None:
    """The body must reproduce the frame that was accepted by the vault."""
    commands = JackeryBleCommands(_home_device())
    task = ScheduleTask.from_body(_ACTIVE_PLAN, pw=1500)

    command = commands.update_schedule_task(task)

    assert command.action_id == 3017
    assert command.ble_msg_type == 112
    assert command.body == {
        "actionType": SCHEDULE_ACTION_UPDATE,
        "taskType": 2,
        "tid": "1778312607",
        "start": "08:00",
        "end": "19:00",
        "loops": "1111111",
        "mode": 2,
        "pw": 1500,
        "sysSwitch": 1,
        "cmd": 112,
    }


def test_schedule_actions_share_the_family() -> None:
    """Add/delete/read use their own action id but the same envelope."""
    commands = JackeryBleCommands(_home_device())
    task = ScheduleTask.from_body(_ACTIVE_PLAN)

    assert commands.add_schedule_task(task).action_id == 3015
    assert commands.add_schedule_task(task).body["actionType"] == SCHEDULE_ACTION_ADD
    assert commands.delete_schedule_task(task).action_id == 3016
    assert (
        commands.delete_schedule_task(task).body["actionType"]
        == SCHEDULE_ACTION_DELETE
    )
    assert commands.read_schedule_tasks().action_id == 3018
    assert commands.read_schedule_tasks().body == {
        "actionType": SCHEDULE_ACTION_READ,
        "taskType": 2,
        "cmd": 112,
    }


def test_schedule_task_round_trips_through_a_reply() -> None:
    """A plan read back from 3018 must rebuild into the same write body."""
    task = ScheduleTask.from_body(_ACTIVE_PLAN, pw=0)

    assert task.pw == 0
    assert task.tid == "1778312607"
    assert task.to_body(SCHEDULE_ACTION_UPDATE)["sysSwitch"] == 1


def test_schedule_command_is_rejected_on_the_accessory() -> None:
    """The CT has no schedule; building one must fail loudly."""
    commands = JackeryBleCommands(_ct_device())
    task = ScheduleTask.from_body(_ACTIVE_PLAN)

    with pytest.raises(ValueError):
        commands.update_schedule_task(task)
