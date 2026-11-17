"""The Simple Aisle sidebar panel (admins only): pairing, devices, links, queue."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

STATIC_URL = f"/{DOMAIN}_static"
PANEL_VERSION = "0.1.0"


async def async_register(hass: HomeAssistant) -> None:
    frontend = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend), False)])
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="simple-aisle",
        webcomponent_name="simple-aisle-panel",
        sidebar_title="Simple Aisle",
        sidebar_icon="mdi:cart-outline",
        module_url=f"{STATIC_URL}/simple-aisle-panel.js?v={PANEL_VERSION}",
        require_admin=True,
        config={},
    )
