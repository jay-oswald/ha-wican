"""Test the WiCAN binary sensor platform."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.const import CONF_WEBHOOK_ID, STATE_ON, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.wican.binary_sensor import (
    DYNAMIC_PID_BINARY_SENSORS,
    pid_value_to_is_on,
)
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


async def test_profile_binary_pids_become_binary_sensors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """PIDs marked binary in params.json get a binary_sensor, not a sensor.

    CHARGING and CHARGER_CONNECTED report the strings "on" and "off", which
    as sensor states cannot drive a state condition or a device trigger.
    """
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    data = dict(mock_webhook_data)
    data["autopid_data"] = {
        "CHARGING": "on",
        "CHARGER_CONNECTED": "off",
        "SOC": 62,
    }
    data["config"] = {
        "CHARGING": {"unit": "", "class": "battery_charging"},
        "CHARGER_CONNECTED": {"unit": "", "class": "plug"},
        "SOC": {"unit": "%", "class": "battery"},
    }

    client = await hass_client()
    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    charging = hass.states.get("binary_sensor.wican_device_charging")
    assert charging is not None
    assert charging.state == STATE_ON
    assert charging.attributes.get("device_class") == "battery_charging"

    plugged = hass.states.get("binary_sensor.wican_device_charger_connected")
    assert plugged is not None
    assert plugged.state == STATE_OFF
    assert plugged.attributes.get("device_class") == "plug"

    # The same PIDs must not also appear on the sensor platform
    assert hass.states.get("sensor.wican_device_charging") is None
    assert hass.states.get("sensor.wican_device_charger_connected") is None

    # Non-binary PIDs are untouched
    assert hass.states.get("sensor.wican_device_soc") is not None


async def test_binary_pids_restored_from_config_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Binary PIDs stored on the entry are recreated on the right platform."""
    entry_data = dict(mock_config_entry.data)
    entry_data["pid_keys"] = ["CHARGING", "PARK_BRAKE", "SOC"]
    entry_data["config"] = {
        "CHARGING": {"unit": "", "class": "battery_charging"},
        "PARK_BRAKE": {"unit": "none", "class": "none"},
        "SOC": {"unit": "%", "class": "battery"},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    entry.add_to_hass(hass)

    with patch("custom_components.wican.async_get_clientsession"), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh",
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    assert entity_registry.async_get("binary_sensor.wican_device_charging") is not None
    assert entity_registry.async_get("binary_sensor.wican_device_park_brake") is not None
    assert entity_registry.async_get("sensor.wican_device_soc") is not None
    assert entity_registry.async_get("sensor.wican_device_charging") is None


async def test_binary_pid_map_cleared_on_unload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """The dynamic binary sensor map does not outlive the config entry."""
    entry = init_integration

    data = dict(mock_webhook_data)
    data["autopid_data"] = {"CHARGING": "on"}
    data["config"] = {"CHARGING": {"unit": "", "class": "battery_charging"}}

    client = await hass_client()
    await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=data)
    await hass.async_block_till_done()

    assert "CHARGING" in DYNAMIC_PID_BINARY_SENSORS[entry.entry_id]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in DYNAMIC_PID_BINARY_SENSORS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("on", True),
        ("ON", True),
        (" on ", True),
        ("off", False),
        ("OFF", False),
        ("true", True),
        ("false", False),
        ("enable", True),
        ("disable", False),
        (1, True),
        (0, False),
        ("1", True),
        ("0", False),
        (1.0, True),
        (0.0, False),
        (True, True),
        (False, False),
        (None, None),
        ("wat", None),
        ("", None),
    ],
)
def test_pid_value_to_is_on(value, expected) -> None:
    """"off" must read as off - bool("off") is True."""
    assert pid_value_to_is_on(value) is expected
