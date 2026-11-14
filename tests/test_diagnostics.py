"""Test the WiCAN diagnostics."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant

from custom_components.wican.diagnostics import async_get_config_entry_diagnostics

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_diagnostics_redacts_webhook_id(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """Test diagnostics redacts sensitive webhook_id."""
    entry = init_integration

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # Webhook ID should be redacted
    assert diagnostics["entry"]["data"][CONF_WEBHOOK_ID] == "**REDACTED**"
    assert diagnostics["runtime_data"]["webhook_id"] == "**REDACTED**"


async def test_diagnostics_never_leaks_webhook_secret(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The webhook secret must not appear ANYWHERE in the diagnostics dump.

    Regression: entry.data also carries webhook_url, which embeds the
    webhook secret in its path (/api/webhook/<id>), and vpn_ip, a
    potentially publicly routable device address. Only webhook_id itself
    was redacted, so a shared diagnostics file handed out push access.
    """
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "webhook_url": f"http://ha.local:8123/api/webhook/{webhook_id}",
            "vpn_ip": "100.64.0.7",
        },
    )
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    serialized = json.dumps(diagnostics, default=str)
    assert webhook_id not in serialized
    assert "100.64.0.7" not in serialized


async def test_diagnostics_includes_device_info(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test diagnostics includes device information."""
    entry = init_integration

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # Check device info fields
    assert diagnostics["device_info"]["fw_version"] == "2.00"
    assert diagnostics["device_info"]["hw_version"] == "v3.1"
    assert diagnostics["device_info"]["device_id"] == "test_device_123"
    assert diagnostics["device_info"]["git_version"] == "abc123"


async def test_diagnostics_includes_coordinator_status(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test diagnostics includes coordinator status."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Before the first push the device has never been heard from.
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["coordinator"]["device_online"] is None
    assert diagnostics["coordinator"]["last_push"] is None
    assert diagnostics["coordinator"]["keep_last_values"] is False

    # Post webhook data to populate coordinator
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # Check coordinator info
    assert diagnostics["coordinator"]["last_update_success"] is True
    assert diagnostics["coordinator"]["data_keys"] == ["status", "pids"]
    assert diagnostics["coordinator"]["device_online"] is True
    assert (
        diagnostics["coordinator"]["last_push"]
        == entry.runtime_data.coordinator.last_push.isoformat()
    )


async def test_diagnostics_includes_entity_states(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test diagnostics includes all entity states."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post webhook data
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # Check entity states are included
    assert "entities" in diagnostics
    assert diagnostics["entity_count"] > 0

    # Should include sensor states
    entities = diagnostics["entities"]
    assert any("wifi_mode" in key for key in entities.keys())
    assert any("battery_voltage" in key for key in entities.keys())


async def test_diagnostics_includes_runtime_data(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test diagnostics includes runtime data."""
    entry = init_integration

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # Check runtime data fields
    assert "runtime_data" in diagnostics
    assert diagnostics["runtime_data"]["post_interval"] == 1000
    assert "device_host" in diagnostics["runtime_data"]
    assert "device_ip" in diagnostics["runtime_data"]


async def test_diagnostics_scoped_to_the_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Diagnostics include only this entry's entities, whatever its name.

    Regression (L7): entities were collected by the "sensor.wican_" name
    prefix, which leaked sibling entries' states in multi-device installs
    and collected nothing once the entry was renamed.
    """
    from homeassistant.helpers import entity_registry as er

    entry = init_integration

    # A sibling entity from ANOTHER (unloaded) WiCAN entry with a
    # matching name prefix must not leak into this entry's dump.
    other = MockConfigEntry(domain="wican", title="WiCAN Other")
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "wican",
        "other_batt",
        suggested_object_id="wican_other_batt_voltage",
        config_entry=other,
    )
    hass.states.async_set("sensor.wican_other_batt_voltage", "11.9")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entity_count"] > 0
    assert "sensor.wican_other_batt_voltage" not in diagnostics["entities"]
    # Entities of this entry are present regardless of naming.
    assert any(
        eid.endswith("battery_voltage") for eid in diagnostics["entities"]
    )


async def test_diagnostics_redacts_vehicle_location(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """The vehicle's coordinates never appear in a diagnostics dump.

    Entity attributes are dumped with each state; the device tracker's
    latitude/longitude are location data and must be redacted like the
    webhook secret.
    """
    entry = init_integration
    client = await hass_client()
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
        json={
            "status": {"device_id": "test_device_123", "uptime": "x"},
            "gps": {"latitude": -37.8123, "longitude": 144.9612, "accuracy": 5},
        },
    )
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    serialized = json.dumps(diagnostics, default=str)
    assert "-37.8123" not in serialized
    assert "144.9612" not in serialized
