"""The device API (`/api/simple_aisle/v1/…`), authenticated by per-device tokens.

These views don't use Home Assistant user tokens. A device token reaches
these endpoints and nothing else in Home Assistant; wrong tokens go through
HA's own failed-login accounting (and IP ban, if the user enabled it).
"""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.ban import process_wrong_login
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import API_BASE, HEADER_TOKEN, PROTOCOL_VERSION, WAIT_MAX_SECONDS
from .engine import SyncError

_LOGGER = logging.getLogger(__name__)

MAX_BODY = 2_000_000


@callback
def async_register(hass: HomeAssistant) -> None:
    for view in (PairView, ConfigView, PreviewView, LinksView, LinkView, SyncView, WaitView, UnpairView):
        hass.http.register_view(view())


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


class _Base(HomeAssistantView):
    requires_auth = False
    cors_allowed = False

    def _entry(self, request: web.Request):
        from . import loaded_entry  # noqa: PLC0415

        return loaded_entry(request.app["hass"])

    async def _body(self, request: web.Request) -> dict[str, Any]:
        if request.content_length and request.content_length > MAX_BODY:
            raise SyncError("too_large", "Request too large", 413)
        try:
            body = await request.json()
        except ValueError as err:
            raise SyncError("bad_request", "Body must be JSON") from err
        if not isinstance(body, dict):
            raise SyncError("bad_request", "Body must be a JSON object")
        return body

    async def _device(self, request: web.Request):
        entry = self._entry(request)
        if entry is None:
            raise SyncError("not_set_up", "Simple Aisle isn't set up in Home Assistant.", 503)
        device_id = entry.runtime_data.engine.device_for_token(request.headers.get(HEADER_TOKEN))
        if device_id is None:
            await process_wrong_login(request)
            raise SyncError("revoked", "This device isn't paired with Home Assistant any more.", 401)
        return entry, device_id

    async def _run(self, request: web.Request, handler) -> web.Response:
        try:
            return await handler()
        except SyncError as err:
            return _error(err.code, str(err), err.status)
        except vol.Invalid as err:
            return _error("bad_request", str(err), 400)
        except HomeAssistantError as err:
            _LOGGER.warning("Simple Aisle request failed: %s", err)
            return _error("ha_error", str(err), 502)


class PairView(_Base):
    url = f"{API_BASE}/pair"
    name = "api:simple_aisle:pair"

    async def post(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            entry = self._entry(request)
            if entry is None:
                raise SyncError("not_set_up", "Simple Aisle isn't set up in Home Assistant.", 503)
            body = await self._body(request)
            engine = entry.runtime_data.engine
            try:
                result = await engine.pair(
                    str(body.get("code", "")),
                    device_name=str(body.get("device_name", "")),
                    household_id=str(body.get("household_id", "")),
                    household_name=body.get("household_name"),
                    app_version=body.get("app_version"),
                )
            except SyncError as err:
                if err.code == "invalid_code":
                    await process_wrong_login(request)
                raise
            result["instance_name"] = request.app["hass"].config.location_name
            result["protocol"] = PROTOCOL_VERSION
            return self.json(result)

        return await self._run(request, handle)


class ConfigView(_Base):
    url = f"{API_BASE}/config"
    name = "api:simple_aisle:config"

    async def get(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            entry, device_id = await self._device(request)
            runtime = entry.runtime_data
            engine = runtime.engine
            linked = {l["entity_id"]: l["store_id"] for l in engine.state["links"].values()}
            lists = [{**l, "linked_store_id": linked.get(l["entity_id"])} for l in runtime.backend.todo_lists()]
            devices = [
                {"device_id": d_id, "name": d["name"], "last_seen": d.get("last_seen"),
                 "created_at": d.get("created_at"), "this_device": d_id == device_id}
                for d_id, d in engine.state["devices"].items()
            ]
            return self.json({
                "protocol": PROTOCOL_VERSION,
                "instance_id": engine.state["instance_id"],
                "instance_name": request.app["hass"].config.location_name,
                "household_id": engine.state["household_id"],
                "household_name": engine.state.get("household_name"),
                "links": engine.public_links(),
                "lists": lists,
                "pending_ops": engine.pending_count(),
                "devices": devices,
                "settings": {"purchased_action": engine.settings.purchased_action},
                "next_seq": engine.state["next_seq"] - 1,
            })

        return await self._run(request, handle)


class PreviewView(_Base):
    url = f"{API_BASE}/links/preview"
    name = "api:simple_aisle:links:preview"

    async def post(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            entry, _ = await self._device(request)
            body = await self._body(request)
            entity_id = str(body.get("entity_id", ""))
            if not entity_id.startswith("todo."):
                raise SyncError("bad_request", "entity_id must be a todo entity")
            items = await entry.runtime_data.engine.preview(entity_id)
            return self.json({"items": [{"uid": i.uid, **i.snapshot()} for i in items]})

        return await self._run(request, handle)


class LinksView(_Base):
    url = f"{API_BASE}/links"
    name = "api:simple_aisle:links"

    async def post(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            from . import create_local_todo  # noqa: PLC0415

            entry, device_id = await self._device(request)
            body = await self._body(request)
            engine = entry.runtime_data.engine
            store_id = str(body.get("store_id", ""))
            store_name = str(body.get("store_name", "")).strip() or "Store"
            if not store_id:
                raise SyncError("bad_request", "store_id is required")
            entity_id = body.get("entity_id")
            if not entity_id:
                create_name = str(body.get("create_name", "")).strip()
                if not create_name:
                    raise SyncError("bad_request", "Give entity_id or create_name")
                entity_id = await create_local_todo(request.app["hass"], create_name[:60])
            if not entry.runtime_data.backend.is_writable(entity_id):
                raise SyncError("list_read_only", "That list can't be edited, so it can't be linked.", 409)
            link = await engine.link(
                store_id=store_id, store_name=store_name, entity_id=entity_id,
                matches=list(body.get("matches") or []), imports=list(body.get("imports") or []),
            )
            _LOGGER.info("Linked a Simple Aisle store to %s (device %s)", entity_id, device_id[:8])
            return self.json({"link": link})

        return await self._run(request, handle)


class LinkView(_Base):
    url = API_BASE + "/links/{store_id}"
    name = "api:simple_aisle:link"

    async def delete(self, request: web.Request, store_id: str) -> web.Response:
        async def handle() -> web.Response:
            entry, _ = await self._device(request)
            remove = request.query.get("items", "keep") == "remove"
            found = await entry.runtime_data.engine.unlink(store_id, remove_items=remove)
            if not found:
                raise SyncError("not_found", "That store isn't linked.", 404)
            return self.json({"ok": True})

        return await self._run(request, handle)


class SyncView(_Base):
    url = f"{API_BASE}/sync"
    name = "api:simple_aisle:sync"

    async def post(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            entry, device_id = await self._device(request)
            body = await self._body(request)
            for key in ("entries", "tombstones", "results", "stores"):
                if key in body and not isinstance(body[key], list):
                    raise SyncError("bad_request", f"{key} must be a list")
            return self.json(await entry.runtime_data.engine.sync(device_id, body))

        return await self._run(request, handle)


class WaitView(_Base):
    url = f"{API_BASE}/wait"
    name = "api:simple_aisle:wait"

    async def get(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            entry, _ = await self._device(request)
            try:
                after = int(request.query.get("after", "0"))
                timeout = min(float(request.query.get("timeout", WAIT_MAX_SECONDS)), WAIT_MAX_SECONDS)
            except ValueError as err:
                raise SyncError("bad_request", "after and timeout must be numbers") from err
            engine = entry.runtime_data.engine
            seq = await engine.wait_for_ops(after, max(timeout, 0))
            return self.json({"seq": seq, "pending": engine.pending_count()})

        return await self._run(request, handle)


class UnpairView(_Base):
    url = f"{API_BASE}/unpair"
    name = "api:simple_aisle:unpair"

    async def post(self, request: web.Request) -> web.Response:
        async def handle() -> web.Response:
            entry, device_id = await self._device(request)
            await entry.runtime_data.engine.revoke(device_id)
            return self.json({"ok": True}, status_code=HTTPStatus.OK)

        return await self._run(request, handle)
