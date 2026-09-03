"""Test the WiCAN binary sensor platform."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.const import CONF_WEBHOOK_ID, STATE_ON, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.wican.const import DOMAIN

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_binary_sensor_entities_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test binary sensor entities are created."""
    entity_registry = er.async_get(hass)

    # Check binary sensors exist
    ble_status = entity_registry.async_get("binary_sensor.wican_device_ble_status")
    assert ble_status is not None
    assert ble_status.unique_id.endswith("_ble_status")

    ecu_status = entity_registry.async_get("binary_sensor.wican_device_ecu_status")
    assert ecu_status is not None
    assert ecu_status.unique_id.endswith("_ecu_status")


async def test_binary_sensor_states_update_from_webhook(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test binary sensor states update when webhook data arrives."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post webhook data
    await client.post(f"/api/webhook/{webhook_id}", json=mock_webhook_data)
    await hass.async_block_till_done()

    # Check binary sensor states
    ble_status_state = hass.states.get("binary_sensor.wican_device_ble_status")
    assert ble_status_state is not None
    assert ble_status_state.state == STATE_OFF  # "Disabled" maps to off

    ecu_status_state = hass.states.get("binary_sensor.wican_device_ecu_status")
    assert ecu_status_state is not None
    assert ecu_status_state.state == STATE_ON  # "Online" maps to on


async def test_binary_sensor_bluetooth_on_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test Bluetooth binary sensor on/off states."""
    import copy
    
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Test "enable" (lowercase, as per TRUE_STRINGS) -> ON
    data = copy.deepcopy(mock_webhook_data)
    data["status"]["ble_status"] = "enable"

    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    ble_state = hass.states.get("binary_sensor.wican_device_ble_status")
    assert ble_state is not None
    assert ble_state.state == STATE_ON

    # Test "disable" -> OFF
    data = copy.deepcopy(mock_webhook_data)
    data["status"]["ble_status"] = "disable"

    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    ble_state = hass.states.get("binary_sensor.wican_device_ble_status")
    assert ble_state is not None
    assert ble_state.state == STATE_OFF


async def test_binary_sensor_ecu_on_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test ECU binary sensor on/off states."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Test "Online" -> ON
    data = mock_webhook_data.copy()
    data["status"]["ecu_status"] = "Online"

    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    ecu_state = hass.states.get("binary_sensor.wican_device_ecu_status")
    assert ecu_state.state == STATE_ON

    # Test "Offline" -> OFF
    data["status"]["ecu_status"] = "Offline"

    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    ecu_state = hass.states.get("binary_sensor.wican_device_ecu_status")
    assert ecu_state.state == STATE_OFF


async def test_binary_sensor_state_restoration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test binary sensor state is restored on startup."""
    # Store states before setup
    hass.states.async_set(
        "binary_sensor.wican_device_ble_status",
        "on",
    )
    hass.states.async_set(
        "binary_sensor.wican_device_ecu_status",
        "off",
    )
    
    # Ensure state is written to restore state storage
    await hass.async_block_till_done()
    
    # Set up the integration - this should restore states
    mock_config_entry.add_to_hass(hass)
    
    with patch(
        "custom_components.wican.async_get_clientsession"
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh"
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Check that states were restored
    ble_status = hass.states.get("binary_sensor.wican_device_ble_status")
    assert ble_status is not None
    assert ble_status.state == "on"
    
    ecu_status = hass.states.get("binary_sensor.wican_device_ecu_status")
    assert ecu_status is not None
    assert ecu_status.state == "off"


def test_is_true_status_with_non_string():
    """Test is_true_status with non-string values."""
    from custom_components.wican.binary_sensor import is_true_status
    
    # Test with integers
    assert is_true_status(1) is True
    assert is_true_status(0) is False
    
    # Test with boolean
    assert is_true_status(True) is True
    assert is_true_status(False) is False
    
    # Test with None
    assert is_true_status(None) is False
    
    # Test with list (any truthy value)
    assert is_true_status([1, 2]) is True
    assert is_true_status([]) is False


async def test_binary_sensor_state_restoration_with_none_initial(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test binary sensor state restoration when initial state is None."""
    mock_config_entry.add_to_hass(hass)
    
    # Pre-populate state registry with a saved state
    hass.states.async_set(
        "binary_sensor.wican_device_ble_status",
        "on",
        {"restored": True}
    )
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # State should be restored
    state = hass.states.get("binary_sensor.wican_device_ble_status")
    assert state is not None


async def test_binary_sensor_none_checks(hass: HomeAssistant, hass_client) -> None:
    """Test binary sensor handles None data gracefully."""
    from unittest.mock import patch
    from homeassistant.const import CONF_WEBHOOK_ID
    from custom_components.wican.const import DOMAIN
    from tests.conftest import MockConfigEntry
    
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "mdns": "http://wican_test.local",
            CONF_WEBHOOK_ID: "test_webhook",
        },
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    with patch("custom_components.wican._async_register_webhook_on_device", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    client = await hass_client()
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    
    # Trigger webhook with None status
    await client.post(
        f"/api/webhook/{webhook_id}",
        json={"status": None}
    )
    await hass.async_block_till_done()
    
    # Trigger webhook with status but missing ble_status key
    await client.post(
        f"/api/webhook/{webhook_id}",
        json={"status": {"other_key": "value"}}
    )
    await hass.async_block_till_done()
    
    # Verify entities exist and didn't crash
    state = hass.states.get("binary_sensor.wican_test_ble_status")
    assert state is not None


async def test_reporting_sensor_created(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """The Reporting sensor is created alongside the other binary sensors."""
    entity_registry = er.async_get(hass)

    reporting = entity_registry.async_get("binary_sensor.wican_device_reporting")
    assert reporting is not None
    assert reporting.unique_id.endswith("_reporting")

    state = hass.states.get("binary_sensor.wican_device_reporting")
    assert state is not None
    assert state.attributes.get("device_class") == "connectivity"


async def test_reporting_sensor_unknown_before_first_webhook(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """No webhook has arrived yet, so there's nothing to report freshness on."""
    state = hass.states.get("binary_sensor.wican_device_reporting")
    assert state is not None
    assert state.state == "unknown"


async def test_reporting_sensor_on_after_webhook(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """A webhook push marks the device as reporting."""
    entry = init_integration
    client = await hass_client()

    await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_webhook_data)
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.wican_device_reporting")
    assert state.state == STATE_ON


async def test_reporting_sensor_goes_stale_after_timeout(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """No further pushes within the timeout flips the sensor off.

    The timeout is normally minutes long (see REPORTING_MIN_TIMEOUT), so this
    patches it down to a fraction of a second rather than simulating time
    passing: the stale check is armed via async_call_later, which schedules
    against the event loop's own monotonic clock and isn't moved by
    async_fire_time_changed (that only affects wall-clock-based trackers).
    """
    import asyncio

    with (
        patch("custom_components.wican.binary_sensor.REPORTING_STALE_MULTIPLIER", 0),
        patch("custom_components.wican.binary_sensor.REPORTING_MIN_TIMEOUT", 0.1),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        client = await hass_client()
        await client.post(
            f"/api/webhook/{mock_config_entry.data[CONF_WEBHOOK_ID]}",
            json=mock_webhook_data,
        )
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.wican_device_reporting").state == STATE_ON

        await asyncio.sleep(0.3)
        await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.wican_device_reporting").state == STATE_OFF


async def test_reporting_sensor_restored_state_still_goes_stale(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """A restored "on" state still gets its own stale-check timer armed.

    Without this, an entity restored as "on" after a Home Assistant restart
    would stay "on" forever if the device never reports again - nothing
    would be left to flip it off, since the new session's coordinator has no
    last_webhook_time of its own yet to recompute freshness from.
    """
    import asyncio

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        client = await hass_client()
        await client.post(
            f"/api/webhook/{mock_config_entry.data[CONF_WEBHOOK_ID]}",
            json=mock_webhook_data,
        )
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.wican_device_reporting").state == STATE_ON

        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    with (
        patch("custom_components.wican.binary_sensor.REPORTING_STALE_MULTIPLIER", 0),
        patch("custom_components.wican.binary_sensor.REPORTING_MIN_TIMEOUT", 0.1),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get("binary_sensor.wican_device_reporting").state == STATE_ON

        await asyncio.sleep(0.3)
        await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.wican_device_reporting").state == STATE_OFF
