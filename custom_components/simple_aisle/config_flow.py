"""Config and options flows for Simple Aisle."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_WHEN_WAITING,
    CONF_PURCHASED_ACTION,
    DOMAIN,
    PURCHASED_COMPLETE,
    PURCHASED_REMOVE,
)


class SimpleAisleConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per Home Assistant; it binds to a household on first pairing."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Simple Aisle", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SimpleAisleOptionsFlow()


class SimpleAisleOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        notify_services = sorted(f"notify.{name}" for name in self.hass.services.async_services_for_domain("notify"))
        schema = vol.Schema({
            vol.Required(CONF_PURCHASED_ACTION, default=options.get(CONF_PURCHASED_ACTION, PURCHASED_COMPLETE)):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[PURCHASED_COMPLETE, PURCHASED_REMOVE], translation_key=CONF_PURCHASED_ACTION,
                )),
            vol.Optional(CONF_NOTIFY_SERVICE, default=options.get(CONF_NOTIFY_SERVICE, "")):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=notify_services, multiple=False, custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )),
            vol.Required(CONF_NOTIFY_WHEN_WAITING, default=options.get(CONF_NOTIFY_WHEN_WAITING, False)): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
