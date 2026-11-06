"""Diagnostics platform for WiCAN integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import MeatPiConfigEntry

# webhook_url embeds the webhook secret in its path; vpn_ip can be a
# publicly routable address for the device.
TO_REDACT = {CONF_WEBHOOK_ID, "webhook_url", "vpn_ip"}

# Entity attributes dumped with each state: the device tracker carries the
# vehicle's location, which HA treats as sensitive in diagnostics.
ATTRIBUTES_TO_REDACT = {"latitude", "longitude"}


def _history_sync_diagnostics(config_entry: MeatPiConfigEntry) -> dict[str, Any] | None:
    """Summarize the last history catch-up run for diagnostics."""
    runtime = config_entry.runtime_data
    result = runtime.last_history_result
    if result is None:
        return None
    return {
        "ran": result.ran,
        "source": result.source,
        "pages": result.pages,
        "rows_fetched": result.rows_fetched,
        "rows_used": result.rows_used,
        "rows_rejected": result.rows_rejected,
        "rows_deferred": result.rows_deferred,
        "hours_imported": result.hours_imported,
        "hours_skipped_existing": result.hours_skipped_existing,
        "hours_overwritten_live": result.hours_overwritten_live,
        "pids_imported": result.pids_imported,
        "pids_skipped": result.pids_skipped,
        "errors": list(result.errors),
        "watermark": result.watermark,
        "cursor": result.cursor,
        "more_pending": result.more_pending,
        "followup_in": result.followup_in,
        "followup_due": runtime.history_followup_due,
        "finished_at": result.finished_at,
        "device": result.device,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: MeatPiConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = config_entry.runtime_data.coordinator

    # Redact sensitive data
    entry_data = async_redact_data(dict(config_entry.data), TO_REDACT)

    # Collect THIS entry's entity states via the registry: a name-prefix
    # match would leak sibling devices' states in multi-car installs and
    # miss everything once the entry is renamed.
    wican_entities = {}
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(
        registry, config_entry.entry_id,
    ):
        state = hass.states.get(registry_entry.entity_id)
        if state is None:
            continue
        wican_entities[state.entity_id] = {
            "state": state.state,
            "attributes": async_redact_data(
                dict(state.attributes), ATTRIBUTES_TO_REDACT,
            ),
        }

    return {
        "entry": {
            "title": config_entry.title,
            "entry_id": config_entry.entry_id,
            "unique_id": config_entry.unique_id,
            "data": entry_data,
            "options": dict(config_entry.options),
        },
        "device_info": {
            "device_type": config_entry.data.get("device_type"),
            "profile_model": config_entry.runtime_data.device_profile.model,
            "fw_version": config_entry.data.get("fw_version"),
            "hw_version": config_entry.data.get("hw_version"),
            "device_id": config_entry.data.get("device_id"),
            "git_version": config_entry.data.get("git_version"),
            "mdns": config_entry.data.get("mdns"),
            "host": config_entry.data.get("host"),
            "ip": config_entry.data.get("ip"),
        },
        "capabilities": {
            "api_level": config_entry.runtime_data.capabilities.api_level,
            "components": sorted(config_entry.runtime_data.capabilities.components),
        },
        "history_sync": _history_sync_diagnostics(config_entry),
        "runtime_data": {
            "webhook_id": "**REDACTED**",
            "post_interval": config_entry.runtime_data.post_interval,
            "device_host": config_entry.runtime_data.device_host,
            "device_ip": config_entry.runtime_data.device_ip,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval)
            if coordinator.update_interval
            else None,
            "data_keys": list(coordinator.data.keys()) if coordinator.data else [],
        },
        "entities": wican_entities,
        "entity_count": len(wican_entities),
    }
