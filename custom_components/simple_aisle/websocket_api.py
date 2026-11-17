"""Admin-only websocket commands behind the Simple Aisle panel (and any other admin UI)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN, STATE_FAILED, STATE_PENDING


@callback
def async_register(hass: HomeAssistant) -> None:
    for command in (ws_state, ws_links, ws_pair, ws_revoke, ws_unlink, ws_retry, ws_discard, ws_request_sync):
        websocket_api.async_register_command(hass, command)


def _engine(hass: HomeAssistant, connection, msg) -> Any | None:
    from . import loaded_entry  # noqa: PLC0415

    entry = loaded_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_set_up", "Simple Aisle isn't set up.")
        return None
    return entry


def connection_urls(hass: HomeAssistant) -> list[str]:
    """Every address a phone might reach this HA at: local first, then remote (incl. Home Assistant Cloud)."""
    urls: list[str] = []
    for kwargs in (
        {"allow_external": False, "allow_cloud": False},
        {"allow_internal": False, "allow_cloud": False},
        {"allow_internal": False, "prefer_cloud": True},
    ):
        try:
            url = get_url(hass, allow_ip=True, **kwargs)
        except NoURLAvailableError:
            continue
        if url and url not in urls:
            urls.append(url.rstrip("/"))
    return urls


def pairing_uri(code: str, instance_id: str, name: str, urls: list[str]) -> str:
    """The link the QR code carries. Kept short, because a smaller code reads
    more easily off a screen: no instance id (the app gets it when pairing), and
    ':' and '/' left unescaped (both are legal in a query value)."""
    del instance_id  # returned by /pair instead
    parts = ["v=1", f"c={code}", f"n={quote(name[:24], safe='')}"] + [f"u={quote(u, safe=':/')}" for u in urls]
    return "simpleaisle://ha-pair?" + "&".join(parts)


def qr_svg(text: str) -> str:
    import segno  # noqa: PLC0415

    # Standard 4-module quiet zone; medium error correction.
    return segno.make(text, error="m").svg_inline(scale=8, border=4, dark="#000", light="#fff")


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/state"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    engine = entry.runtime_data.engine
    state = engine.state
    ops = [
        {k: op.get(k) for k in ("op_id", "seq", "type", "store_id", "state", "created_at", "attempts",
                                "last_reason", "result", "lease_device", "finished_at")}
        for op in state["ops"] if op["state"] in (STATE_PENDING, STATE_FAILED)
    ]
    connection.send_result(msg["id"], {
        "household_id": state["household_id"],
        "household_name": state.get("household_name"),
        "devices": [
            {"device_id": d_id, "name": d["name"], "created_at": d.get("created_at"),
             "last_seen": d.get("last_seen"), "app_version": d.get("app_version")}
            for d_id, d in state["devices"].items()
        ],
        "links": engine.public_links(),
        "lists": entry.runtime_data.backend.todo_lists(),
        "ops": ops,
        "pending": engine.pending_count(),
        "stats": state["stats"],
        "urls": connection_urls(hass),
        "settings": {"purchased_action": engine.settings.purchased_action},
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/links"})
@websocket_api.async_response
async def ws_links(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """For dashboards (any signed-in user): which lists are Simple Aisle stores. No devices, codes or queue details."""
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    engine = entry.runtime_data.engine
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    # The live "changes waiting" sensor: dashboards follow its state instead of
    # a number read once (which goes stale when a phone syncs).
    pending_entity = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_pending_changes")
    connection.send_result(msg["id"], {
        "links": [{k: link[k] for k in ("store_id", "store_name", "entity_id", "state")} for link in engine.public_links()],
        "pending": engine.pending_count(),
        "pending_entity": pending_entity,
        "last_sync": engine.state["stats"].get("last_sync"),
    })


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/pair"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_pair(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    engine = entry.runtime_data.engine
    urls = connection_urls(hass)
    if not urls:
        connection.send_error(msg["id"], "no_url",
                              "Set Home Assistant's local or remote URL (Settings → System → Network) first.")
        return
    code, expires = engine.create_pair_code()
    uri = pairing_uri(code, engine.state["instance_id"], hass.config.location_name, urls)
    svg = await hass.async_add_executor_job(qr_svg, uri)
    connection.send_result(msg["id"], {"code": code, "uri": uri, "urls": urls, "expires_at": expires, "svg": svg})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/revoke", vol.Required("device_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_revoke(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    connection.send_result(msg["id"], {"removed": await entry.runtime_data.engine.revoke(msg["device_id"])})


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/unlink",
    vol.Required("store_id"): str,
    vol.Optional("remove_items", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_unlink(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    found = await entry.runtime_data.engine.unlink(msg["store_id"], remove_items=msg["remove_items"])
    connection.send_result(msg["id"], {"removed": found})


async def _set_op_state(entry, op_id: str, new_state: str) -> bool:
    engine = entry.runtime_data.engine
    async with engine.lock:
        op = next((o for o in engine.state["ops"] if o["op_id"] == op_id), None)
        if op is None or op["state"] not in (STATE_PENDING, STATE_FAILED):
            return False
        op["state"] = new_state
        op["lease_device"] = None
        op["lease_until"] = None
        if new_state == STATE_PENDING:
            op["attempts"] = 0
            op.pop("deferred_since", None)
            op.pop("finished_at", None)
        else:
            op["finished_at"] = engine.now()
        await engine._commit()  # noqa: SLF001
        return True


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/retry", vol.Required("op_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_retry(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    connection.send_result(msg["id"], {"ok": await _set_op_state(entry, msg["op_id"], STATE_PENDING)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/discard", vol.Required("op_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_discard(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    connection.send_result(msg["id"], {"ok": await _set_op_state(entry, msg["op_id"], "cancelled")})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/request_sync"})
@websocket_api.async_response
async def ws_request_sync(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    from . import send_sync_request  # noqa: PLC0415

    entry = _engine(hass, connection, msg)
    if entry is None:
        return
    sent = await send_sync_request(hass, entry)
    connection.send_result(msg["id"], {"notifications_sent": sent,
                                       "pending": entry.runtime_data.engine.pending_count()})
