"""Config flow for the Jackery BLE integration."""

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfo,
    async_discovered_service_info,
    async_process_advertisements,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_DEVICE_DATA,
    CONF_ENCRYPTION_KEY,
    CONF_RESERVE_PERCENT,
    CONF_SUPERVISOR_ENABLED,
    DEFAULT_SUPERVISOR_ENABLED,
    DOMAIN,
)
from .control import DEFAULT_RESERVE_PERCENT
from .jackery_device_data import JackeryDeviceData

ADDITIONAL_DISCOVERY_TIMEOUT = 60

# AES keys accepted by the devices; anything else cannot decrypt a frame.
VALID_KEY_LENGTHS = (16, 24, 32)

_LOGGER = logging.getLogger(__name__)


def decode_key(value: str) -> bytes | None:
    """Decode a pasted key, tolerating whitespace and missing padding.

    Returns None when the value is not base64 or not a valid AES key length.
    """
    cleaned = "".join(value.split())
    if not cleaned:
        return None

    for candidate in (cleaned, cleaned + "=" * (-len(cleaned) % 4)):
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) in VALID_KEY_LENGTHS:
            return decoded

    return None


@dataclass
class Discovery:
    """A discovered bluetooth device."""

    title: str
    discovery_info: BluetoothServiceInfo
    device: JackeryDeviceData


class JackeryBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jackery BLE."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry) -> OptionsFlow:
        """Return the options flow for this entry."""
        return JackeryBleOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfo | None = None
        self._discovered_device: JackeryDeviceData | None = None
        self._discovered_devices: dict[str, Discovery] = {}

    async def _async_wait_for_full_advertisement(
        self, discovery_info: BluetoothServiceInfo, device: JackeryDeviceData
    ) -> BluetoothServiceInfo:
        """Sometimes first advertisement we receive is blank or incomplete.

        Wait until we get a useful one.
        """
        if not device.is_missing_data():
            return discovery_info

        def _process_more_advertisements(
            service_info: BluetoothServiceInfo,
        ) -> bool:
            device.update(service_info)
            return not device.is_missing_data()

        return await async_process_advertisements(
            self.hass,
            _process_more_advertisements,
            {"address": discovery_info.address},
            BluetoothScanningMode.ACTIVE,
            ADDITIONAL_DISCOVERY_TIMEOUT,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = JackeryDeviceData()

        if not device.supported(discovery_info):
            _LOGGER.debug("Unsupported device: %s", discovery_info)
            return self.async_abort(reason="not_supported")

        self._discovered_device = device

        try:
            self._discovery_info = await self._async_wait_for_full_advertisement(
                discovery_info, device
            )
        except TimeoutError:
            return self.async_abort(reason="not_supported")

        if device.model_data is None:
            _LOGGER.debug("Unknown Jackery model code: %s", device.model_code)
            return self.async_abort(reason="not_supported")

        self.context["title_placeholders"] = {"name": device.name}

        return await self.async_step_encryption_key()

    async def async_step_encryption_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user provide the encryption key for the device while we wait for a public Jackery API."""
        assert self._discovered_device

        errors: dict[str, str] = {}

        if user_input is not None:
            key = decode_key(user_input[CONF_ENCRYPTION_KEY])
            if key is None:
                errors[CONF_ENCRYPTION_KEY] = "invalid_key"
            else:
                self._discovered_device.set_encryption_key(key)

                return self.async_create_entry(
                    title=self._discovered_device.name,
                    data={
                        CONF_DEVICE_DATA: self._discovered_device.to_config_entry_device_data()
                    },
                )

        return self.async_show_form(
            step_id="encryption_key",
            description_placeholders=self.context["title_placeholders"],
            data_schema=vol.Schema({vol.Required(CONF_ENCRYPTION_KEY): str}),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            discovery = self._discovered_devices[address]

            try:
                self._discovery_info = await self._async_wait_for_full_advertisement(
                    discovery.discovery_info, discovery.device
                )
            except TimeoutError:
                return self.async_abort(reason="not_supported")

            if discovery.device.model_data is None:
                _LOGGER.debug(
                    "Unknown Jackery model code: %s", discovery.device.model_code
                )
                return self.async_abort(reason="not_supported")

            self._discovered_device = discovery.device
            self.context["title_placeholders"] = {"name": discovery.device.name}

            return await self.async_step_encryption_key()

        current_addresses = self._async_current_ids(include_ignore=False)
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            device = JackeryDeviceData()
            if device.supported(discovery_info):
                self._discovered_devices[address] = Discovery(
                    title=device.name,
                    discovery_info=discovery_info,
                    device=device,
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        titles = {
            address: discovery.title
            for (address, discovery) in self._discovered_devices.items()
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(titles)}),
        )


class JackeryBleOptionsFlow(OptionsFlow):
    """Options for the follow-meter supervisor."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the supervisor options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SUPERVISOR_ENABLED,
                        default=options.get(
                            CONF_SUPERVISOR_ENABLED, DEFAULT_SUPERVISOR_ENABLED
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_RESERVE_PERCENT,
                        default=options.get(
                            CONF_RESERVE_PERCENT, DEFAULT_RESERVE_PERCENT
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
                }
            ),
        )
