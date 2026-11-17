"""Simple Aisle: two-way sync between Simple Aisle and native Home Assistant lists."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_WHEN_WAITING,
    CONF_PURCHASED_ACTION,
    DOMAIN,
    NOTIFY_MIN_INTERVAL,
    NOTIFY_WAIT_SECONDS,
    POLL_SECONDS,
    PURCHASED_COMPLETE,
    SIGNAL_UPDATED,
    STORAGE_VERSION,
)
from .engine import Engine, Settings, SyncError, empty_state
from .ha_backend import HATodoBackend

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_MOVE = "move_item"
SERVICE_REQUEST_SYNC = "request_sync"

MOVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("item"): cv.string,
        vol.Exclusive("target_entity_id", "target"): cv.entity_id,
        vol.Exclusive("target_store_id", "target"): cv.string,
    }
)


@dataclass
class RuntimeData:
    engine: Engine
    backend: HATodoBackend
    store: Store[dict[str, Any]]
    unsubs: list[Callable[[], None]] = field(default_factory=list)
    watchers: dict[str, Callable[[], None]] = field(default_factory=dict)
    pending_observe: dict[str, Callable[[], None]] = field(default_factory=dict)
    last_notified: float = 0.0


type SimpleAisleConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the HTTP API, websocket commands, services and panel once."""
    from . import http_api, panel, websocket_api  # noqa: PLC0415

    http_api.async_register(hass)
    websocket_api.async_register(hass)
    await panel.async_register(hass)
    _register_services(hass)
    return True


def settings_from(entry: ConfigEntry) -> Settings:
    return Settings(purchased_action=entry.options.get(CONF_PURCHASED_ACTION, PURCHASED_COMPLETE))


async def async_setup_entry(hass: HomeAssistant, entry: SimpleAisleConfigEntry) -> bool:
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}", private=True,
                                         atomic_writes=True)
    state = await store.async_load() or empty_state()
    backend = HATodoBackend(hass)

    async def save(data: dict[str, Any]) -> None:
        await store.async_save(data)

    runtime: RuntimeData

    @callback
    def changed() -> None:
        async_dispatcher_send(hass, SIGNAL_UPDATED)
        hass.async_create_task(_refresh_watchers(hass, runtime), eager_start=False)

    engine = Engine(state, backend, now=time.time, save=save, settings=settings_from(entry), on_change=changed)
    runtime = RuntimeData(engine=engine, backend=backend, store=store)
    entry.runtime_data = runtime

    await engine.recover()

    async def poll(_now: Any = None) -> None:
        await _refresh_watchers(hass, runtime)
        try:
            await engine.observe_all()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Reading linked lists failed")
        await _maybe_notify_waiting(hass, entry)

    runtime.unsubs.append(async_track_time_interval(hass, poll, timedelta(seconds=POLL_SECONDS)))
    runtime.unsubs.append(entry.add_update_listener(_options_updated))
    # Lists from other integrations may load after this one.
    runtime.unsubs.append(async_call_later(hass, 5, poll))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_updated(hass: HomeAssistant, entry: SimpleAisleConfigEntry) -> None:
    entry.runtime_data.engine.settings = settings_from(entry)
    async_dispatcher_send(hass, SIGNAL_UPDATED)


async def async_unload_entry(hass: HomeAssistant, entry: SimpleAisleConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = entry.runtime_data
    for unsub in [*runtime.unsubs, *runtime.watchers.values(), *runtime.pending_observe.values()]:
        unsub()
    runtime.unsubs.clear()
    runtime.watchers.clear()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Removing the integration deletes its own storage. Lists and their items stay."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}", private=True).async_remove()


async def _refresh_watchers(hass: HomeAssistant, runtime: RuntimeData) -> None:
    wanted = {link["entity_id"] for link in runtime.engine.state["links"].values()}
    for entity_id in list(runtime.watchers):
        if entity_id not in wanted:
            runtime.watchers.pop(entity_id)()
    for entity_id in wanted - set(runtime.watchers):

        @callback
        def on_update(entity_id: str = entity_id) -> None:
            _schedule_observe(hass, runtime, entity_id)

        unsub = runtime.backend.subscribe(entity_id, on_update)
        if unsub is not None:
            runtime.watchers[entity_id] = unsub


@callback
def _schedule_observe(hass: HomeAssistant, runtime: RuntimeData, entity_id: str) -> None:
    """Coalesce bursts of list updates into one read."""
    if (cancel := runtime.pending_observe.pop(entity_id, None)) is not None:
        cancel()

    async def run(_now: Any) -> None:
        runtime.pending_observe.pop(entity_id, None)
        try:
            await runtime.engine.observe(entity_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Reading %s failed", entity_id)

    runtime.pending_observe[entity_id] = async_call_later(hass, 0.5, run)


def loaded_entry(hass: HomeAssistant) -> SimpleAisleConfigEntry | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry
    return None


def require_entry(hass: HomeAssistant) -> SimpleAisleConfigEntry:
    entry = loaded_entry(hass)
    if entry is None:
        raise ServiceValidationError("Simple Aisle isn't set up.")
    return entry


# ----------------------------------------------------------------------
# Notifications ("Request sync")


def notify_targets(entry: ConfigEntry) -> list[str]:
    raw = entry.options.get(CONF_NOTIFY_SERVICE, "") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


async def send_sync_request(hass: HomeAssistant, entry: SimpleAisleConfigEntry) -> int:
    """Asks phones to open Simple Aisle. Returns how many notifications were sent."""
    engine = entry.runtime_data.engine
    pending = engine.pending_count()
    message = (
        f"{pending} change{'s' if pending != 1 else ''} from Home Assistant waiting. Open Simple Aisle to sync."
        if pending else "Open Simple Aisle to sync with Home Assistant."
    )
    sent = 0
    for target in notify_targets(entry):
        domain, _, service = target.partition(".")
        if domain != "notify" or not hass.services.has_service("notify", service):
            _LOGGER.warning("Simple Aisle notify target %s doesn't exist", target)
            continue
        await hass.services.async_call(
            "notify", service,
            {"title": "Simple Aisle", "message": message,
             "data": {"url": "simpleaisle://ha-sync", "tag": "simple-aisle-sync", "group": "simple-aisle"}},
            blocking=False,
        )
        sent += 1
    entry.runtime_data.last_notified = time.time()
    return sent


async def _maybe_notify_waiting(hass: HomeAssistant, entry: SimpleAisleConfigEntry) -> None:
    if not entry.options.get(CONF_NOTIFY_WHEN_WAITING):
        return
    age = entry.runtime_data.engine.oldest_pending_age()
    if age is None or age < NOTIFY_WAIT_SECONDS:
        return
    if time.time() - entry.runtime_data.last_notified < NOTIFY_MIN_INTERVAL:
        return
    await send_sync_request(hass, entry)


# ----------------------------------------------------------------------
# Services


def _register_services(hass: HomeAssistant) -> None:
    async def move(call: ServiceCall) -> ServiceResponse:
        entry = require_entry(hass)
        engine = entry.runtime_data.engine
        target_store = call.data.get("target_store_id")
        if target_entity := call.data.get("target_entity_id"):
            link = next((l for l in engine.state["links"].values() if l["entity_id"] == target_entity), None)
            if link is None:
                raise ServiceValidationError(f"{target_entity} isn't linked to a Simple Aisle store.")
            target_store = link["store_id"]
        if not target_store:
            raise ServiceValidationError("Choose a destination list or store.")
        try:
            return await engine.move_item(call.data[ATTR_ENTITY_ID], call.data["item"], target_store)
        except SyncError as err:
            raise ServiceValidationError(str(err)) from err

    async def request_sync(call: ServiceCall) -> ServiceResponse:
        entry = require_entry(hass)
        sent = await send_sync_request(hass, entry)
        return {"notifications_sent": sent, "pending": entry.runtime_data.engine.pending_count()}

    hass.services.async_register(DOMAIN, SERVICE_MOVE, move, schema=MOVE_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_REQUEST_SYNC, request_sync,
                                 supports_response=SupportsResponse.OPTIONAL)


async def create_local_todo(hass: HomeAssistant, name: str) -> str:
    """Creates a native Local To-do list through its own config flow; returns the entity id."""
    from homeassistant.config_entries import SOURCE_USER  # noqa: PLC0415
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    flow = await hass.config_entries.flow.async_init("local_todo", context={"source": SOURCE_USER})
    if flow.get("type") != "form":
        raise HomeAssistantError("Local To-do couldn't start creating a list")
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"todo_list_name": name})
    if result.get("type") != "create_entry":
        reason = result.get("reason", "unknown")
        raise HomeAssistantError(
            "A Local To-do list with that name already exists" if reason == "already_configured"
            else f"Local To-do couldn't create the list ({reason})"
        )
    created = result["result"]
    registry = er.async_get(hass)
    for _ in range(100):
        entities = er.async_entries_for_config_entry(registry, created.entry_id)
        if entities and hass.states.get(entities[0].entity_id) is not None:
            return entities[0].entity_id
        await _sleep(0.1)
    raise HomeAssistantError("The new list didn't appear in time")


async def _sleep(seconds: float) -> None:
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(seconds)
