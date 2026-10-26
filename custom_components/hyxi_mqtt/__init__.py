"""Self-contained MQTT broker + HYXi DMU responder.

The DMU needs a plain MQTT broker on 1883 that accepts anonymous clients (its
username carries a per-connect random) and answers its PB requests.  Running
that in HA's own asyncio loop avoids a second process/add-on on a small VM.

Add to ``configuration.yaml``::

    hyxi_mqtt:

Optional::

    hyxi_mqtt:
      port: 1883
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .protocol import build_ack

DOMAIN = "hyxi_mqtt"
_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 1883
ACK_TOPIC = "device/commonSvrRsp/{}"
FRAME_LOG = "/config/hyxi_mqtt_frames.log"

#: Must match ``custom_components.hyxi_dmu.const.EVENT_PUSH``.
EVENT_PUSH = "hyxi_dmu_push"
UPLOAD_TOPIC_PREFIX = "device/uploadDataReq/"


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Match an MQTT topic filter (supports + and #)."""
    if topic_filter == topic:
        return True
    filter_parts = topic_filter.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[index]:
            return False
    return len(filter_parts) == len(topic_parts)


@dataclass(eq=False)
class _Client:
    """One connected MQTT client."""

    writer: asyncio.StreamWriter
    client_id: str = ""
    subscriptions: set[str] = field(default_factory=set)


class DmuBroker:
    """Minimal MQTT 3.1.1 broker with the DMU reply logic."""

    def __init__(self, hass: HomeAssistant, frames: logging.Logger) -> None:
        """Initialize the broker."""
        self.hass = hass
        self.frames = frames
        self.clients: set[_Client] = set()
        self._server: asyncio.AbstractServer | None = None

    async def start(self, port: int) -> None:
        """Bind and start serving."""
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", port)
        _LOGGER.info("HYXi DMU MQTT broker listening on 0.0.0.0:%d", port)

    async def stop(self) -> None:
        """Close the listener and all clients."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for client in list(self.clients):
            client.writer.close()
        self.clients.clear()

    # -- packet loop -------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = _Client(writer)
        self.clients.add(client)
        peer = writer.get_extra_info("peername")
        try:
            while True:
                first = await reader.readexactly(1)
                header = first[0]
                length = await self._read_varint(reader)
                body = await reader.readexactly(length)
                packet_type, flags = header >> 4, header & 0x0F
                if packet_type == 1:  # CONNECT
                    self._on_connect(client, body, peer)
                    writer.write(b"\x20\x02\x00\x00")
                    await writer.drain()
                elif packet_type == 3:  # PUBLISH
                    await self._on_publish(client, flags, body)
                elif packet_type == 8:  # SUBSCRIBE
                    self._on_subscribe(client, body)
                    await writer.drain()
                elif packet_type == 12:  # PINGREQ
                    self.frames.info("PINGREQ %r", client.client_id)
                    writer.write(b"\xd0\x00")
                    await writer.drain()
                elif packet_type == 14:  # DISCONNECT
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:  # a bad client must not kill the broker
            _LOGGER.exception("MQTT client %s failed", client.client_id or peer)
        finally:
            self.frames.info("DISCONNECT %r", client.client_id)
            self.clients.discard(client)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _read_varint(reader: asyncio.StreamReader) -> int:
        value = 0
        mult = 1
        while True:
            byte = (await reader.readexactly(1))[0]
            value += (byte & 0x7F) * mult
            if not byte & 0x80:
                return value
            mult *= 128

    # -- packets -----------------------------------------------------------

    def _on_connect(self, client: _Client, body: bytes, peer: object) -> None:
        try:
            index = 0
            name_len = int.from_bytes(body[0:2], "big")
            index = 2 + name_len
            flags = body[index + 1]
            index += 4
            cid_len = int.from_bytes(body[index : index + 2], "big")
            index += 2
            client.client_id = body[index : index + cid_len].decode("utf-8", "replace")
            index += cid_len
            username = ""
            if flags & 0x80:
                user_len = int.from_bytes(body[index : index + 2], "big")
                index += 2
                username = body[index : index + user_len].decode("utf-8", "replace")
                index += user_len
        except (IndexError, ValueError):
            username = ""
        self.frames.info(
            "CONNECT %r from %s username=%r", client.client_id, peer, username
        )

    def _on_subscribe(self, client: _Client, body: bytes) -> None:
        packet_id = int.from_bytes(body[0:2], "big")
        index = 2
        topics: list[str] = []
        while index < len(body):
            topic_len = int.from_bytes(body[index : index + 2], "big")
            index += 2
            topic = body[index : index + topic_len].decode("utf-8", "replace")
            index += topic_len + 1  # topic + requested QoS
            topics.append(topic)
            client.subscriptions.add(topic)
        self.frames.info("SUBSCRIBE %r %s", client.client_id, topics)
        _LOGGER.info("DMU MQTT SUBSCRIBE %s", topics)
        client.writer.write(
            bytes([0x90, 2 + len(topics)])
            + packet_id.to_bytes(2, "big")
            + bytes([0] * len(topics))
        )

    async def _on_publish(self, client: _Client, flags: int, body: bytes) -> None:
        qos = (flags >> 1) & 0x03
        topic_len = int.from_bytes(body[0:2], "big")
        topic = body[2 : 2 + topic_len].decode("utf-8", "replace")
        index = 2 + topic_len
        packet_id = None
        if qos:
            packet_id = int.from_bytes(body[index : index + 2], "big")
            index += 2
        payload = body[index:]

        if qos == 1 and packet_id is not None:
            client.writer.write(b"\x40\x02" + packet_id.to_bytes(2, "big"))

        self.frames.info("PUBLISH %s %s", topic, payload.hex())
        if topic.startswith(UPLOAD_TOPIC_PREFIX):
            self.hass.bus.async_fire(
                EVENT_PUSH,
                {
                    "sn": topic.rsplit("/", 1)[-1],
                    "topic": topic,
                    "payload": payload.hex(),
                },
            )
        await self._publish(topic, payload, exclude=client)

        result = build_ack(payload)
        if result is None:
            _LOGGER.debug("DMU PUBLISH %s (%d bytes, no ack)", topic, len(payload))
            return
        opcode, ack = result
        sn = topic.rsplit("/", 1)[-1]
        ack_topic = ACK_TOPIC.format(sn)
        await self._publish(ack_topic, ack)
        _LOGGER.debug("DMU PUBLISH %s op=%#x acked", topic, opcode)

    async def _publish(
        self, topic: str, payload: bytes, exclude: _Client | None = None
    ) -> None:
        body = len(topic).to_bytes(2, "big") + topic.encode() + payload
        packet = bytes([0x30]) + _varint(len(body)) + body
        for client in list(self.clients):
            if client is exclude:
                continue
            if any(topic_matches(sub, topic) for sub in client.subscriptions):
                with contextlib.suppress(Exception):
                    client.writer.write(packet)
                    await client.writer.drain()


def _varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out += bytes([byte | 0x80])
        else:
            return out + bytes([byte])


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Start the broker on HA's event loop."""
    options = config.get(DOMAIN) or {}
    port = int(options.get("port", DEFAULT_PORT))

    frames = logging.getLogger("hyxi_mqtt.frames")
    frames.setLevel(logging.INFO)
    frames.propagate = False
    if not frames.handlers:
        handler = logging.FileHandler(FRAME_LOG, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        frames.addHandler(handler)

    broker = DmuBroker(hass, frames)
    try:
        await broker.start(port)
    except OSError as err:
        _LOGGER.error("Cannot bind MQTT port %d: %s", port, err)
        return False

    hass.data[DOMAIN] = broker

    async def _shutdown(_event: Event) -> None:
        await broker.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    return True
