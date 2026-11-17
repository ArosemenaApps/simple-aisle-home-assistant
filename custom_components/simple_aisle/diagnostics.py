"""Diagnostics: counts and states only. No tokens, item text, names or device identifiers."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SimpleAisleConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SimpleAisleConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data.engine.diagnostics()
    data["options"] = {k: (bool(v) if k == "notify_service" else v) for k, v in entry.options.items()}
    return data
