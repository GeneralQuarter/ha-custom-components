"""DataUpdateCoordinator for Jackery BLE."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from json import JSONDecodeError
from time import monotonic
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import establish_connection
from homeassistant.components.bluetooth.api import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHARACTERISTIC_DATA_NOTIFY,
    CHARACTERISTIC_DATA_WRITE,
    CONF_DEVICE_DATA,
)
from .frame_filter import STALE_AFTER, FrameFilter
from .jackery_ble_protocol import (
    JackeryBleCommand,
    JackeryBleCommands,
    JackeryBlePacketBuilder,
    JackeryBlePacketGroup,
)
from .jackery_device_data import JackeryDeviceData

# Back-off between reconnect attempts; the last value keeps repeating.
RECONNECT_DELAYS = (5, 15, 30, 60, 120)

# A live link that stops delivering frames is recycled rather than trusted.
STALE_RECONNECT_AFTER = 90.0
WATCHDOG_INTERVAL = timedelta(seconds=30)


class JackeryBleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Jackery Ble Coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger,
        address: str,
        config_entry: ConfigEntry,
    ) -> None:
        """Constructor."""
        super().__init__(
            hass,
            logger,
            name=config_entry.data.get(CONF_DEVICE_DATA)["sn"],
            config_entry=config_entry,
        )

        self._address = address
        self._client: BleakClient | None = None
        self.device_data = JackeryDeviceData.from_config_entry_device_data(
            config_entry.data.get(CONF_DEVICE_DATA)
        )
        self._packet_builder = JackeryBlePacketBuilder.from_device_data(
            self.device_data
        )
        self._commands = JackeryBleCommands(self.device_data)
        self._data_notify_characteristic: BleakGATTCharacteristic | None = None
        self._data_write_characteristic: BleakGATTCharacteristic | None = None
        self._packet_groups: dict[int, JackeryBlePacketGroup] = {}
        self._cached_data: dict[str, Any] = {}
        self._frame_filter = FrameFilter()
        self._schedule_future: asyncio.Future[list[dict]] | None = None
        self.supervisor: Any | None = None
        self._shutdown = False
        self._reconnect_task: asyncio.Task | None = None
        self._watchdog_unsub = None

    @property
    def commands(self) -> JackeryBleCommands:
        """Command builders for this device."""
        return self._commands

    def _on_completed_packet_group(self, packet_group: JackeryBlePacketGroup):
        if packet_group.action_id in self._packet_groups:
            self._packet_groups.pop(packet_group.action_id)

        try:
            body = packet_group.body
        except JSONDecodeError:
            self.logger.error("Could not decode body: %s", packet_group.raw_body)
            body = None

        self.logger.debug("Packet group body: %s", body)

        if not body:
            return

        # The schedule family replies with a task list rather than live state.
        if body.get("cmd") == 112:
            tasks = body.get("tasks")
            if isinstance(tasks, list):
                future = self._schedule_future
                if future is not None and not future.done():
                    future.set_result(tasks)
            return

        if body.get("cmd") in [106, 107, 120, 121]:
            device_sn = body.get("deviceSn")
            if device_sn not in (None, "system", self.device_data.sn):
                return
            self._cached_data.update(body)
            self.async_set_updated_data(self._cached_data)

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the latest pushed data.

        The device is push-driven (the CT reports every ~2 s), so there is no
        periodic polling. Returning the cache keeps a manual refresh from
        wiping known values.
        """
        return dict(self._cached_data)

    @callback
    def _notification_handler(self, _: BleakGATTCharacteristic, data: bytes):
        decrypted = self.device_data.decrypt(data)
        if decrypted is None:
            self.logger.debug("Could not decrypt notification: %s", data.hex())
            return
        if not decrypted.startswith(b"\xdf\xed"):
            self.logger.debug("Unexpected frame magic: %s", decrypted[:4].hex())
            return

        # Devices repeat every frame byte-for-byte a few hundred ms later.
        if not self._frame_filter.accept(decrypted):
            self.logger.debug("Dropped duplicate frame: %s", decrypted[:16].hex())
            return

        try:
            packet = self._packet_builder.to_packet(decrypted)
        except ValueError:
            self.logger.debug("Could not parse notification: %s", decrypted.hex())
            return

        self.logger.debug("Packet received: %s", packet)

        if packet.header.count == 1:
            self._on_completed_packet_group(JackeryBlePacketGroup(packet))
            return

        action_id = packet.header.action_id

        if action_id in self._packet_groups:
            self._packet_groups[action_id].add_packet(packet)
        else:
            self._packet_groups[action_id] = JackeryBlePacketGroup(packet)

        if self._packet_groups[action_id].complete:
            self._on_completed_packet_group(self._packet_groups[action_id])

    async def _async_setup(self):
        """Connect to the Jackery device and start listening for notifications."""
        self._shutdown = False
        await self._async_connect()
        self._watchdog_unsub = async_track_time_interval(
            self.hass, self._async_watchdog, WATCHDOG_INTERVAL
        )

    async def _async_connect(self) -> None:
        """Open the link, subscribe to notifications and send the handshake."""
        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )

        if not ble_device:
            raise RuntimeError(f"Device {self._address} not found in Bluetooth cache")

        client = await establish_connection(
            BleakClient,
            ble_device,
            ble_device.address,
            disconnected_callback=self._on_disconnected,
            max_attempts=1,
        )
        self._client = client

        try:
            notify = client.services.get_characteristic(CHARACTERISTIC_DATA_NOTIFY)
            write = client.services.get_characteristic(CHARACTERISTIC_DATA_WRITE)

            if not notify or not write:
                raise RuntimeError(
                    "Device does not have the required characteristics."
                )

            self._data_notify_characteristic = notify
            self._data_write_characteristic = write

            await client.start_notify(notify, self._notification_handler)

            await self.async_send_command(self._commands.send_time_zone())
            await self.async_send_command(self._commands.read_device_info())
            await self.async_send_command(self._commands.read_system_info())
        except Exception:
            # Never leave a half-open connection behind; the caller retries.
            self._client = None
            with suppress(Exception):
                await client.disconnect()
            raise

        # Give the watchdog a reference point: a link that connects but never
        # delivers a frame must be recycled instead of trusted.
        self._frame_filter.last_seen = monotonic()

    @callback
    def _on_disconnected(self, client: BleakClient) -> None:
        """Handle an unexpected disconnect."""
        if self._shutdown or self._client is not client:
            return

        self.logger.warning("Disconnected from %s; reconnecting", self._address)
        self._client = None
        self._data_notify_characteristic = None
        self._data_write_characteristic = None
        # Let entities re-evaluate availability right away.
        self.async_set_updated_data(self._cached_data)
        self._async_start_reconnect()

    @callback
    def _async_start_reconnect(self) -> None:
        """Start the reconnect loop unless one is already running."""
        if self._shutdown:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(
            self._async_reconnect(), f"{self.name} reconnect"
        )

    async def _async_reconnect(self) -> None:
        """Retry until the device answers again, then refresh its state."""
        attempt = 0
        while not self._shutdown:
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            await asyncio.sleep(delay)
            if self._shutdown:
                return

            try:
                await self._async_connect()
            except Exception as err:  # the next attempt may still succeed
                attempt += 1
                self.logger.debug(
                    "Reconnect attempt %d for %s failed: %s",
                    attempt,
                    self._address,
                    err,
                )
                continue

            self.logger.info("Reconnected to %s", self._address)
            return

    @callback
    def _async_watchdog(self, _now) -> None:
        """Recycle a live link that stopped sending, or retry a dead one."""
        if self._shutdown:
            return

        if self._client is None or not self._client.is_connected:
            self._async_start_reconnect()
            return

        if self._frame_filter.is_stale(STALE_RECONNECT_AFTER):
            self.logger.warning(
                "No frames for %.0f s from %s; recycling the connection",
                STALE_RECONNECT_AFTER,
                self._address,
            )
            self.hass.async_create_task(
                self._async_recycle(), f"{self.name} recycle"
            )

    async def _async_recycle(self) -> None:
        """Drop a silent connection so the reconnect loop takes over."""
        client = self._client
        if client is None:
            return

        with suppress(Exception):
            await client.disconnect()

        if self._client is client:
            self._client = None
            self.async_set_updated_data(self._cached_data)

        self._async_start_reconnect()

    async def async_shutdown(self):
        """Stop listening for Jackery device notifications and disconnect it."""
        self._shutdown = True

        if self._watchdog_unsub is not None:
            self._watchdog_unsub()
            self._watchdog_unsub = None

        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        if self._schedule_future is not None and not self._schedule_future.done():
            self._schedule_future.cancel()
            self._schedule_future = None

        if self._client and self._client.is_connected:
            if self._data_notify_characteristic is not None:
                await self._client.stop_notify(self._data_notify_characteristic)
            await self._client.disconnect()

    async def async_send_command(self, command: JackeryBleCommand | None):
        """Write a command to the device."""
        if not command:
            return

        if self._client is None or not self._client.is_connected:
            raise RuntimeError(f"Device {self._address} is not connected")

        mtu = getattr(self._client, "mtu_size", None) or 247
        if mtu < 61:
            mtu = self._data_write_characteristic.max_write_without_response_size + 3

        if mtu < 61:
            raise RuntimeError(f"Device MTU is too small: {mtu}")

        self.logger.debug(
            "Send command %s (%s): %s",
            command.ble_msg_type,
            command.action_id,
            command.body,
        )

        for packet in self._packet_builder.command_to_packets(command, mtu - 60):
            await self._client.write_gatt_char(
                self._data_write_characteristic,
                self.device_data.encrypt(self._packet_builder.to_bytes(packet)),
                response=False,
            )

    async def async_read_schedule(self, max_wait: float = 5.0) -> list[dict] | None:
        """Request the custom-mode schedule and wait for the reply.

        Returns None when the device is not connected or does not answer, so
        callers can keep their last known plans instead of acting on nothing.
        """
        if self._client is None or not self._client.is_connected:
            return None

        future: asyncio.Future[list[dict]] = (
            asyncio.get_running_loop().create_future()
        )
        self._schedule_future = future
        try:
            await self.async_send_command(self._commands.read_schedule_tasks())
            return await asyncio.wait_for(future, max_wait)
        except TimeoutError:
            self.logger.debug("Schedule read timed out")
            return None
        finally:
            self._schedule_future = None

    def available(self) -> bool:
        """Return True when connected and still receiving data."""
        if self._client is None or not self._client.is_connected:
            return False

        return not self._frame_filter.is_stale(STALE_AFTER)
