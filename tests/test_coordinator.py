"""Coordinator tests driven by genuinely encrypted frames.

The notifications below are built with the same packet builder the integration
writes with and then encrypted with the device key, so the handler exercises
the real framing, packet grouping, duplicate suppression and payload filter.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from custom_components.jackery_ble.coordinator import JackeryBleCoordinator
from custom_components.jackery_ble.jackery_ble_protocol import (
    JackeryBleCommand,
    JackeryBlePacketBuilder,
)
from custom_components.jackery_ble.jackery_device_data import JackeryDeviceData
from tests.conftest import CT_ADDRESS, CT_SN, HOME_ADDRESS, HOME_SN, LOGGER


def _encrypted_frames(
    device_data: JackeryDeviceData,
    body: dict[str, Any],
    *,
    action_id: int = 3019,
    ble_msg_type: int = 120,
    chunk_size: int = 1000,
) -> list[bytes]:
    """Build and encrypt the notifications a device would send."""
    builder = JackeryBlePacketBuilder.from_device_data(device_data)
    command = JackeryBleCommand(
        action_id=action_id, ble_msg_type=ble_msg_type, body=body
    )
    return [
        device_data.encrypt(builder.to_bytes(packet))
        for packet in builder.command_to_packets(command, chunk_size)
    ]


def _feed(coordinator: JackeryBleCoordinator, frames: list[bytes]) -> None:
    for frame in frames:
        coordinator._notification_handler(None, frame)


async def _coordinator(hass, config_entry, address: str) -> JackeryBleCoordinator:
    return JackeryBleCoordinator(hass, LOGGER, address, config_entry)


async def test_notification_payload_is_merged_into_data(
    hass, home_config_entry, home_device_data
) -> None:
    """A system frame updates the coordinator state."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    _feed(
        coordinator,
        _encrypted_frames(
            home_device_data,
            {"soc": 33, "pvPw": 1290, "deviceSn": HOME_SN},
        ),
    )

    assert coordinator.data == {"cmd": 120, "soc": 33, "pvPw": 1290, "deviceSn": HOME_SN}


async def test_successive_frames_accumulate(hass, home_config_entry, home_device_data) -> None:
    """Each device report only carries the fields that changed."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    _feed(coordinator, _encrypted_frames(home_device_data, {"soc": 33}))
    _feed(coordinator, _encrypted_frames(home_device_data, {"batOutPw": 0}))

    assert coordinator.data["soc"] == 33
    assert coordinator.data["batOutPw"] == 0


async def test_duplicate_frame_is_dropped(
    hass, home_config_entry, home_device_data
) -> None:
    """The devices repeat every frame; the second copy must not republish."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    frames = _encrypted_frames(home_device_data, {"soc": 33})
    _feed(coordinator, frames)

    with patch.object(coordinator, "async_set_updated_data") as updated:
        _feed(coordinator, frames)

    updated.assert_not_called()


async def test_other_device_payload_is_ignored(
    hass, home_config_entry, home_device_data
) -> None:
    """A bound accessory reporting over the vault link must not overwrite state."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    _feed(coordinator, _encrypted_frames(home_device_data, {"soc": 33}))

    _feed(
        coordinator,
        _encrypted_frames(home_device_data, {"soc": 99, "deviceSn": CT_SN}),
    )

    assert coordinator.data["soc"] == 33


async def test_system_sender_is_accepted(hass, home_config_entry, home_device_data) -> None:
    """Frames addressed to "system" belong to the vault itself."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    _feed(
        coordinator,
        _encrypted_frames(
            home_device_data, {"soc": 34, "deviceSn": "system"}
        ),
    )

    assert coordinator.data["soc"] == 34


async def test_unknown_message_type_is_ignored(
    hass, home_config_entry, home_device_data
) -> None:
    """Only the four live reports drive entities."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    _feed(
        coordinator,
        _encrypted_frames(
            home_device_data, {"cmd": 999, "soc": 99}, ble_msg_type=0
        ),
    )

    assert not coordinator.data


async def test_multipart_body_is_reassembled(
    hass, ct_config_entry, ct_device_data
) -> None:
    """The CT splits its report; nothing is published until all parts arrive."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)
    payload = {"power": 780, "volt": 231, "filler": "x" * 120}
    frames = _encrypted_frames(ct_device_data, payload, chunk_size=24)
    assert len(frames) > 2

    real_update = coordinator.async_set_updated_data
    with patch.object(
        coordinator, "async_set_updated_data", side_effect=real_update
    ) as updated:
        _feed(coordinator, frames[:-1])
        updated.assert_not_called()

        _feed(coordinator, frames[-1:])
        updated.assert_called_once()

    assert coordinator.data["power"] == 780
    assert coordinator.data["volt"] == 231
    assert coordinator.data["filler"] == "x" * 120


async def test_undecryptable_frame_is_ignored(hass, ct_config_entry) -> None:
    """A frame from a device with another key must be dropped silently."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)

    coordinator._notification_handler(None, b"\x00" * 48)

    assert not coordinator.data


async def test_foreign_frame_magic_is_ignored(hass, ct_config_entry, ct_device_data) -> None:
    """Frames that do not start with the Jackery preamble are not parsed."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)
    garbage = ct_device_data.encrypt(b"\x00\x00" + b"\x01" * 20)

    coordinator._notification_handler(None, garbage)

    assert not coordinator.data


async def test_cached_data_survives_a_manual_refresh(
    hass, home_config_entry, home_device_data
) -> None:
    """A manual refresh must not wipe push-driven values."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    _feed(coordinator, _encrypted_frames(home_device_data, {"soc": 33}))

    assert await coordinator._async_update_data() == coordinator.data


async def test_availability_requires_a_connection(hass, home_config_entry) -> None:
    """No client means unavailable, regardless of cached data."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    assert coordinator.available() is False


async def test_device_data_is_restored_from_the_entry(
    hass, ct_config_entry, ct_device_data
) -> None:
    """The coordinator must rebuild the same identity and key from the entry."""
    coordinator = await _coordinator(hass, ct_config_entry, CT_ADDRESS)

    assert coordinator.device_data.sn == CT_SN
    assert coordinator.device_data.encryption_key == ct_device_data.encryption_key
    assert coordinator.device_data.is_accessory is True


async def test_schedule_reply_resolves_a_pending_read(
    hass, home_config_entry, home_device_data
) -> None:
    """3018/112 answers a schedule read and must not touch live state."""
    from types import SimpleNamespace

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator._client = SimpleNamespace(is_connected=True)

    tasks = [
        {
            "tid": "1778312607",
            "loops": "1111111",
            "start": "08:00",
            "end": "19:00",
            "mode": 2,
            "pw": 1010,
            "sysSwitch": 1,
        }
    ]
    frames = _encrypted_frames(
        home_device_data,
        {"cmd": 112, "tasks": tasks},
        action_id=3018,
        ble_msg_type=112,
    )

    async def fake_send(command) -> None:
        assert command.action_id == 3018
        _feed(coordinator, frames)

    coordinator.async_send_command = fake_send

    assert await coordinator.async_read_schedule(max_wait=1.0) == tasks
    assert not coordinator.data


async def test_schedule_read_times_out_without_a_reply(
    hass, home_config_entry
) -> None:
    """A silent device must return None so callers keep their last plans."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator._client = SimpleNamespace(is_connected=True)
    coordinator.async_send_command = AsyncMock()

    assert await coordinator.async_read_schedule(max_wait=0.05) is None


async def test_schedule_read_requires_a_connection(
    hass, home_config_entry
) -> None:
    """No client means no request is attempted."""
    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    assert await coordinator.async_read_schedule(max_wait=0.05) is None


async def test_disconnect_schedules_a_reconnect(hass, home_config_entry) -> None:
    """An unexpected disconnect must start the retry loop."""
    from types import SimpleNamespace
    from unittest.mock import patch

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    client = SimpleNamespace(is_connected=False)
    coordinator._client = client

    with patch.object(coordinator, "_async_reconnect") as reconnect:
        coordinator._on_disconnected(client)
        await hass.async_block_till_done()

    assert coordinator._client is None
    reconnect.assert_called_once()


async def test_stale_callback_from_an_old_client_is_ignored(
    hass, home_config_entry
) -> None:
    """A late callback from a replaced connection must not disrupt the new one."""
    from types import SimpleNamespace
    from unittest.mock import patch

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    current = SimpleNamespace(is_connected=True)
    old = SimpleNamespace(is_connected=False)
    coordinator._client = current

    with patch.object(coordinator, "_async_start_reconnect") as start:
        coordinator._on_disconnected(old)

    assert coordinator._client is current
    start.assert_not_called()


async def test_shutdown_ignores_disconnects(hass, home_config_entry) -> None:
    """Disconnecting during unload must not start a reconnect."""
    from types import SimpleNamespace
    from unittest.mock import patch

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    client = SimpleNamespace(is_connected=False)
    coordinator._client = client
    coordinator._shutdown = True

    with patch.object(coordinator, "_async_start_reconnect") as start:
        coordinator._on_disconnected(client)

    start.assert_not_called()


async def test_watchdog_recycles_a_silent_link(hass, home_config_entry) -> None:
    """A connected link that stops sending is dropped, not trusted."""
    from time import monotonic
    from types import SimpleNamespace
    from unittest.mock import patch

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    coordinator._client = SimpleNamespace(is_connected=True)
    coordinator._frame_filter.last_seen = monotonic() - 1000

    with patch.object(coordinator, "_async_recycle") as recycle:
        coordinator._async_watchdog(None)
        await hass.async_block_till_done()

    recycle.assert_called_once()


async def test_watchdog_retries_a_dead_link(hass, home_config_entry) -> None:
    """With no client at all the watchdog restarts the reconnect loop."""
    from unittest.mock import patch

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    with patch.object(coordinator, "_async_start_reconnect") as start:
        coordinator._async_watchdog(None)

    start.assert_called_once()


async def test_reconnect_retries_until_the_device_answers(
    hass, home_config_entry
) -> None:
    """A failed attempt must be retried rather than abandoning the loop."""
    from unittest.mock import AsyncMock, patch

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)
    connect = AsyncMock(side_effect=[RuntimeError("not found"), None])

    with (
        patch.object(coordinator, "_async_connect", connect),
        patch(
            "custom_components.jackery_ble.coordinator.asyncio.sleep",
            new=AsyncMock(),
        ),
    ):
        await coordinator._async_reconnect()

    assert connect.await_count == 2
    assert coordinator._reconnect_task is None


async def test_send_command_requires_a_connection(hass, home_config_entry) -> None:
    """Writing while disconnected must fail loudly, not silently."""
    import pytest as pytest_module

    coordinator = await _coordinator(hass, home_config_entry, HOME_ADDRESS)

    with pytest_module.raises(RuntimeError):
        await coordinator.async_send_command(coordinator.commands.read_device_info())
