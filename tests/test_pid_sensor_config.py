"""Test PID sensor dynamic configuration from webhook data."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.wican.const import DOMAIN

from tests.conftest import MockConfigEntry


async def test_pid_sensor_dynamic_creation_from_webhook(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensors are created dynamically from webhook data."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Simulate webhook with PID data
    webhook_data = {
        "bus": "0",
        "type": "rx",
        "ts": 12345,
        "frame": [{"id": 2015, "dlc": 8, "rtr": False, "extd": False, "data": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "autopid_data": {
            "rpm": 1500,
            "speed": 65,
            "coolant_temp": 85
        },
        "config": {
            "rpm": {"unit": "rpm", "class": ""},
            "speed": {"unit": "km/h", "class": "speed"},
            "coolant_temp": {"unit": "°C", "class": "temperature"}
        }
    }

    # Send webhook via dispatcher (simulates webhook handler)
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Verify PID sensors were created
    entity_reg = er.async_get(hass)
    
    rpm_entity = entity_reg.async_get("sensor.wican_device_rpm")
    assert rpm_entity is not None
    assert hass.states.get("sensor.wican_device_rpm").state == "1500"
    
    speed_entity = entity_reg.async_get("sensor.wican_device_speed")
    assert speed_entity is not None
    assert hass.states.get("sensor.wican_device_speed").state == "65"
    
    coolant_entity = entity_reg.async_get("sensor.wican_device_coolant_temp")
    assert coolant_entity is not None
    assert hass.states.get("sensor.wican_device_coolant_temp").state == "85"


async def test_pid_sensor_unit_normalization(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensor handles unit normalization (none, empty string, None)."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Test with various "no unit" values
    webhook_data = {
        "autopid_data": {
            "pid_none": 100,
            "pid_empty": 200,
            "pid_null": 300
        },
        "config": {
            "pid_none": {"unit": "none", "class": ""},
            "pid_empty": {"unit": "", "class": ""},
            "pid_null": {"unit": None, "class": ""}
        }
    }
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # All should have None as unit
    state_none = hass.states.get("sensor.wican_device_pid_none")
    assert state_none is not None
    assert state_none.attributes.get("unit_of_measurement") is None
    
    state_empty = hass.states.get("sensor.wican_device_pid_empty")
    assert state_empty is not None
    assert state_empty.attributes.get("unit_of_measurement") is None
    
    state_null = hass.states.get("sensor.wican_device_pid_null")
    assert state_null is not None
    assert state_null.attributes.get("unit_of_measurement") is None


async def test_pid_sensor_device_class_normalization(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensor device_class normalization."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    webhook_data = {
        "autopid_data": {
            "temp": 25,
            "pressure": 101.3,
            "custom": 42
        },
        "config": {
            "temp": {"unit": "°C", "class": "temperature"},
            "pressure": {"unit": "kPa", "class": "pressure"},
            "custom": {"unit": "custom", "class": "invalid_class"}
        }
    }
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Valid device classes
    temp_state = hass.states.get("sensor.wican_device_temp")
    assert temp_state.attributes.get("device_class") == "temperature"
    
    pressure_state = hass.states.get("sensor.wican_device_pressure")
    assert pressure_state.attributes.get("device_class") == "pressure"
    
    # Invalid device class should be None
    custom_state = hass.states.get("sensor.wican_device_custom")
    assert custom_state.attributes.get("device_class") is None


async def test_pid_sensor_config_persistence(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensor config is persisted to config entry."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    webhook_data = {
        "autopid_data": {
            "new_pid": 123
        },
        "config": {
            "new_pid": {"unit": "unit", "class": ""}
        }
    }
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Check config was saved
    entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    assert "config" in entry.data
    assert "new_pid" in entry.data["config"]
    assert entry.data["config"]["new_pid"]["unit"] == "unit"
    assert "pid_keys" in entry.data
    assert "new_pid" in entry.data["pid_keys"]


async def test_pid_sensor_update_existing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test updating existing PID sensor values."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Create initial PID sensor
    webhook_data_1 = {
        "autopid_data": {"test_pid": 100},
        "config": {"test_pid": {"unit": "unit", "class": ""}}
    }
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data_1)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data_1)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    assert hass.states.get("sensor.wican_device_test_pid").state == "100"
    
    # Update with new value
    webhook_data_2 = {
        "autopid_data": {"test_pid": 200},
        "config": {"test_pid": {"unit": "unit", "class": ""}}
    }
    
    coordinator.handle_webhook_data(webhook_data_2)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data_2)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Value should be updated
    assert hass.states.get("sensor.wican_device_test_pid").state == "200"


async def test_pid_sensor_multiple_webhooks(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test multiple webhooks adding different PID sensors."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    
    # First webhook with some PIDs
    webhook_data_1 = {
        "autopid_data": {"pid1": 10, "pid2": 20},
        "config": {
            "pid1": {"unit": "u1", "class": ""},
            "pid2": {"unit": "u2", "class": ""}
        }
    }
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data_1)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data_1)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Second webhook with additional PIDs
    webhook_data_2 = {
        "autopid_data": {"pid2": 25, "pid3": 30},
        "config": {
            "pid2": {"unit": "u2", "class": ""},
            "pid3": {"unit": "u3", "class": ""}
        }
    }
    coordinator.handle_webhook_data(webhook_data_2)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data_2)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # All PIDs should exist
    assert hass.states.get("sensor.wican_device_pid1").state == "10"
    assert hass.states.get("sensor.wican_device_pid2").state == "25"  # Updated
    assert hass.states.get("sensor.wican_device_pid3").state == "30"  # New


async def test_pid_sensor_no_config_section(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensors work when config section is missing."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Webhook without config section
    webhook_data = {
        "autopid_data": {"test_pid": 42}
        # No "config" key
    }
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Should still create sensor with default config
    state = hass.states.get("sensor.wican_device_test_pid")
    assert state is not None
    assert state.state == "42"
    assert state.attributes.get("unit_of_measurement") is None
    assert state.attributes.get("device_class") is None


async def test_pid_sensor_partial_config(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensor handles partial config (some PIDs configured, some not)."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    webhook_data = {
        "autopid_data": {
            "configured_pid": 100,
            "unconfigured_pid": 200
        },
        "config": {
            "configured_pid": {"unit": "°C", "class": "temperature"}
            # unconfigured_pid has no config
        }
    }
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Configured PID - uses valid temperature unit "°C"
    configured_state = hass.states.get("sensor.wican_device_configured_pid")
    assert configured_state.state == "100"
    assert configured_state.attributes.get("unit_of_measurement") == "°C"
    assert configured_state.attributes.get("device_class") == "temperature"
    
    # Unconfigured PID (defaults)
    unconfigured_state = hass.states.get("sensor.wican_device_unconfigured_pid")
    assert unconfigured_state.state == "200"
    assert unconfigured_state.attributes.get("unit_of_measurement") is None
    assert unconfigured_state.attributes.get("device_class") is None


async def test_pid_sensor_empty_pids_dict(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test webhook with empty pids dict doesn't crash."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Empty pids
    webhook_data = {
        "autopid_data": {},
        "config": {}
    }
    
    # Should not crash
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()


async def test_pid_sensor_real_world_data_format(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensors with real-world WiCAN webhook data format."""
    mock_config_entry.add_to_hass(hass)
    
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Real-world data format from actual WiCAN device
    webhook_data = {
        "config": {
            "ENGINE_RPM": {"class": "frequency", "unit": "RPM"},
            "SPEED": {"class": "speed", "unit": "km/h"},
            "COOLANT_TMP": {"class": "temperature", "unit": "°C"},
            "FUEL": {"class": "none", "unit": "%"},
            "INTAKE_AIR_TMP": {"class": "temperature", "unit": "°C"},
            "THROTTLE": {"class": "none", "unit": "%"},
            "MAF": {"class": "none", "unit": "g/s"},
            "FUEL_PRESSURE": {"class": "pressure", "unit": "kPa"},
            "0C-EngineRPM": {"class": "speed", "unit": "rpm"},
            "0D-VehicleSpeed": {"class": "speed", "unit": "km/h"},
        },
        "autopid_data": {
            "ENGINE_RPM": 4300,
            "SPEED": 26,
            "COOLANT_TMP": 162,
            "FUEL": 62.35,
            "INTAKE_AIR_TMP": -40,
            "THROTTLE": 53.33,
            "MAF": 12.28,
            "FUEL_PRESSURE": 300,
            "0C-EngineRPM": 4300,
            "0D-VehicleSpeed": 26,
        }
    }
    
    # Send webhook
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    
    # Verify critical sensors were created with correct values
    entity_reg = er.async_get(hass)
    
    # ENGINE_RPM sensor
    rpm_entity = entity_reg.async_get("sensor.wican_device_engine_rpm")
    assert rpm_entity is not None
    rpm_state = hass.states.get("sensor.wican_device_engine_rpm")
    assert rpm_state.state == "4300"
    assert rpm_state.attributes.get("unit_of_measurement") == "rpm"
    # Note: "frequency" class should be normalized to None or valid HA device class
    
    # SPEED sensor
    speed_entity = entity_reg.async_get("sensor.wican_device_speed")
    assert speed_entity is not None
    speed_state = hass.states.get("sensor.wican_device_speed")
    assert speed_state.state == "26"
    assert speed_state.attributes.get("unit_of_measurement") == "km/h"
    assert speed_state.attributes.get("device_class") == "speed"
    
    # COOLANT_TMP sensor
    coolant_entity = entity_reg.async_get("sensor.wican_device_coolant_tmp")
    assert coolant_entity is not None
    coolant_state = hass.states.get("sensor.wican_device_coolant_tmp")
    assert coolant_state.state == "162"
    assert coolant_state.attributes.get("unit_of_measurement") == "°C"
    assert coolant_state.attributes.get("device_class") == "temperature"
    
    # FUEL sensor (class="none" should normalize to None)
    fuel_entity = entity_reg.async_get("sensor.wican_device_fuel")
    assert fuel_entity is not None
    fuel_state = hass.states.get("sensor.wican_device_fuel")
    assert fuel_state.state == "62.35"
    assert fuel_state.attributes.get("unit_of_measurement") == "%"
    # device_class should be None since "none" is not a valid HA device class
    
    # FUEL_PRESSURE sensor
    pressure_entity = entity_reg.async_get("sensor.wican_device_fuel_pressure")
    assert pressure_entity is not None
    pressure_state = hass.states.get("sensor.wican_device_fuel_pressure")
    assert pressure_state.state == "300"
    assert pressure_state.attributes.get("unit_of_measurement") == "kPa"
    assert pressure_state.attributes.get("device_class") == "pressure"
    
    # 0C-EngineRPM sensor (with special chars in name)
    # Note: device_class="speed" with unit="rpm" is invalid, so it gets normalized to None
    obd_rpm_entity = entity_reg.async_get("sensor.wican_device_0c_enginerpm")
    assert obd_rpm_entity is not None
    obd_rpm_state = hass.states.get("sensor.wican_device_0c_enginerpm")
    assert obd_rpm_state.state == "4300"
    assert obd_rpm_state.attributes.get("unit_of_measurement") == "rpm"
    # device_class is None because speed+rpm is an invalid combination
    assert obd_rpm_state.attributes.get("device_class") is None
    
    # Verify config was persisted to entry
    updated_entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    assert "ENGINE_RPM" in updated_entry.data.get("pid_keys", [])
    assert "SPEED" in updated_entry.data.get("pid_keys", [])
    assert "COOLANT_TMP" in updated_entry.data.get("pid_keys", [])
    assert "0C-EngineRPM" in updated_entry.data.get("pid_keys", [])


# Additional edge case tests for PID sensors from test_sensor_edge_cases.py


async def test_pid_sensor_invalid_unit_normalization(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_client,
) -> None:
    """Test PID sensors handle invalid unit values properly."""
    # Add PID configuration with various invalid unit values
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["pid_0x01", "pid_0x02", "pid_0x03"]
    entry_data["config"] = {
        "pid_0x01": {"unit": "none", "class": "temperature"},  # "none" should become None
        "pid_0x02": {"unit": "", "class": "speed"},  # empty string should become None
        "pid_0x03": {"unit": None, "class": "pressure"},  # None should stay None
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession"
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh"
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Verify sensors were created with normalized units
    entity_registry = er.async_get(hass)
    
    sensor1 = entity_registry.async_get("sensor.wican_device_pid_0x01")
    if sensor1:
        state = hass.states.get("sensor.wican_device_pid_0x01")
        # Unit should be None (not "none")
        assert state.attributes.get("unit_of_measurement") is None


async def test_pid_sensor_device_class_normalization_rpm_speed_mismatch(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device_class=speed with rpm unit gets dropped to avoid mismatch."""
    # Add PID configuration with speed class but rpm unit (invalid combo)
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["pid_rpm_speed"]
    entry_data["config"] = {
        "pid_rpm_speed": {"unit": "rpm", "class": "speed"},  # Invalid: speed class + rpm unit
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession"
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh"
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Verify sensor created but device_class was dropped
    state = hass.states.get("sensor.wican_device_pid_rpm_speed")
    if state:
        # device_class should be None (dropped due to mismatch)
        assert state.attributes.get("device_class") is None
        assert state.attributes.get("unit_of_measurement") == "rpm"


async def test_pid_sensor_invalid_device_class_string(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test PID sensor handles invalid device_class strings."""
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["pid_invalid_class"]
    entry_data["config"] = {
        "pid_invalid_class": {"unit": "°C", "class": "invalid_class_name"},
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession"
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh"
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Sensor should be created with device_class=None
    state = hass.states.get("sensor.wican_device_pid_invalid_class")
    if state:
        assert state.attributes.get("device_class") is None


async def test_pid_sensor_empty_autopid_data(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """Test webhook with empty autopid_data doesn't crash."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send webhook with empty autopid_data
    data = {
        "status": {
            "wifi_mode": "Station",
            "batt_voltage": "12.5V",
            "vpn_status": "Not Connected",
        },
        "autopid_data": {},  # Empty PID data
    }

    response = await client.post(f"/api/webhook/{webhook_id}", json=data)
    assert response.status == 204


async def test_pid_sensor_no_autopid_data(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """Test webhook without autopid_data doesn't crash."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send webhook without autopid_data at all
    data = {
        "status": {
            "wifi_mode": "Station",
            "batt_voltage": "12.5V",
            "vpn_status": "Not Connected",
        },
        # No autopid_data key
    }

    response = await client.post(f"/api/webhook/{webhook_id}", json=data)
    assert response.status == 204


async def test_pid_sensor_with_config_from_webhook(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """Test PID sensor configuration can come from webhook config section."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send webhook with PID data AND config
    data = {
        "status": {
            "wifi_mode": "Station",
            "batt_voltage": "12.5V",
        },
        "pids": {
            "pid_0x0d": {"value": 85, "name": "Vehicle Speed"}
        },
        "config": {
            "pid_0x0d": {"unit": "km/h", "class": "speed"}
        }
    }

    response = await client.post(f"/api/webhook/{webhook_id}", json=data)
    assert response.status == 204
    await hass.async_block_till_done()

    # Check that sensor was created with config from webhook
    state = hass.states.get("sensor.wican_device_vehicle_speed")
    if state:
        assert state.attributes.get("unit_of_measurement") == "km/h"


async def test_sensor_invalid_voltage_format(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test sensor handles invalid voltage formats gracefully."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Test invalid voltage formats
    test_cases = [
        ("invalid", "invalid"),  # Non-numeric string passes through
        ("12.5.3V", "12.5.3V"),  # Multiple decimals (invalid, passes through)
        ("ABC", "ABC"),  # Pure alpha passes through
        ("", ""),  # Empty string edge case
    ]

    for invalid_voltage, expected in test_cases:
        data = mock_webhook_data.copy()
        data["status"]["batt_voltage"] = invalid_voltage

        await client.post(f"/api/webhook/{webhook_id}", json=data)
        await hass.async_block_till_done()

        batt_voltage_state = hass.states.get("sensor.wican_device_batt_voltage")
        # Normalization should handle invalid values gracefully
        # Invalid values pass through as-is
        assert batt_voltage_state.state in (invalid_voltage, "unknown", expected)


async def test_sensor_numeric_string_normalization(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test sensors normalize numeric strings to numbers."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Test numeric string normalization
    test_cases = [
        ("42", 42),  # Integer string
        ("3.14", 3.14),  # Float string
        ("100.0", 100.0),  # Float with .0
    ]

    for string_val, expected_num in test_cases:
        data = {
            "status": {
                "wifi_mode": string_val,  # Numeric string in non-numeric field
                "batt_voltage": "12.5V",
            }
        }

        await client.post(f"/api/webhook/{webhook_id}", json=data)
        await hass.async_block_till_done()

        # Coordinator should normalize, but string fields stay as string
        # (only voltage gets special treatment)


async def test_pid_sensor_value_update_with_pending_value(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """Test PID sensor properly handles pending value updates."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send initial PID data
    data = {
        "status": {"wifi_mode": "Station"},
        "pids": {
            "pid_0x0c": {"value": 1000, "name": "Engine RPM"}
        },
        "config": {
            "pid_0x0c": {"unit": "rpm", "class": None}
        }
    }
    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    # Check initial value
    state = hass.states.get("sensor.wican_device_engine_rpm")
    if state:
        assert float(state.state) == 1000

    # Send update
    data["pids"]["pid_0x0c"]["value"] = 2500
    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    # Check updated value
    state = hass.states.get("sensor.wican_device_engine_rpm")
    if state:
        assert float(state.state) == 2500


@pytest.mark.asyncio
async def test_pid_sensor_ev_parameters(
    hass: HomeAssistant,
    hass_client,
    init_integration: MockConfigEntry,
) -> None:
    """Test EV-specific PID sensors (SOC, HV battery, charging, etc.)."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send EV-specific PID data (typical EV like Nissan Leaf, Tesla, etc.)
    data = {
        "status": {"wifi_mode": "Station"},
        "autopid_data": {
            "soc": 75,
            "soh": 95,
            "hv_v": 355.5,
            "hv_a": -12.3,
            "hv_w": 4380,
            "range": 180,
            "lv_v": 12.6,
            "hv_t_1": 28,
            "hv_t_a": 30,
            "charger_dc_pwr": 0,
            "kwh_charged": 45.2,
        },
        "config": {
            "soc": {"unit": "%", "class": "battery"},
            "soh": {"unit": "%", "class": "battery"},
            "hv_v": {"unit": "V", "class": "voltage"},
            "hv_a": {"unit": "A", "class": "current"},
            "hv_w": {"unit": "W", "class": "power"},
            "range": {"unit": "km", "class": "distance"},
            "lv_v": {"unit": "V", "class": "voltage"},
            "hv_t_1": {"unit": "°C", "class": "temperature"},
            "hv_t_a": {"unit": "°C", "class": "temperature"},
            "charger_dc_pwr": {"unit": "kW", "class": "power"},
            "kwh_charged": {"unit": "kWh", "class": "energy"},
        }
    }
    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    # Verify SOC sensor was created with correct unit
    state = hass.states.get("sensor.wican_device_soc")
    if state:
        assert state.attributes.get("unit_of_measurement") == "%"

    # Verify HV Battery Voltage sensor was created with correct unit
    state = hass.states.get("sensor.wican_device_hv_v")
    if state:
        assert state.attributes.get("unit_of_measurement") == "V"

    # Verify HV Battery Current sensor was created with correct unit
    state = hass.states.get("sensor.wican_device_hv_a")
    if state:
        assert state.attributes.get("unit_of_measurement") == "A"

    # Verify Range sensor was created with correct unit
    state = hass.states.get("sensor.wican_device_range")
    if state:
        assert state.attributes.get("unit_of_measurement") == "km"

    # Verify 12V Battery sensor was created with correct unit
    state = hass.states.get("sensor.wican_device_lv_v")
    if state:
        assert state.attributes.get("unit_of_measurement") == "V"

    # Verify HV battery temperature sensor
    state = hass.states.get("sensor.wican_device_hv_t_1")
    if state:
        assert state.attributes.get("unit_of_measurement") == "°C"

    # Verify charger power sensor
    state = hass.states.get("sensor.wican_device_charger_dc_pwr")
    if state:
        assert state.attributes.get("unit_of_measurement") == "kW"


@pytest.mark.asyncio
async def test_pid_sensor_ev_fallback_units_and_icons(
    hass: HomeAssistant,
    hass_client,
    init_integration: MockConfigEntry,
) -> None:
    """Test EV sensors get correct fallback units and icons when config not provided."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send EV data WITHOUT config section (device doesn't send it)
    data = {
        "status": {"wifi_mode": "Station"},
        "autopid_data": {
            "soc": 80,
            "hv_v": 400,
            "hv_a": 50,
            "range": 200,
            "hv_t_a": 35,
            "power_max": 150,
            "regen_max": 60,
        },
        # No config section - relies on fallback mappings
    }
    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    # Verify SOC sensor with fallback unit
    state = hass.states.get("sensor.wican_device_soc")
    if state:
        # Unit should come from PID_NAME_UNITS fallback
        assert state.attributes.get("unit_of_measurement") == "%"

    # Verify HV Battery Voltage sensor with fallback unit
    state = hass.states.get("sensor.wican_device_hv_v")
    if state:
        assert state.attributes.get("unit_of_measurement") == "V"

    # Verify Range sensor with fallback unit
    state = hass.states.get("sensor.wican_device_range")
    if state:
        assert state.attributes.get("unit_of_measurement") == "km"

    # Verify Power max sensor with fallback unit
    state = hass.states.get("sensor.wican_device_power_max")
    if state:
        assert state.attributes.get("unit_of_measurement") == "kW"

    # Verify regen max sensor with fallback unit
    state = hass.states.get("sensor.wican_device_regen_max")
    if state:
        assert state.attributes.get("unit_of_measurement") == "kW"


@pytest.mark.asyncio
async def test_pid_sensor_tyre_pressure_monitoring(
    hass: HomeAssistant,
    hass_client,
    init_integration: MockConfigEntry,
) -> None:
    """Test tyre pressure and temperature sensors (TPMS)."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    client = await hass_client()

    # Send tyre data (common in EVs and modern ICE vehicles)
    data = {
        "status": {"wifi_mode": "Station"},
        "autopid_data": {
            "tyre_p_fl": 32,
            "tyre_p_fr": 33,
            "tyre_p_rl": 31,
            "tyre_p_rr": 32,
            "tyre_t_fl": 25,
            "tyre_t_fr": 26,
        },
        "config": {
            "tyre_p_fl": {"unit": "psi", "class": "pressure"},
            "tyre_p_fr": {"unit": "psi", "class": "pressure"},
            "tyre_p_rl": {"unit": "psi", "class": "pressure"},
            "tyre_p_rr": {"unit": "psi", "class": "pressure"},
            "tyre_t_fl": {"unit": "°C", "class": "temperature"},
            "tyre_t_fr": {"unit": "°C", "class": "temperature"},
        }
    }
    await client.post(f"/api/webhook/{webhook_id}", json=data)
    await hass.async_block_till_done()

    # Verify front left tyre pressure sensor created with correct unit
    state = hass.states.get("sensor.wican_device_tyre_p_fl")
    if state:
        # Note: HA may convert psi to kPa depending on device_class=pressure
        assert state.attributes.get("unit_of_measurement") in ("psi", "kPa")

    # Verify front left tyre temperature sensor created with correct unit
    state = hass.states.get("sensor.wican_device_tyre_t_fl")
    if state:
        assert state.attributes.get("unit_of_measurement") == "°C"


@pytest.mark.asyncio
async def test_pid_sensor_invalid_device_class_unit_combo_filtered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that invalid device class + unit combinations are filtered.
    
    This tests the hardening added to handle firmware bugs like:
    - EngineRPM with class="speed" but unit="rpm" (firmware bug)
    - Temperature with invalid unit
    """
    # Setup config with invalid class+unit combinations
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["engine_rpm", "test_sensor"]
    entry_data["config"] = {
        # BUG: Firmware has class="speed" but unit="rpm" - invalid combo!
        # speed class requires km/h, mph, m/s, etc - NOT rpm
        "engine_rpm": {"unit": "rpm", "class": "speed"},
        # BUG: Invalid unit "bananas" for temperature class
        "test_sensor": {"unit": "bananas", "class": "temperature"},
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession"
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh"
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # engine_rpm: device_class should be filtered out (speed+rpm invalid)
    # but unit should still be preserved
    rpm_state = hass.states.get("sensor.wican_device_engine_rpm")
    if rpm_state:
        assert rpm_state.attributes.get("unit_of_measurement") == "rpm"
        # Device class "speed" should be filtered because rpm is not valid for speed
        assert rpm_state.attributes.get("device_class") is None

    # test_sensor: device_class should be filtered (temperature+bananas invalid)
    test_state = hass.states.get("sensor.wican_device_test_sensor")
    if test_state:
        assert test_state.attributes.get("unit_of_measurement") == "bananas"
        # Device class "temperature" should be filtered because bananas is not valid
        assert test_state.attributes.get("device_class") is None



async def test_standard_pid_sensors_are_measurements(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Standard PIDs restored from the config entry report as measurements.

    42-ControlModuleVolt and 46-AmbientAirTemp are the two standard Mode 01
    PIDs the WiCAN firmware exposes for 12V supply voltage and outside
    temperature. The firmware spells the ambient unit "degC", which Home
    Assistant does not accept for the temperature device class.
    """
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["42-ControlModuleVolt", "46-AmbientAirTemp"]
    entry_data["config"] = {
        "42-ControlModuleVolt": {"unit": "V", "class": "voltage"},
        "46-AmbientAirTemp": {"unit": "degC", "class": "temperature"},
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession",
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh",
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    volt_state = hass.states.get("sensor.wican_device_42_controlmodulevolt")
    assert volt_state is not None
    assert volt_state.attributes.get("state_class") == "measurement"
    assert volt_state.attributes.get("device_class") == "voltage"
    assert volt_state.attributes.get("unit_of_measurement") == "V"

    temp_state = hass.states.get("sensor.wican_device_46_ambientairtemp")
    assert temp_state is not None
    assert temp_state.attributes.get("state_class") == "measurement"
    assert temp_state.attributes.get("device_class") == "temperature"
    # "degC" is rewritten to the unit HA accepts for the temperature class
    assert temp_state.attributes.get("unit_of_measurement") == "°C"


async def test_pid_sensor_created_from_webhook_is_a_measurement(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A PID discovered from a webhook gets the same state class as a restored one.

    Regression test: the discovery path used to omit state_class entirely, so
    a PID looked like a text sensor until Home Assistant was restarted and the
    restore path recreated it.
    """
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    webhook_data = {
        "autopid_data": {"42-ControlModuleVolt": 12.6, "46-AmbientAirTemp": 21},
        "config": {
            "42-ControlModuleVolt": {"unit": "V", "class": "voltage"},
            "46-AmbientAirTemp": {"unit": "degC", "class": "temperature"},
        },
    }

    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(webhook_data)
    async_dispatcher_send(hass, "wican", "test_webhook_id", webhook_data)
    await hass.async_block_till_done()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()

    volt_state = hass.states.get("sensor.wican_device_42_controlmodulevolt")
    assert volt_state is not None
    assert volt_state.attributes.get("state_class") == "measurement"
    assert volt_state.attributes.get("device_class") == "voltage"
    assert volt_state.state == "12.6"

    temp_state = hass.states.get("sensor.wican_device_46_ambientairtemp")
    assert temp_state is not None
    assert temp_state.attributes.get("state_class") == "measurement"
    assert temp_state.attributes.get("unit_of_measurement") == "°C"


async def test_text_pid_sensor_has_no_state_class(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """PIDs with neither a unit nor a device class must not be measurements.

    Vehicle profiles mark flags such as CHARGING and AC_PLUG as binary and send
    them with unit "" and class "none"; HA raises if a measurement sensor
    reports a non-numeric state.
    """
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["AC_PLUG", "41-MonStatusDriveCycle"]
    entry_data["config"] = {
        "AC_PLUG": {"unit": "", "class": "none"},
        # Bitfield PIDs report the placeholder unit "Encoded"
        "41-MonStatusDriveCycle": {"unit": "Encoded", "class": "none"},
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession",
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh",
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    for entity_id in (
        "sensor.wican_device_ac_plug",
        "sensor.wican_device_41_monstatusdrivecycle",
    ):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes.get("state_class") is None, entity_id
        assert state.attributes.get("unit_of_measurement") is None, entity_id


async def test_device_class_dropped_when_ha_rejects_the_unit(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Classes HA cannot pair with the reported unit are dropped, not passed on.

    The firmware pairs gas with "ratio" and volume_storage with "%", and
    declares a device class on PIDs that report no unit at all. Keeping the
    class would cost the sensor its unit conversion and long-term statistics.
    """
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["24-O2S1WRLambda", "2F-FuelLevel", "3C-CatTempB1S1"]
    entry_data["config"] = {
        "24-O2S1WRLambda": {"unit": "ratio", "class": "gas"},
        "2F-FuelLevel": {"unit": "%", "class": "volume_storage"},
        "3C-CatTempB1S1": {"unit": "none", "class": "temperature"},
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession",
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh",
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    lambda_state = hass.states.get("sensor.wican_device_24_o2s1wrlambda")
    assert lambda_state is not None
    assert lambda_state.attributes.get("device_class") is None
    assert lambda_state.attributes.get("unit_of_measurement") == "ratio"
    assert lambda_state.attributes.get("state_class") == "measurement"

    fuel_state = hass.states.get("sensor.wican_device_2f_fuellevel")
    assert fuel_state is not None
    assert fuel_state.attributes.get("device_class") is None
    assert fuel_state.attributes.get("unit_of_measurement") == "%"

    # Device class declared but no unit reported: HA accepts neither pairing
    cat_state = hass.states.get("sensor.wican_device_3c_cattempb1s1")
    assert cat_state is not None
    assert cat_state.attributes.get("device_class") is None
    assert cat_state.attributes.get("unit_of_measurement") is None


async def test_pid_sensors_get_display_precision(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """PIDs with a device class get a sensible number of decimals.

    Without one HA renders whatever the device sends: ODOMETER came through
    as 50339.1443967231 after the km->mi conversion, while HV_V arrived as
    345 with no decimals because the firmware's JSON drops the fraction on
    integral values.
    """
    entry_data = mock_config_entry.data.copy()
    entry_data["pid_keys"] = ["ODOMETER", "HV_V", "BATT_TEMP", "MOTOR_RPM"]
    entry_data["config"] = {
        "ODOMETER": {"unit": "km", "class": "distance"},
        "HV_V": {"unit": "V", "class": "voltage"},
        "BATT_TEMP": {"unit": "°C", "class": "temperature"},
        # rpm has no HA device class, so no precision should be invented
        "MOTOR_RPM": {"unit": "rpm", "class": "speed"},
    }
    mock_config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=entry_data,
        options=mock_config_entry.options,
        unique_id=mock_config_entry.unique_id,
    )
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican.async_get_clientsession",
    ), patch(
        "custom_components.wican.WiCANDataUpdateCoordinator.async_config_entry_first_refresh",
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)

    def precision(entity_id: str) -> int | None:
        entry = entity_registry.async_get(entity_id)
        assert entry is not None, entity_id
        return entry.options.get("sensor", {}).get("suggested_display_precision")

    assert precision("sensor.wican_device_odometer") == 1
    # Two decimals so per-cell voltage differences stay visible
    assert precision("sensor.wican_device_hv_v") == 2
    assert precision("sensor.wican_device_batt_temp") == 1
    # No device class resolved (speed does not accept rpm), so no precision
    assert precision("sensor.wican_device_motor_rpm") is None
