"""Jackery BLE Protocol.

Pure protocol helpers (no Home Assistant imports) so they can be reused by the
standalone probe in ``tools/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from json import dumps, loads
from struct import Struct
from typing import Any, NamedTuple

from .jackery_device_data import JackeryDeviceData, JackeryDeviceModelType

# Custom-mode schedule family: every operation shares ``cmd 112`` and carries
# its own action type inside the JSON body.
SCHEDULE_ACTION_ADD = 1
SCHEDULE_ACTION_DELETE = 2
SCHEDULE_ACTION_UPDATE = 3
SCHEDULE_ACTION_READ = 4

SCHEDULE_CUSTOM = 2
SCHEDULE_MODE_CHARGE = 1
SCHEDULE_MODE_DISCHARGE = 2


@dataclass(frozen=True)
class ScheduleTask:
    """One custom-mode power plan.

    The vault stores only ``tid/loops/start/end/mode/pw/sysSwitch``; the task
    type has to be supplied on every write.
    """

    tid: str
    start: str
    end: str
    pw: int
    mode: int = SCHEDULE_MODE_DISCHARGE
    loops: str = "1111111"
    sys_switch: int = 1

    @classmethod
    def from_body(cls, body: dict[str, Any], **overrides: Any) -> ScheduleTask:
        """Build from a ``3018/112`` task entry, allowing field overrides."""
        return cls(
            tid=str(body["tid"]),
            start=str(body["start"]),
            end=str(body["end"]),
            pw=int(body["pw"]),
            mode=int(body.get("mode", SCHEDULE_MODE_DISCHARGE)),
            loops=str(body.get("loops", "1111111")),
            sys_switch=int(body.get("sysSwitch", 1)),
        ).with_overrides(**overrides)

    def with_overrides(self, **overrides: Any) -> ScheduleTask:
        """Return a copy with selected fields replaced."""
        values = {
            "tid": self.tid,
            "start": self.start,
            "end": self.end,
            "pw": self.pw,
            "mode": self.mode,
            "loops": self.loops,
            "sys_switch": self.sys_switch,
        }
        values.update(overrides)
        return ScheduleTask(**values)

    def to_body(self, action_type: int) -> dict[str, Any]:
        """Render the JSON body for the given schedule action."""
        body: dict[str, Any] = {
            "actionType": action_type,
            "taskType": SCHEDULE_CUSTOM,
            "tid": self.tid,
        }
        if action_type == SCHEDULE_ACTION_READ:
            return body
        body.update(
            {
                "start": self.start,
                "end": self.end,
                "loops": self.loops,
                "mode": int(self.mode),
                "pw": int(self.pw),
                "sysSwitch": int(self.sys_switch),
            }
        )
        return body


class JackeryBleCommands:
    """Jackery BLE commands."""

    def __init__(self, device_data: JackeryDeviceData) -> None:
        """Constructor."""
        self._device_data = device_data

    def _require_home(self) -> None:
        if self._device_data.is_accessory:
            raise ValueError("Command is only supported on the SolarVault")

    def read_device_info(self) -> JackeryBleCommand:
        """Read the device info."""
        return JackeryBleCommand(
            action_id=4005 if self._device_data.is_accessory else 3011,
            ble_msg_type=106,
            body={},
        )

    def read_system_info(self) -> JackeryBleCommand | None:
        """Read the system info."""
        if self._device_data.is_accessory:
            return None
        return JackeryBleCommand(action_id=3019, ble_msg_type=120, body={})

    def send_time_zone(self) -> JackeryBleCommand:
        """Send the local time zone."""
        now = datetime.now()
        timezone = now.astimezone()

        return JackeryBleCommand(
            action_id=4003 if self._device_data.is_accessory else 3003,
            ble_msg_type=3,
            body={
                "uo": timezone.utcoffset().total_seconds(),
                "ts": now.timestamp(),
                "timezone": timezone.tzname(),
            },
        )

    def set_work_model(self, work_model: int) -> JackeryBleCommand:
        """Set the work model (2 = self-use, 4 = custom, 7 = tariff, 8 = AI)."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3027, ble_msg_type=121, body={"workModel": int(work_model)}
        )

    def set_follow_meter(self, enabled: bool) -> JackeryBleCommand:
        """Enable/disable the native follow-meter loop."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3044,
            ble_msg_type=121,
            body={"isFollowMeterPw": int(bool(enabled))},
        )

    def set_default_power(self, watts: int) -> JackeryBleCommand:
        """Set the custom-mode fallback output power."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3043, ble_msg_type=121, body={"defaultPw": int(watts)}
        )

    def set_max_feed_grid(self, watts: int) -> JackeryBleCommand:
        """Set the maximum grid feed-in power."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3029, ble_msg_type=121, body={"maxFeedGrid": int(watts)}
        )

    def set_soc_limits(
        self, charge_limit: int, discharge_limit: int
    ) -> JackeryBleCommand:
        """Set the charge and discharge SOC limits in percent (3028/121).

        The device takes both values in one frame, applied immediately, and
        echoes them back in ``3011/106``.
        """
        self._require_home()
        return JackeryBleCommand(
            action_id=3028,
            ble_msg_type=121,
            body={
                "socChgLimit": int(charge_limit),
                "socDischgLimit": int(discharge_limit),
            },
        )

    def bind_smart_accessory(self, sn: str) -> JackeryBleCommand:
        """Bind a smart accessory (e.g. the CT meter) to the SolarVault."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3012, ble_msg_type=108, body={"sn": sn}
        )

    def unbind_smart_accessory(self, sn: str) -> JackeryBleCommand:
        """Unbind a smart accessory from the SolarVault."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3013, ble_msg_type=109, body={"sn": sn}
        )

    def read_schedule_tasks(self) -> JackeryBleCommand:
        """Ask for the custom-mode schedule (cmd 112, actionType 4)."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3018,
            ble_msg_type=112,
            body={"actionType": SCHEDULE_ACTION_READ, "taskType": SCHEDULE_CUSTOM},
        )

    def add_schedule_task(self, task: ScheduleTask) -> JackeryBleCommand:
        """Create a custom-mode power plan (cmd 112, actionType 1)."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3015,
            ble_msg_type=112,
            body=task.to_body(SCHEDULE_ACTION_ADD),
        )

    def delete_schedule_task(self, task: ScheduleTask) -> JackeryBleCommand:
        """Delete a custom-mode power plan (cmd 112, actionType 2)."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3016,
            ble_msg_type=112,
            body=task.to_body(SCHEDULE_ACTION_DELETE),
        )

    def update_schedule_task(self, task: ScheduleTask) -> JackeryBleCommand:
        """Rewrite an existing plan, e.g. to move its power cap (actionType 3)."""
        self._require_home()
        return JackeryBleCommand(
            action_id=3017,
            ble_msg_type=112,
            body=task.to_body(SCHEDULE_ACTION_UPDATE),
        )


class JackeryBleAccessoryPacketHeader(NamedTuple):
    """Jackery Ble Accessory Packet Header fields (with defaults)."""

    packet_type: bytes = b"\xdf\xed"
    page: int = 1
    count: int = 1
    action_id: int = 0
    ble_msg_type: int = 0
    length: int = 0


class JackeryBlePacketHeader(NamedTuple):
    """Jackery Ble Packet Header fields (with defaults)."""

    packet_type: bytes = b"\xdf\xed"
    sep1: int = 1  # unknown int field (always 1 outbound, 100 inbound)
    page: int = 1
    count: int = 1
    action_id: int = 0
    ble_msg_type: int = 0
    code: int = 1
    length: int = 0


@dataclass
class JackeryBleCommand:
    """Jackery BLE command."""

    action_id: int
    ble_msg_type: int
    body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Add cmd to the body when the message type carries one."""
        if self.ble_msg_type > 0:
            self.body.setdefault("cmd", self.ble_msg_type)


@dataclass
class JackeryBlePacket:
    """Represent a Jackery BLE Packet."""

    header: JackeryBleAccessoryPacketHeader | JackeryBlePacketHeader
    body: bytes = b""


@dataclass
class JackeryBlePacketBuilder:
    """Jackery Ble Packet Builder (bytes -> JackeryBlePacket and JackeryBlePacket -> bytes)."""

    header_cls: type[JackeryBleAccessoryPacketHeader | JackeryBlePacketHeader]
    header_struct: Struct

    @classmethod
    def from_device_data(cls, device_data: JackeryDeviceData) -> JackeryBlePacketBuilder:
        """Return a builder based on the device model type."""
        if device_data.model_type is JackeryDeviceModelType.ACCESSORY:
            return cls(JackeryBleAccessoryPacketHeader, Struct(">2shhhhh"))
        return cls(JackeryBlePacketHeader, Struct(">2shhhhhhh"))

    @classmethod
    def home(cls) -> JackeryBlePacketBuilder:
        """Return the 16-byte home frame builder."""
        return cls(JackeryBlePacketHeader, Struct(">2shhhhhhh"))

    @classmethod
    def accessory(cls) -> JackeryBlePacketBuilder:
        """Return the 12-byte accessory frame builder."""
        return cls(JackeryBleAccessoryPacketHeader, Struct(">2shhhhh"))

    def command_to_packets(
        self, command: JackeryBleCommand, chunk_size: int
    ) -> list[JackeryBlePacket]:
        """Transforms a jackery ble command into a list of jackery ble packets."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")

        data = dumps(command.body, separators=(",", ":")).encode("utf-8")
        if not data:
            return []

        count = (len(data) + chunk_size - 1) // chunk_size
        return [
            JackeryBlePacket(
                header=self.header_cls(
                    page=page + 1,
                    count=count,
                    action_id=command.action_id,
                    ble_msg_type=command.ble_msg_type,
                    length=len(data[data_index : data_index + chunk_size]),
                ),
                body=data[data_index : data_index + chunk_size],
            )
            for page, data_index in enumerate(range(0, len(data), chunk_size))
        ]

    def to_packet(self, data: bytes) -> JackeryBlePacket:
        """Transforms bytes into a JackeryBlePacket."""
        header_size = self.header_struct.size
        if len(data) < header_size:
            raise ValueError(
                f"Frame too short for {header_size}-byte header: {len(data)}"
            )
        header = self.header_cls._make(self.header_struct.unpack(data[:header_size]))
        body = data[header_size:]
        return JackeryBlePacket(header, body)

    def to_bytes(self, packet: JackeryBlePacket) -> bytes:
        """Transforms a JackeryBlePacket into bytes."""
        return self.header_struct.pack(*packet.header) + packet.body


class JackeryBlePacketGroup:
    """Jackery BLE packet group. Holds packets until we get all of them."""

    action_id: int
    _count: int
    _packets: dict[int, JackeryBlePacket]

    def __init__(self, first_packet: JackeryBlePacket) -> None:
        """First packet is given to initialize the packet group."""
        self.action_id = first_packet.header.action_id
        self._count = first_packet.header.count
        self._packets = {}
        self.add_packet(first_packet)

    def add_packet(self, packet: JackeryBlePacket) -> None:
        """Add the next packet."""
        if packet.header.action_id != self.action_id:
            return
        if not 1 <= packet.header.page <= self._count:
            return

        self._packets[packet.header.page] = packet

    @property
    def count(self) -> int:
        """Number of packets in the group."""
        return self._count

    @property
    def complete(self) -> bool:
        """Whether the packet group is complete."""
        return self._count > 0 and len(self._packets) == self._count

    @property
    def raw_body(self) -> bytes:
        """Packet group body (bytes).

        Prefer the header length so the per-frame random suffix is removed
        precisely. Fall back to the historical "strip two bytes" behavior for
        devices that do not populate ``length``.
        """
        result = bytearray()

        for _, packet in sorted(self._packets.items()):
            body = packet.body
            length = getattr(packet.header, "length", 0)
            if 0 < length <= len(body):
                result += body[:length]
            elif len(body) >= 2:
                result += body[:-2]
            else:
                result += body

        return bytes(result)

    @property
    def body(self) -> dict[str, Any] | None:
        """Packet group body (JSON). None if incomplete."""
        if not self.complete:
            return None

        return loads(self.raw_body)
