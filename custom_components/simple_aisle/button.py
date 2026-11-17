"""Request sync: notifies phones (iOS doesn't let Home Assistant wake Simple Aisle)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SimpleAisleConfigEntry, send_sync_request
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: SimpleAisleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([RequestSyncButton(entry)])


class RequestSyncButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "request_sync"
    _attr_icon = "mdi:cellphone-arrow-down"

    def __init__(self, entry: SimpleAisleConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_request_sync"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        await send_sync_request(self.hass, self.entry)
