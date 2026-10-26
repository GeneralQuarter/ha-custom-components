"""Coordinator that polls the HYX-DMU over its local TCP service."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DOMAIN,
    REQUEST_TIMEOUT,
)
from .protocol import (
    CHANNEL_PV,
    OP_LIST,
    InverterReading,
    ProtocolError,
    build_login,
    build_request,
    build_restart,
    extract_frames,
    opcode_of,
    parse_list_reply,
)

_LOGGER = logging.getLogger(__name__)

REPLY_LIST = 0x4003
REPLY_RESTART = 0x4005
REPLY_LOGIN = 0x4022
RESTART_LOG = Path("/config/hyxi_dmu_restart.log")


def _restart_log(message: str) -> None:
    """Append a restart diagnostic without leaking credentials."""
    try:
        with RESTART_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        _LOGGER.debug("Cannot write restart log: %s", message)


async def _read_reply(reader: asyncio.StreamReader, wanted: int) -> bytes:
    """Read frames until the wanted reply arrives (caller applies a timeout)."""
    buffer = b""

    while True:
        chunk = await reader.read(4096)
        if not chunk:
            raise ConnectionError("DMU closed the connection")

        buffer += chunk
        frames, buffer = extract_frames(buffer)
        for payload in frames:
            if opcode_of(payload) == wanted:
                return payload


async def _login(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, password: str
) -> None:
    """Send the login handshake that unlocks the inverter channel."""
    writer.write(build_login(password))
    await writer.drain()
    await _read_reply(reader, REPLY_LOGIN)


async def async_probe(host: str, password: str = DEFAULT_PASSWORD) -> None:
    """Log in and read the device list; used to validate a config entry."""
    async with asyncio.timeout(20):
        reader, writer = await asyncio.open_connection(host, DEFAULT_PORT)
        try:
            await _login(reader, writer, password)
            writer.write(build_request(OP_LIST, CHANNEL_PV))
            await writer.drain()
            await _read_reply(reader, REPLY_LIST)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


class HyxiDmuCoordinator(DataUpdateCoordinator[list[InverterReading]]):
    """Hold the DMU's live readings, normally delivered by MQTT push.

    There is deliberately no periodic poll: the DMU prioritises a local 333
    request over its own cloud/MQTT upload, so polling every few seconds
    starves the uplink.  A single snapshot is taken at setup, then updates
    arrive from ``hyxi_mqtt`` as ``uploadDataReq`` frames.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        password: str = DEFAULT_PASSWORD,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Constructor."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} {host}",
            update_interval=None,
        )
        self.host = host
        self.password = password

    def async_apply_push(self, readings: list[InverterReading]) -> None:
        """Publish one pushed snapshot, retaining inverters it omitted.

        The DMU normally includes every inverter, but a partial list would make
        the site energy total drop and look like a counter reset.
        """
        if not readings:
            return
        merged = {reading.sn: reading for reading in self.data or []}
        merged.update({reading.sn: reading for reading in readings})
        self.async_set_updated_data(list(merged.values()))

    async def _restart_once(self) -> None:
        """One login + C005 attempt."""
        _restart_log(f"connect {self.host}:{DEFAULT_PORT}")
        reader, writer = await asyncio.open_connection(self.host, DEFAULT_PORT)
        try:
            await _login(reader, writer, self.password)
            _restart_log("login ok")
            writer.write(build_restart())
            await writer.drain()
            _restart_log("c005 sent")
            with suppress(ConnectionError, TimeoutError):
                async with asyncio.timeout(8):
                    await _read_reply(reader, REPLY_RESTART)
                _restart_log("c005 reply")
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def async_restart(self) -> None:
        """Reboot the DMU, retrying if the local 333 service is busy."""
        last_error: Exception | None = None
        _restart_log(f"restart requested via {self.host}")
        for attempt in range(1, 4):
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    await self._restart_once()
            except (OSError, TimeoutError, ProtocolError, ConnectionError) as err:
                last_error = err
                _restart_log(f"attempt {attempt} failed: {type(err).__name__}: {err}")
                _LOGGER.warning(
                    "Restart attempt %d for %s failed: %s", attempt, self.host, err
                )
                if attempt < 3:
                    await asyncio.sleep(2)
            else:
                _restart_log(f"restart sent on attempt {attempt}")
                _LOGGER.info("Restart requested from %s (attempt %d)", self.host, attempt)
                return
        _restart_log(f"restart failed: {last_error!r}")
        raise UpdateFailed(f"Could not restart {self.host}: {last_error}") from last_error

    async def _async_update_data(self) -> list[InverterReading]:
        """Fetch one list snapshot."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                return await self._fetch()
        except (OSError, TimeoutError, ProtocolError) as err:
            raise UpdateFailed(f"Cannot read {self.host}: {err}") from err

    async def _fetch(self) -> list[InverterReading]:
        reader, writer = await asyncio.open_connection(self.host, DEFAULT_PORT)
        try:
            await _login(reader, writer, self.password)
            writer.write(build_request(OP_LIST, CHANNEL_PV))
            await writer.drain()
            async with asyncio.timeout(REQUEST_TIMEOUT - 2):
                payload = await _read_reply(reader, REPLY_LIST)
            return parse_list_reply(payload)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
