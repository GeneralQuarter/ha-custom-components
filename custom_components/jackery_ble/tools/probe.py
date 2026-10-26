#!/usr/bin/env python3
"""Standalone BLE probe for Jackery SolarVault / Smart Meter.

Connects through an ESPHome Bluetooth proxy (``bleak-esphome``), scans for
Jackery advertisements, lets you pick a device, prompts for the Bluetooth key
(hidden), and logs raw + decrypted frames.

Examples
--------
Scan and read the CT meter::

    python jackery_ble/tools/probe.py --proxy 192.0.2.20

Read a device by name from devices.json (address, key and frame format)::

    python jackery_ble/tools/probe.py --proxy 192.0.2.20 --device vault
    python jackery_ble/tools/probe.py --proxy 192.0.2.20 --device ct

Check the config without touching a device (keys are masked)::

    python jackery_ble/tools/probe.py --list-devices

Send the local CT bind command to the SolarVault::

    python jackery_ble/tools/probe.py --proxy 192.0.2.20 \
        --device vault --action 3012 --cmd 108 --body '{"sn":"JA..."}'

Bind a CT and watch ``ctStat`` (optionally enable follow-meter afterwards)::

    python jackery_ble/tools/probe.py --proxy 192.0.2.20 \
        --device vault --bind-sn JA... --follow-meter

The Bluetooth key is read from devices.json when present; it is never written
to the log.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import getpass
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Make the integration package importable when this file is run directly.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jackery_ble.const import (
    CHARACTERISTIC_DATA_NOTIFY,
    CHARACTERISTIC_DATA_WRITE,
    SERVICE_UUID,
)
from jackery_ble.jackery_ble_protocol import (
    JackeryBleCommand,
    JackeryBleCommands,
    JackeryBlePacketBuilder,
    JackeryBlePacketGroup,
)
from jackery_ble.jackery_device_data import (
    JackeryDeviceCategory,
    JackeryDeviceData,
)

try:
    import bleak
    import habluetooth
    from bleak_esphome import APIConnectionManager
except ImportError as exc:  # pragma: no cover - friendly message
    sys.exit(
        "Missing probe dependencies. Install them with:\n"
        "  python -m pip install -r jackery_ble/tools/requirements-probe.txt\n"
        f"\nOriginal error: {exc}"
    )


LOGGER = logging.getLogger("jackery.probe")

HOME_BUILDER = JackeryBlePacketBuilder.home()
ACCESSORY_BUILDER = JackeryBlePacketBuilder.accessory()

# Timer/schedule family from HomeCmdAction. All four share cmd=112; the
# action id selects the operation, and actionType repeats it in the body.
_SCHEDULE_ACTIONS: dict[str, tuple[int, int]] = {
    "add": (3015, 1),
    "delete": (3016, 2),
    "update": (3017, 3),
    "read": (3018, 4),
}


@dataclass
class FoundDevice:
    """A discovered Jackery device."""

    address: str
    name: str
    rssi: int | None
    device: Any
    info: JackeryDeviceData
    adv: Any
    is_jackery: bool = False


class _AdvShim:
    """The tiny subset of BluetoothServiceInfo used by JackeryDeviceData."""

    def __init__(self, adv: Any) -> None:
        self.manufacturer_data = getattr(adv, "manufacturer_data", None) or {}
        self.service_data = getattr(adv, "service_data", None) or {}


def _is_jackery_adv(adv: Any) -> bool:
    uuids = {str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])}
    if SERVICE_UUID.lower() in uuids:
        return True

    for company_id in (getattr(adv, "manufacturer_data", None) or {}):
        high = (company_id >> 8) & 0xFF
        low = company_id & 0xFF
        if high == 0x48 and low in (2, 9):
            return True

    name = str(getattr(adv, "local_name", "") or "").lower()
    return any(token in name for token in ("jackery", "solarvault", "smart meter"))


def _decode_key(raw: str) -> bytes:
    """Decode a base64 (or hex) Bluetooth key and validate its AES length."""
    value = raw.strip().strip('"').strip("'")
    if not value:
        raise ValueError("empty key")

    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) in (32, 48, 64):
        key = bytes.fromhex(value)
    else:
        padded = value + "=" * (-len(value) % 4)
        try:
            key = base64.b64decode(padded, validate=True)
        except binascii.Error:
            try:
                key = base64.urlsafe_b64decode(padded)
            except binascii.Error as exc:
                raise ValueError(f"not valid base64: {exc}") from exc

    if len(key) not in (16, 24, 32):
        raise ValueError(
            f"decoded key is {len(key)} bytes; expected 16, 24 or 32 bytes"
        )

    return key


# --------------------------------------------------------------- config file
#
# Devices and their Bluetooth keys live outside the code so a run does not
# need the key on the command line (where it would land in shell history) or
# on stdin every time. The file is never logged; only the path is.

CONFIG_FILENAME = "devices.json"

_CONFIG_TEMPLATE = {
    "devices": {
        "vault": {
            "address": "AA:BB:CC:DD:EE:01",
            "key": "",
            "format": "home",
        },
        "ct": {
            "address": "AA:BB:CC:DD:EE:02",
            "key": "",
            "format": "accessory",
        },
    }
}


def default_config_path() -> Path:
    return Path(__file__).with_name(CONFIG_FILENAME)


def load_config(path: Path) -> dict[str, dict[str, Any]]:
    """Load ``devices.json``; create a template when it does not exist yet."""
    if not path.exists():
        try:
            path.write_text(
                json.dumps(_CONFIG_TEMPLATE, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            LOGGER.warning("Could not create %s (%s); keys must be entered.", path, exc)
            return {}
        LOGGER.info(
            "Created %s with the known addresses. Put each Bluetooth key in it "
            "to stop being prompted.",
            path,
        )
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read {path}: {exc}") from exc

    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict):
        raise SystemExit(f'{path}: expected {{"devices": {{"name": {{...}}}}}}')

    entries: dict[str, dict[str, Any]] = {}
    for name, entry in devices.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: device '{name}' must be an object")
        address = str(entry.get("address", "")).strip()
        if not address:
            raise SystemExit(f"{path}: device '{name}' has no address")
        entries[str(name)] = {
            "address": address,
            "key": str(entry.get("key", "")).strip(),
            "format": str(entry.get("format", "")).strip() or "auto",
        }
    return entries


def key_for_address(
    entries: dict[str, dict[str, Any]], address: str | None
) -> str | None:
    """Return the configured key for this address, if any."""
    if not address:
        return None
    wanted = address.strip().upper()
    for entry in entries.values():
        if entry["address"].upper() == wanted and entry["key"]:
            return entry["key"]
    return None


def print_config(entries: dict[str, dict[str, Any]], path: Path) -> None:
    """List devices without ever printing a key."""
    print(f"Config: {path}")
    if not entries:
        print("  (no devices)")
        return
    for name, entry in entries.items():
        state = (
            f"key set ({len(entry['key'])} chars)" if entry["key"] else "NO KEY"
        )
        print(f"  {name:12} {entry['address']:18} {entry['format']:10} {state}")


def _shape(value: Any) -> str:
    """Human-readable type/shape used in the field summary."""
    if isinstance(value, dict):
        return f"object[{len(value)}]"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    return type(value).__name__


def _walk_fields(value: Any, prefix: str, out: dict[str, dict[str, str]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.setdefault(path, {}).setdefault("type", _shape(child))
            _walk_fields(child, path, out)
    elif isinstance(value, list):
        for index, child in enumerate(value[:4]):
            _walk_fields(child, f"{prefix}[{index}]", out)


def _mode_summary(adv: Any) -> str:
    parts: list[str] = []
    for company_id, payload in (getattr(adv, "manufacturer_data", None) or {}).items():
        parts.append(f"mfr 0x{company_id:04x}={payload.hex()}")
    for uuid, payload in (getattr(adv, "service_data", None) or {}).items():
        parts.append(f"svc {uuid}={payload.hex()}")
    return ", ".join(parts) if parts else "(no mfr/service data)"


def setup_logging(args: argparse.Namespace) -> Path:
    log_dir = Path(args.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"probe-{datetime.now():%Y%m%d-%H%M%S}.log"

    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    LOGGER.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    LOGGER.addHandler(file_handler)

    # Keep the third-party BLE stack quiet unless --verbose is used.
    for name in ("bleak", "bleak_esphome", "habluetooth", "aioesphomeapi"):
        logging.getLogger(name).setLevel(
            logging.DEBUG if args.verbose else logging.WARNING
        )

    return log_path


async def scan_devices(timeout: float) -> list[FoundDevice]:
    LOGGER.info("Scanning for BLE advertisements (%.0fs)...", timeout)
    discovered = await bleak.BleakScanner.discover(return_adv=True, timeout=timeout)

    found: list[FoundDevice] = []
    for address, (device, adv) in discovered.items():
        is_jackery = _is_jackery_adv(adv)

        info = JackeryDeviceData()
        try:
            info.update(_AdvShim(adv))
        except Exception:
            LOGGER.debug("Could not parse advertisement for %s", address, exc_info=True)

        name = (
            getattr(adv, "local_name", None)
            or getattr(device, "name", None)
            or info.name
            or "(unnamed)"
        )
        found.append(
            FoundDevice(
                address=address,
                name=name,
                rssi=getattr(adv, "rssi", None),
                device=device,
                info=info,
                adv=adv,
                is_jackery=is_jackery,
            )
        )

    found.sort(
        key=lambda item: (
            not item.is_jackery,
            -(item.rssi if item.rssi is not None else -999),
        )
    )
    return found


def _say(message: str = "") -> None:
    """Print to the console and record the same line in the log file."""
    print(message)
    if message:
        LOGGER.info(message)


def print_devices(found: list[FoundDevice]) -> None:
    if not found:
        _say("\nNo BLE devices found.\n")
        _say("Checks:")
        _say("  - Is the ESPHome proxy online and active: true?")
        _say("  - Are HA's Bluetooth connections stopped (proxy slots free)?")
        _say("  - Try --scan-timeout 30 and move the proxy closer.\n")
        return

    _say(f"\nFound {len(found)} BLE device(s):\n")
    for index, item in enumerate(found, 1):
        info = item.info
        model = info.model_data.name if info.model_data else "(unknown model)"
        tag = "Jackery" if item.is_jackery else "other  "
        _say(f"  [{index}] [{tag}] {item.address}  RSSI {item.rssi}  {item.name}")
        _say(f"      SN: {info.sn or '(not parsed)'}")
        _say(
            f"      category: {info.category.name}  "
            f"model: {info.model_code or '?'}  {model}"
        )
        if item.adv is not None:
            _say(f"      {_mode_summary(item.adv)}")
        _say()


def choose_device(
    found: list[FoundDevice], address: str | None, include_all: bool
) -> FoundDevice:
    if address:
        for item in found:
            if item.address.lower() == address.lower():
                return item
        LOGGER.warning(
            "Address %s was not in the scan; trying it anyway (format may need --format)",
            address,
        )
        return FoundDevice(
            address=address,
            name="(not in scan)",
            rssi=None,
            device=None,
            info=JackeryDeviceData(),
            adv=None,
        )

    candidates = found if include_all else [item for item in found if item.is_jackery]
    if not candidates and found:
        print(
            "\nNo advertisement matched the Jackery signature; "
            "showing every device so you can pick the meter/vault by name.\n"
        )
        candidates = found
    if not candidates:
        raise SystemExit("No device selected (nothing found).")

    print_devices(candidates)
    while True:
        raw = input(f"Select device [1-{len(candidates)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        print("Invalid selection.")


class Probe:
    """One connected device and its logging/parsing state."""

    def __init__(self, args: argparse.Namespace, selected: FoundDevice, key: bytes):
        self.args = args
        self.selected = selected
        self.info = selected.info
        self.info.set_encryption_key(key)

        self.client: bleak.BleakClient | None = None
        self.notify_char: Any = None
        self.write_char: Any = None
        self.primary = self._choose_primary()
        self.groups: dict[int, JackeryBlePacketGroup] = {}
        self.response_event = asyncio.Event()
        self.fields: dict[str, dict[str, str]] = {}
        self.raw_notifications = 0
        self.last_body: dict[str, Any] | None = None
        self._expected_action: int | None = None
        self._answer_body: dict[str, Any] | None = None

    def _choose_primary(self) -> JackeryBlePacketBuilder:
        if self.args.format == "accessory":
            return ACCESSORY_BUILDER
        if self.args.format == "home":
            return HOME_BUILDER
        if (
            self.info.is_accessory
            or self.info.category is JackeryDeviceCategory.JACKERY_ACCESSORY
        ):
            return ACCESSORY_BUILDER
        return HOME_BUILDER

    def _candidates(self) -> list[JackeryBlePacketBuilder]:
        alternate = HOME_BUILDER if self.primary is ACCESSORY_BUILDER else ACCESSORY_BUILDER
        return [self.primary, alternate]

    # ------------------------------------------------------------------ I/O
    async def connect(self) -> None:
        device = self.selected.device
        if device is None:
            LOGGER.info("Looking up %s...", self.selected.address)
            device = await bleak.BleakScanner.find_device_by_address(
                self.selected.address, timeout=20
            )
        if device is None:
            raise RuntimeError(
                f"{self.selected.address} not found. Is it advertising and in range?"
            )

        LOGGER.info("Connecting to %s (%s)...", self.selected.address, self.selected.name)
        self.client = bleak.BleakClient(device, timeout=30)
        await self.client.connect()

        mtu = getattr(self.client, "mtu_size", None) or 247
        self.chunk_size = self.args.chunk_size or max(20, int(mtu) - 60)
        LOGGER.info("Connected. MTU=%s chunk_size=%s", mtu, self.chunk_size)

        services = self.client.services
        notify = services.get_characteristic(CHARACTERISTIC_DATA_NOTIFY)
        write = services.get_characteristic(CHARACTERISTIC_DATA_WRITE)
        if notify is None or write is None:
            LOGGER.warning(
                "Expected Jackery characteristics not found; dumping GATT and "
                "auto-selecting notify/write characteristics"
            )
            for service in services:
                LOGGER.warning("service %s", service.uuid)
                for char in service.characteristics:
                    LOGGER.warning("  char %s %s", char.uuid, char.properties)
            if notify is None:
                notify = next(
                    (
                        char
                        for service in services
                        for char in service.characteristics
                        if "notify" in char.properties
                    ),
                    None,
                )
            if write is None:
                write = next(
                    (
                        char
                        for service in services
                        for char in service.characteristics
                        if "write" in char.properties
                        or "write-without-response" in char.properties
                    ),
                    None,
                )
        if notify is None or write is None:
            raise RuntimeError("No usable Jackery notify/write characteristics found")

        self.notify_char = notify
        self.write_char = write
        await self.client.start_notify(notify, self._on_notify)
        LOGGER.info("Subscribed to notify %s (write %s)", notify.uuid, write.uuid)

        if self.args.time_sync:
            command = JackeryBleCommands(self.info).send_time_zone()
            LOGGER.info("Sending time zone sync (action=%s cmd=%s)", command.action_id, command.ble_msg_type)
            await self.send(
                self.primary,
                command.action_id,
                command.ble_msg_type,
                command.body,
            )
            await asyncio.sleep(1.0)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                if self.notify_char is not None:
                    await self.client.stop_notify(self.notify_char)
            except Exception:
                LOGGER.debug("stop_notify failed", exc_info=True)
            await self.client.disconnect()

    async def _write(self, payload: bytes) -> None:
        assert self.client is not None
        characteristic = self.write_char or CHARACTERISTIC_DATA_WRITE
        try:
            await self.client.write_gatt_char(
                characteristic, payload, response=False
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("write without response failed (%s); retrying with response", exc)
            await self.client.write_gatt_char(
                characteristic, payload, response=True
            )

    async def send(
        self,
        builder: JackeryBlePacketBuilder,
        action_id: int,
        cmd: int,
        body: dict[str, Any] | None = None,
    ) -> list[Any]:
        command = JackeryBleCommand(
            action_id=action_id, ble_msg_type=cmd, body=dict(body or {})
        )
        packets = builder.command_to_packets(command, self.chunk_size)
        LOGGER.info(
            ">>> action=%s cmd=%s body=%s (%d packet(s), %s header)",
            action_id,
            cmd,
            json.dumps(command.body, separators=(",", ":")),
            len(packets),
            "accessory" if builder is ACCESSORY_BUILDER else "home",
        )
        for packet in packets:
            plain = builder.to_bytes(packet)
            encrypted = self.info.encrypt(plain)
            payload = self._encode_wire(encrypted)
            LOGGER.debug("TX %d bytes: %s", len(payload), payload.hex())
            await self._write(payload)
            await asyncio.sleep(0.02)
        return packets

    def _encode_wire(self, encrypted: bytes) -> bytes:
        if self.args.wire == "base64":
            return base64.b64encode(encrypted)
        return encrypted

    # -------------------------------------------------------------- receive
    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        raw = bytes(data)
        self.raw_notifications += 1
        LOGGER.debug("NOTIFY %d bytes: %s", len(raw), raw.hex())

        plain = self._decode_notification(raw)
        if plain is None:
            LOGGER.warning(
                "Notification %d bytes could not be decrypted/parsed: %s",
                len(raw),
                raw.hex(),
            )
            return

        parsed = self._parse_plain(plain)
        if parsed is None:
            LOGGER.warning("Decrypted frame has no plausible header: %s", plain.hex())
            return

        builder, packet = parsed
        self._handle_packet(builder, packet)

    def _decode_notification(self, raw: bytes) -> bytes | None:
        # Normal case: IV || AES-CBC ciphertext.
        plain = self.info.decrypt(raw)
        if plain is not None:
            if plain.startswith(b"\xdf\xed"):
                return plain
            if plain.startswith(b"DFED"):
                LOGGER.warning(
                    "Decrypted an ASCII-hex frame. This firmware uses the older "
                    "text framing; the probe currently writes the binary framing."
                )
                return plain

        # Older app captures carry the encrypted blob as ASCII hex/base64.
        text = raw.decode("ascii", errors="ignore").strip()
        if text:
            candidates: list[bytes] = []
            if len(text) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", text):
                try:
                    candidates.append(bytes.fromhex(text))
                except ValueError:
                    pass
            padded = text + "=" * (-len(text) % 4)
            try:
                candidates.append(base64.b64decode(padded, validate=True))
            except binascii.Error:
                pass

            for encrypted in candidates:
                plain = self.info.decrypt(encrypted)
                if plain is not None and plain.startswith((b"\xdf\xed", b"DFED")):
                    return plain

        # Last resort: maybe the frame is not encrypted at all.
        if raw.startswith(b"\xdf\xed"):
            LOGGER.warning("Falling back to unencrypted frame")
            return raw

        return None

    def _parse_plain(self, plain: bytes) -> tuple[JackeryBlePacketBuilder, Any] | None:
        best: tuple[int, JackeryBlePacketBuilder, Any] | None = None

        for builder in self._candidates():
            try:
                packet = builder.to_packet(plain)
            except Exception:  # noqa: BLE001, S112 - the other header shape may be right
                continue

            score = self._score(builder, packet)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, builder, packet)

        if best is None:
            return None
        return best[1], best[2]

    def _score(self, builder: JackeryBlePacketBuilder, packet: Any) -> int:
        header = packet.header
        body = packet.body

        if header.packet_type != b"\xdf\xed":
            return 0
        if not 1 <= header.page <= 64:
            return 0
        if not 1 <= header.count <= 64:
            return 0
        if header.page > header.count:
            return 0
        if not 0 <= header.action_id <= 0xFFFF:
            return 0
        if not 0 <= header.length <= len(body):
            return 0

        # The 16-byte home header has a version field. Outbound is 1, inbound
        # observed as 100. Anything else means this is the wrong header shape.
        if hasattr(header, "sep1") and header.sep1 not in (1, 100):
            return 0
        if hasattr(header, "code") and header.code not in (0, 1):
            return 0

        score = 2
        if 0 < header.length <= len(body):
            score += 2

        candidate_body = self._body_bytes(packet)
        try:
            json.loads(candidate_body)
        except ValueError:
            pass
        else:
            score += 5

        return score

    @staticmethod
    def _body_bytes(packet: Any) -> bytes:
        length = getattr(packet.header, "length", 0)
        if 0 < length <= len(packet.body):
            return packet.body[:length]
        if len(packet.body) >= 2:
            return packet.body[:-2]
        return packet.body

    def _handle_packet(
        self, builder: JackeryBlePacketBuilder, packet: Any
    ) -> None:
        header = packet.header
        LOGGER.debug(
            "RX header: page=%s/%s action=%s cmd=%s length=%s body=%d",
            header.page,
            header.count,
            header.action_id,
            header.ble_msg_type,
            header.length,
            len(packet.body),
        )

        if header.count == 1:
            self._emit(builder, JackeryBlePacketGroup(packet))
            return

        group = self.groups.get(header.action_id)
        if group is None or group.count != header.count:
            group = JackeryBlePacketGroup(packet)
            self.groups[header.action_id] = group
        else:
            group.add_packet(packet)

        if group.complete:
            self.groups.pop(header.action_id, None)
            self._emit(builder, group)

    def _emit(
        self, builder: JackeryBlePacketBuilder, group: JackeryBlePacketGroup
    ) -> None:
        raw = group.raw_body
        try:
            body = group.body
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "JSON decode failed for action %s: %s | raw=%s",
                group.action_id,
                exc,
                raw.hex(),
            )
            return

        if body is None:
            return

        LOGGER.info(
            "BODY action=%s cmd=%s (%s header, %d byte(s)):\n%s",
            group.action_id,
            body.get("cmd", "?"),
            "accessory" if builder is ACCESSORY_BUILDER else "home",
            len(raw),
            json.dumps(body, indent=2, ensure_ascii=False),
        )
        _walk_fields(body, "", self.fields)
        self.last_body = body

        # Only a frame carrying the action we asked for counts as an answer.
        # Both devices also emit unsolicited pushes with rolling action ids;
        # treating those as answers made unsupported commands look answered.
        if self._expected_action is None or group.action_id == self._expected_action:
            if self._expected_action is not None:
                self._answer_body = body
            self.response_event.set()
        else:
            LOGGER.debug(
                "Ignoring action=%s push while waiting for action=%s",
                group.action_id,
                self._expected_action,
            )

    # ---------------------------------------------------------------- probes
    async def _query(
        self,
        builder: JackeryBlePacketBuilder,
        action_id: int,
        cmd: int,
        wait: float | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        timeout = wait if wait is not None else self.args.response_timeout
        self._expected_action = action_id
        self._answer_body = None
        self.response_event.clear()
        await self.send(builder, action_id, cmd, body)
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout=timeout)
        except TimeoutError:
            LOGGER.warning(
                "<<< no reply to action=%s cmd=%s within %.1fs "
                "(unsolicited pushes are not counted)",
                action_id,
                cmd,
                timeout,
            )
            return None
        finally:
            self._expected_action = None

        LOGGER.info("<<< response received for action=%s cmd=%s", action_id, cmd)
        return self._answer_body or self.last_body

    async def run_queries(self) -> None:
        LOGGER.info("Waiting 2s for any unsolicited pushes...")
        await asyncio.sleep(2)

        looks_accessory = (
            self.info.is_accessory
            or self.info.category is JackeryDeviceCategory.JACKERY_ACCESSORY
        )
        accessory_mode = self.args.format == "accessory" or looks_accessory

        if accessory_mode:
            if self.args.format == "accessory":
                pairs = [
                    (ACCESSORY_BUILDER, 4005),
                    (ACCESSORY_BUILDER, 3011),
                    (HOME_BUILDER, 4005),
                    (HOME_BUILDER, 3011),
                ]
            elif self.args.format == "home":
                pairs = [(HOME_BUILDER, 4005), (HOME_BUILDER, 3011)]
            else:
                pairs = [
                    (ACCESSORY_BUILDER, 4005),
                    (ACCESSORY_BUILDER, 3011),
                    (HOME_BUILDER, 4005),
                    (HOME_BUILDER, 3011),
                ]

            for builder, action_id in pairs:
                got_one = await self._query(builder, action_id, 106)
                got_one = await self._query(builder, action_id, 120) or got_one
                if got_one:
                    break
                await asyncio.sleep(0.3)
        else:
            await self._query(HOME_BUILDER, 3011, 106)
            await self._query(HOME_BUILDER, 3019, 120)

        LOGGER.info(
            "Listening for unsolicited pushes for %.0fs...",
            self.args.listen,
        )
        await asyncio.sleep(self.args.listen)

    async def run_write(self) -> None:
        body: dict[str, Any]
        if self.args.body.startswith("@"):
            body = json.loads(Path(self.args.body[1:]).read_text(encoding="utf-8"))
        else:
            body = json.loads(self.args.body)

        verify_key = self.args.verify_key
        before: Any = None
        if verify_key:
            if not await self._query(HOME_BUILDER, 3019, 120):
                raise SystemExit(
                    "Cannot verify the write: the device did not answer 3019/120."
                )
            before = (self.last_body or {}).get(verify_key)
            LOGGER.info("verify: %s before = %s", verify_key, before)

        self._expected_action = self.args.action
        self.response_event.clear()
        await self.send(self.primary, self.args.action, self.args.cmd, body)
        try:
            await asyncio.wait_for(
                self.response_event.wait(), timeout=self.args.response_timeout
            )
        except TimeoutError:
            LOGGER.warning(
                "<<< no reply to action=%s within %.1fs "
                "(unsolicited pushes are not counted; write commands often "
                "do not answer at all)",
                self.args.action,
                self.args.response_timeout,
            )
        finally:
            self._expected_action = None

        if verify_key:
            await asyncio.sleep(2.0)
            if await self._query(HOME_BUILDER, 3019, 120):
                after = (self.last_body or {}).get(verify_key)
                if after == before:
                    LOGGER.warning(
                        "verify: %s unchanged (%s) - the write was not applied",
                        verify_key,
                        after,
                    )
                else:
                    LOGGER.info(
                        "verify: %s changed %s -> %s", verify_key, before, after
                    )

        LOGGER.info("Listening for %.0fs...", self.args.listen)
        await asyncio.sleep(self.args.listen)

    # Timer/schedule family from HomeCmdAction: same cmd=112, the action id
    # selects the operation. Bodies come from TaskBaseBean + CustomModeTask,
    # whose field names are serialised verbatim by the app.
    CUSTOM_MODE_TASK_TYPE = 2
    DISCHARGE_MODE = 2
    CHARGE_MODE = 1

    async def run_schedule(self) -> None:
        """Read or rewrite the vault's custom-mode power schedule."""
        operation = self.args.schedule
        action_id, action_type = _SCHEDULE_ACTIONS[operation]

        body: dict[str, Any] = {
            "actionType": action_type,
            "taskType": self.CUSTOM_MODE_TASK_TYPE,
        }

        if operation != "read":
            tid = self.args.schedule_tid or str(int(time.time()))
            body["tid"] = tid
            if operation in ("add", "update"):
                body.update(
                    {
                        "start": self.args.schedule_start,
                        "end": self.args.schedule_end,
                        "loops": self.args.schedule_loops,
                        "mode": (
                            self.CHARGE_MODE
                            if self.args.schedule_mode == "charge"
                            else self.DISCHARGE_MODE
                        ),
                        "pw": self.args.schedule_pw,
                        "sysSwitch": self.args.schedule_switch,
                    }
                )

        LOGGER.info(
            "=== Schedule %s: action=%s cmd=112 body=%s ===",
            operation,
            action_id,
            json.dumps(body, separators=(",", ":")),
        )
        answer = await self._query(HOME_BUILDER, action_id, 112, body=body)
        if answer is None:
            LOGGER.warning("No answer to the schedule %s command.", operation)
            return

        if operation == "read":
            self._log_schedule(answer)
            return

        # The device confirms the write; re-read the whole list to prove it
        # landed and to see the ids the firmware assigned.
        await asyncio.sleep(1.0)
        plans = await self._query(
            HOME_BUILDER,
            3018,
            112,
            body={"actionType": 4, "taskType": self.CUSTOM_MODE_TASK_TYPE},
        )
        if plans is None:
            LOGGER.warning("Schedule sent, but the read-back timed out.")
        else:
            self._log_schedule(plans)

        LOGGER.info("Listening for %.0fs...", self.args.listen)
        await asyncio.sleep(self.args.listen)

    def _log_schedule(self, body: dict[str, Any]) -> None:
        tasks = body.get("tasks")
        if not isinstance(tasks, list):
            LOGGER.info("Schedule body has no task list: %s", body)
            return
        LOGGER.info("Schedule holds %d plan(s):", len(tasks))
        for task in tasks:
            if not isinstance(task, dict):
                LOGGER.info("  %s", task)
                continue
            LOGGER.info(
                "  tid=%s %s-%s mode=%s pw=%s switch=%s loops=%s",
                task.get("tid"),
                task.get("start"),
                task.get("end"),
                task.get("mode"),
                task.get("pw"),
                task.get("sysSwitch"),
                task.get("loops"),
            )

    BIND_KEYS = (
        "ctStat",
        "ctState",
        "workModel",
        "isFollowMeterPw",
        "inGridSidePw",
        "outGridSidePw",
        "otherLoadPw",
        "soc",
        "maxOutPw",
        "maxFeedGrid",
        "defaultPw",
    )

    def _log_interesting(self, label: str) -> None:
        body = self.last_body or {}
        if not body:
            LOGGER.info("%s: (no decoded body)", label)
            return
        seen = [(key, body[key]) for key in self.BIND_KEYS if key in body]
        if not seen:
            LOGGER.info("%s: none of %s present", label, ", ".join(self.BIND_KEYS))
            return
        for key, value in seen:
            LOGGER.info("%s: %s = %s", label, key, value)

    async def run_bind(self) -> None:
        """Bind a CT to this vault and watch ``ctStat`` while it takes effect."""
        if self.info.is_accessory:
            raise SystemExit(
                "The bind command must be sent to the SolarVault, not to the CT."
            )

        sn = self.args.bind_sn.strip()
        commands = JackeryBleCommands(self.info)

        LOGGER.info("=== Baseline before binding %s ===", sn)
        await self._query(HOME_BUILDER, 3011, 106)
        await self._query(HOME_BUILDER, 3019, 120)
        baseline = (self.last_body or {}).get("ctStat")

        command = commands.bind_smart_accessory(sn)
        LOGGER.info(
            "=== Sending bind: action=%s cmd=%s body=%s ===",
            command.action_id,
            command.ble_msg_type,
            command.body,
        )
        await self.send(
            HOME_BUILDER, command.action_id, command.ble_msg_type, command.body
        )

        deadline = time.monotonic() + self.args.bind_wait
        current = baseline
        while time.monotonic() < deadline:
            await asyncio.sleep(2.0)
            if not await self._query(HOME_BUILDER, 3019, 120, wait=6.0):
                continue

            body = self.last_body or {}
            current = body.get("ctStat", current)
            LOGGER.info(
                "poll: ctStat=%s ctState=%s gridIn=%s gridOut=%s otherLoad=%s",
                body.get("ctStat"),
                body.get("ctState"),
                body.get("inGridSidePw"),
                body.get("outGridSidePw"),
                body.get("otherLoadPw"),
            )
            if current is not None and current != baseline:
                break

        if current is None:
            LOGGER.warning("No ctStat field seen; the bind result is unknown.")
        elif current == baseline:
            LOGGER.warning("ctStat did not change from %s.", baseline)
        else:
            LOGGER.info("ctStat changed: %s -> %s", baseline, current)

        if self.args.follow_meter:
            for command in (
                commands.set_work_model(4),
                commands.set_follow_meter(True),
            ):
                LOGGER.info(
                    "=== Sending action=%s cmd=%s body=%s ===",
                    command.action_id,
                    command.ble_msg_type,
                    command.body,
                )
                await self.send(
                    HOME_BUILDER,
                    command.action_id,
                    command.ble_msg_type,
                    command.body,
                )
                await asyncio.sleep(2.0)
            await self._query(HOME_BUILDER, 3019, 120)
            self._log_interesting("after follow-meter")

    def print_summary(self) -> None:
        LOGGER.info("Received %d notification(s).", self.raw_notifications)
        if not self.fields:
            LOGGER.info("No decoded fields.")
            return

        LOGGER.info("Fields seen (dotted paths):")
        for path in sorted(self.fields):
            LOGGER.info("  %-55s %s", path, self.fields[path].get("type", "?"))


async def async_main(args: argparse.Namespace) -> None:
    log_path = setup_logging(args)
    LOGGER.info("Log file: %s", log_path)

    config_path = Path(args.config).expanduser()
    entries = load_config(config_path)
    if args.list_devices:
        print_config(entries, config_path)
        return

    target: dict[str, Any] | None = None
    if args.device:
        target = entries.get(args.device)
        if target is None:
            known = ", ".join(sorted(entries)) or "none configured"
            raise SystemExit(f"Unknown --device '{args.device}' ({known}).")
    address = args.address or (target or {}).get("address")
    if args.format == "auto" and target and target["format"] != "auto":
        args.format = target["format"]

    proxy_key = args.proxy_key or None
    manager = APIConnectionManager(
        {"address": args.proxy, "noise_psk": proxy_key}
    )

    bt_manager = habluetooth.BluetoothManager()
    await bt_manager.async_setup()

    LOGGER.info("Connecting to ESPHome proxy %s:6053...", args.proxy)
    try:
        await asyncio.wait_for(manager.start(), timeout=args.proxy_timeout)
    except TimeoutError as exc:
        raise SystemExit(
            f"Timed out connecting to the ESPHome API at {args.proxy}:6053. "
            "Check the IP, the API encryption key, and that the proxy is online."
        ) from exc
    except Exception as exc:
        raise SystemExit(f"Could not connect to the ESPHome proxy: {exc}") from exc

    LOGGER.info("Proxy connected. Waiting for advertisements...")
    await asyncio.sleep(5)

    selected: FoundDevice | None = None
    try:
        found = await scan_devices(args.scan_timeout)
        if args.scan_only:
            candidates = (
                found
                if args.all
                else [item for item in found if item.is_jackery] or found
            )
            print_devices(candidates)
            return

        selected = choose_device(found, address, args.all)

        info = selected.info
        if not info.sn:
            LOGGER.warning("This device's SN could not be parsed from its advertisement.")

        if args.key:
            key_text = args.key
        else:
            configured = key_for_address(entries, selected.address)
            if configured:
                LOGGER.info(
                    "Using the key configured for %s from %s.",
                    selected.address,
                    config_path,
                )
                key_text = configured
            else:
                key_text = getpass.getpass("Bluetooth key (base64, hidden): ")

        try:
            key = _decode_key(key_text)
        except ValueError as exc:
            raise SystemExit(f"Invalid Bluetooth key: {exc}") from exc

        LOGGER.info(
            "Key accepted: %d bytes (AES-%d). Not written to the log.",
            len(key),
            len(key) * 8,
        )

        probe = Probe(args, selected, key)
        await probe.connect()
        try:
            if args.bind_sn:
                await probe.run_bind()
            elif args.schedule:
                await probe.run_schedule()
            elif args.action is not None:
                await probe.run_write()
            else:
                await probe.run_queries()
            probe.print_summary()
        finally:
            await probe.disconnect()
    finally:
        try:
            await manager.stop()
        except Exception:
            LOGGER.debug("Error while stopping the ESPHome connection", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe Jackery BLE devices through an ESPHome Bluetooth proxy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--proxy",
        default="192.0.2.20",
        help="ESPHome proxy host/IP (default: 192.0.2.20)",
    )
    parser.add_argument(
        "--proxy-key",
        default="",
        help="ESPHome API noise_psk (base64). Omit if encryption is disabled.",
    )
    parser.add_argument(
        "--proxy-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the ESPHome API connection (default: 30)",
    )
    parser.add_argument(
        "--address",
        help="BLE address to use directly (skips the interactive selection)",
    )
    parser.add_argument(
        "--device",
        help=(
            "Device name from the config file (e.g. vault, ct); sets the "
            "address, key and frame format"
        ),
    )
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help=(
            "Path to the device/key config (default: devices.json next to "
            "this script)"
        ),
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print the configured device names and whether a key is set, then exit",
    )
    parser.add_argument(
        "--key",
        help="Bluetooth key (base64/hex). If omitted, you are prompted (hidden).",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "home", "accessory"),
        default="auto",
        help="Frame header to use (default: auto from the advertisement)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=0,
        help="Plaintext bytes per packet (default: MTU - 60)",
    )
    parser.add_argument(
        "--wire",
        choices=("binary", "base64"),
        default="binary",
        help="GATT write encoding (default: binary; try base64 for older firmware)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show every BLE device, not only Jackery-tagged advertisements",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Scan and print devices, then exit (no key prompt, no connection)",
    )
    parser.add_argument(
        "--time-sync",
        action="store_true",
        help="Send the local time zone after connecting (some firmware expects it)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=15.0,
        help="Advertisement scan duration in seconds (default: 15)",
    )
    parser.add_argument(
        "--response-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for each query response (default: 8)",
    )
    parser.add_argument(
        "--listen",
        type=float,
        default=10.0,
        help="Seconds to listen for pushes after the queries (default: 10)",
    )
    parser.add_argument(
        "--action",
        type=int,
        help="Write mode: action id (e.g. 3012 for BindSmartAccessory)",
    )
    parser.add_argument(
        "--cmd",
        type=int,
        default=None,
        help="Write mode: ble message type (e.g. 108 for BindSmartAccessory)",
    )
    parser.add_argument(
        "--body",
        default="{}",
        help='Write mode: JSON body, or @path/to/body.json (default: "{}")',
    )
    parser.add_argument(
        "--bind-sn",
        help=(
            "Send 3012/108 to the SolarVault to bind this CT serial, then poll "
            "3019/120 until ctStat changes"
        ),
    )
    parser.add_argument(
        "--bind-wait",
        type=float,
        default=45.0,
        help="Seconds to poll for the bind result (default: 45)",
    )
    parser.add_argument(
        "--follow-meter",
        action="store_true",
        help="After --bind-sn: set workModel=4 and isFollowMeterPw=1",
    )
    parser.add_argument(
        "--verify-key",
        help=(
            "After a --action write: read 3019/120 before and after and report "
            "whether this body key changed (e.g. maxFeedGrid)"
        ),
    )
    parser.add_argument(
        "--schedule",
        choices=("read", "add", "update", "delete"),
        help=(
            "Custom-mode power schedule (3015/3016/3017/3018, cmd 112). "
            "'add' creates a plan, 'update' rewrites one by --schedule-tid, "
            "'delete' removes one, 'read' lists them"
        ),
    )
    parser.add_argument(
        "--schedule-pw",
        type=int,
        default=500,
        help="Plan power in W (discharge max = maxSysOutPw, 2500 on a 3 Pro Max)",
    )
    parser.add_argument(
        "--schedule-mode",
        choices=("charge", "discharge"),
        default="discharge",
        help="Plan direction (default: discharge)",
    )
    parser.add_argument(
        "--schedule-start",
        default="00:00",
        help='Plan start "HH:MM" in the vault clock (default: 00:00)',
    )
    parser.add_argument(
        "--schedule-end",
        default="23:59",
        help='Plan end "HH:MM" in the vault clock (default: 23:59)',
    )
    parser.add_argument(
        "--schedule-loops",
        default="1111111",
        help="Seven day flags, Monday first (default: 1111111 = every day)",
    )
    parser.add_argument(
        "--schedule-switch",
        type=int,
        choices=(0, 1),
        default=1,
        help="Plan enabled flag (default: 1)",
    )
    parser.add_argument(
        "--schedule-tid",
        default="",
        help="Plan id for update/delete (default: current epoch seconds)",
    )
    parser.add_argument(
        "--log-dir",
        default=str(Path(__file__).resolve().parent / "logs"),
        help="Directory for probe logs",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show debug output (including raw frames) on the console",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    has_target = bool(args.address or args.device)
    if args.action is not None and (not has_target or args.cmd is None):
        # Writes are explicit; do not silently target the wrong device or cmd.
        print("Write mode requires --address (or --device) and --cmd.", file=sys.stderr)
        raise SystemExit(2)
    if args.schedule and not has_target:
        print("Schedule mode requires --address (or --device).", file=sys.stderr)
        raise SystemExit(2)
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)


if __name__ == "__main__":
    main()
