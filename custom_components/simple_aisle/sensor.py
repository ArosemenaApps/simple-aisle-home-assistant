"""Status sensors: changes waiting for a phone, paired devices, last sync."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SimpleAisleConfigEntry
from .const import DOMAIN, SIGNAL_UPDATED


def device_info(entry: SimpleAisleConfigEntry) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Simple Aisle", manufacturer="Simple Aisle",
                      entry_type=DeviceEntryType.SERVICE)


async def async_setup_entry(
    hass: HomeAssistant, entry: SimpleAisleConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([PendingSensor(entry), DevicesSensor(entry), LastSyncSensor(entry)])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: SimpleAisleConfigEntry, key: str) -> None:
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device_info(entry)

    @property
    def engine(self):
        return self.entry.runtime_data.engine

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()


class PendingSensor(_Base):
    _attr_icon = "mdi:cloud-upload-outline"

    def __init__(self, entry: SimpleAisleConfigEntry) -> None:
        super().__init__(entry, "pending_changes")

    @property
    def native_value(self) -> int:
        return self.engine.pending_count()


class DevicesSensor(_Base):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cellphone-link"

    def __init__(self, entry: SimpleAisleConfigEntry) -> None:
        super().__init__(entry, "paired_devices")

    @property
    def native_value(self) -> int:
        return len(self.engine.state["devices"])


class LastSyncSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: SimpleAisleConfigEntry) -> None:
        super().__init__(entry, "last_sync")

    @property
    def native_value(self) -> datetime | None:
        stamp = self.engine.state["stats"].get("last_sync")
        return datetime.fromtimestamp(stamp, UTC) if stamp else None
