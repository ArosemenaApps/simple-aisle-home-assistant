"""Native to-do lists through Home Assistant's supported todo services."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components.todo import DOMAIN as TODO_DOMAIN, TodoListEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from .const import COMPLETED, NEEDS_ACTION
from .engine import Item

_LOGGER = logging.getLogger(__name__)


class HATodoBackend:
    """Reads and writes native lists (Shopping List, Local To-do, any todo entity)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _features(self, entity_id: str) -> int:
        state = self.hass.states.get(entity_id)
        return int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)) if state else 0

    def supports_description(self, entity_id: str) -> bool:
        return bool(self._features(entity_id) & TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM)

    def is_writable(self, entity_id: str) -> bool:
        needed = (TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.UPDATE_TODO_ITEM
                  | TodoListEntityFeature.DELETE_TODO_ITEM)
        return self._features(entity_id) & needed == needed

    async def get_items(self, entity_id: str) -> list[Item] | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state == "unavailable":
            return None
        try:
            response = await self.hass.services.async_call(
                TODO_DOMAIN, "get_items", {ATTR_ENTITY_ID: entity_id, "status": [NEEDS_ACTION, COMPLETED]},
                blocking=True, return_response=True,
            )
        except (HomeAssistantError, ServiceNotFound) as err:
            _LOGGER.debug("Reading %s failed: %s", entity_id, err)
            return None
        raw = (response or {}).get(entity_id, {}).get("items")
        if raw is None:
            return None
        return [
            Item(uid=str(i["uid"]), summary=str(i.get("summary") or ""),
                 status=COMPLETED if i.get("status") == COMPLETED else NEEDS_ACTION,
                 description=i.get("description") or None)
            for i in raw if i.get("uid")
        ]

    async def add(self, entity_id: str, summary: str, description: str | None, known_uids: set[str]) -> str:
        before = await self.get_items(entity_id) or []
        before_uids = {i.uid for i in before} | known_uids
        data: dict[str, Any] = {ATTR_ENTITY_ID: entity_id, "item": summary}
        if description and self.supports_description(entity_id):
            data["description"] = description
        await self.hass.services.async_call(TODO_DOMAIN, "add_item", data, blocking=True)
        after = await self.get_items(entity_id) or []
        fresh = [i for i in after if i.uid not in before_uids]
        exact = [i for i in fresh if i.summary == summary]
        chosen = (exact or fresh)[-1:] if (exact or fresh) else []
        if not chosen:
            raise HomeAssistantError(f"Added “{summary}” to {entity_id} but couldn't find it again")
        return chosen[0].uid

    async def update(
        self, entity_id: str, uid: str, *, summary: str | None = None,
        description: str | None = None, status: str | None = None,
    ) -> None:
        data: dict[str, Any] = {ATTR_ENTITY_ID: entity_id, "item": uid}
        if summary is not None:
            data["rename"] = summary
        if status is not None:
            data["status"] = status
        if description is not None and self.supports_description(entity_id):
            data["description"] = description
        if len(data) == 2:
            return
        await self.hass.services.async_call(TODO_DOMAIN, "update_item", data, blocking=True)

    async def remove(self, entity_id: str, uids: list[str]) -> None:
        if not uids:
            return
        await self.hass.services.async_call(
            TODO_DOMAIN, "remove_item", {ATTR_ENTITY_ID: entity_id, "item": uids}, blocking=True
        )

    @callback
    def subscribe(self, entity_id: str, listener: Callable[[], None]) -> Callable[[], None] | None:
        """The entity's own item-update subscription (what the HA frontend uses)."""
        from homeassistant.components.todo.const import DATA_COMPONENT  # noqa: PLC0415

        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(entity_id) if component else None
        if entity is None:
            return None
        return entity.async_subscribe_updates(lambda _items: listener())

    def todo_lists(self) -> list[dict[str, Any]]:
        lists = []
        for state in self.hass.states.async_all(TODO_DOMAIN):
            lists.append({
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name") or state.entity_id,
                "writable": self.is_writable(state.entity_id),
                "descriptions": self.supports_description(state.entity_id),
            })
        return sorted(lists, key=lambda l: l["name"].casefold())
