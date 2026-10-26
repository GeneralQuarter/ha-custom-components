"""Config flow for the Hyxi DMU integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_HOST, CONF_PASSWORD, DEFAULT_PASSWORD, DOMAIN
from .coordinator import async_probe

_LOGGER = logging.getLogger(__name__)


class HyxiDmuConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the DMU address and verify it answers."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            password = str(user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD))
            try:
                await async_probe(host, password)
            except Exception:
                _LOGGER.debug("Could not probe %s", host, exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Hyxi DMU ({host})",
                    data={CONF_HOST: host, CONF_PASSWORD: password},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): vol.All(
                        str, vol.Length(min=1)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the DMU address or local password without re-adding it."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            password = str(user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD))
            try:
                await async_probe(host, password)
            except Exception:
                _LOGGER.debug("Could not probe %s", host, exc_info=True)
                errors["base"] = "cannot_connect"
            else:
                if host != entry.unique_id:
                    await self.async_set_unique_id(host, raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=host,
                    title=f"Hyxi DMU ({host})",
                    data_updates={CONF_HOST: host, CONF_PASSWORD: password},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=str(entry.data.get(CONF_HOST, ""))
                    ): str,
                    vol.Optional(
                        CONF_PASSWORD,
                        default=str(entry.data.get(CONF_PASSWORD, DEFAULT_PASSWORD)),
                    ): vol.All(str, vol.Length(min=1)),
                }
            ),
            errors=errors,
        )
