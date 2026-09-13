"""Test the WiCAN device_tracker platform."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from homeassistant.components.device_tracker import SourceType
from homeassistant.const import CONF_WEBHOOK_ID, STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN
from custom_components.wican.device_tracker import CONF_HAS_REPORTED_GPS


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A WiCAN-PRO entry, overriding the conftest one for this module.

    hw_version matters here in a way it does not elsewhere: only a PRO gets its
    tracker created up front, so the behaviour tests below - which need a
    tracker to exist before any GPS arrives - have to use one. The string is
    what the firmware actually reports ("WiCAN-" + "OBD-PRO", see wican-fw
    CMakeLists.txt), not a tidied-up "WiCAN-PRO".
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "fw_version": "2.00",
            "hw_version": "WiCAN-OBD-PRO",
            "device_id": "test_device_123",
            "git_version": "abc123",
            "host": "wican_test.local",
            "ip": "192.168.1.100",
        },
        options={CONF_POST_INTERVAL: 1000},
        unique_id="wican_test-192.168.1.100:80",
    )


@pytest.fixture
def mock_gps_data():
    """Fixture for GPS webhook data."""
    return {
        "status": {
            "device_id": "test_device_123",  # Match conftest device_id format
            "fw_version": "2.00",
        },
        "gps": {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "accuracy": 10,
            "altitude": 25.5,
            "speed": 15.3,
            "heading": 180,
        },
    }


async def test_device_tracker_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker entity is created on setup."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Verify device_tracker entity was created
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state is not None
    
    # Initially should be unavailable (no GPS data yet)
    assert state.state == STATE_UNAVAILABLE


async def test_device_tracker_gps_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_gps_data: dict,
) -> None:
    """Test device tracker updates with GPS data from webhook."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Send GPS data via coordinator
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(mock_gps_data)
    await hass.async_block_till_done()

    # Verify GPS coordinates are set
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state is not None
    
    # State should be "not_home" (unless in a defined zone)
    assert state.state == STATE_NOT_HOME
    
    # Verify attributes
    assert state.attributes["latitude"] == 37.7749
    assert state.attributes["longitude"] == -122.4194
    assert state.attributes["gps_accuracy"] == 10
    assert state.attributes["source_type"] == SourceType.GPS
    
    # Verify extra attributes
    assert state.attributes["altitude"] == 25.5
    assert state.attributes["speed"] == 15.3
    assert state.attributes["heading"] == 180


async def test_device_tracker_invalid_coordinates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker handles invalid GPS coordinates."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Send invalid GPS data
    invalid_gps_data = {
        "status": {"device_id": "test_device_123"},
        "gps": {
            "latitude": 999.0,  # Invalid latitude
            "longitude": -122.4194,
        },
    }

    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(invalid_gps_data)
    await hass.async_block_till_done()

    # Entity should remain unavailable
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE


async def test_device_tracker_missing_gps_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker when GPS data is missing from webhook."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Send webhook data without GPS
    no_gps_data = {
        "status": {
            "device_id": "test_device_123",
            "fw_version": "2.00",
        },
    }

    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(no_gps_data)
    await hass.async_block_till_done()

    # Entity should remain unavailable
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE


async def test_device_tracker_partial_gps_data(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker with partial GPS data (missing optional fields)."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Send GPS data with only required fields
    partial_gps_data = {
        "status": {"device_id": "test_device_123"},
        "gps": {
            "latitude": 37.7749,
            "longitude": -122.4194,
            # No accuracy, altitude, speed, heading
        },
    }

    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(partial_gps_data)
    await hass.async_block_till_done()

    # Entity should be available with default accuracy
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.state == STATE_NOT_HOME
    assert state.attributes["latitude"] == 37.7749
    assert state.attributes["longitude"] == -122.4194
    assert state.attributes["gps_accuracy"] == 0
    
    # Optional attributes should not be present
    assert "altitude" not in state.attributes
    assert "speed" not in state.attributes
    assert "heading" not in state.attributes


async def test_device_tracker_state_restoration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_gps_data: dict,
) -> None:
    """Test device tracker restores last known location."""
    mock_config_entry.add_to_hass(hass)
    
    # Set up and send GPS data
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(mock_gps_data)
    await hass.async_block_till_done()

    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.attributes["latitude"] == 37.7749
    assert state.attributes["longitude"] == -122.4194

    # Unload and reload
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Verify location was restored
    state = hass.states.get(entity_id)
    assert state.attributes["latitude"] == 37.7749
    assert state.attributes["longitude"] == -122.4194


async def test_device_tracker_zone_matching(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_gps_data: dict,
) -> None:
    """Test device tracker provides GPS data for zone matching."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # Send GPS data
    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(mock_gps_data)
    await hass.async_block_till_done()

    # Verify GPS coordinates are available for zone matching
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    # Zone matching happens automatically via HA core
    # This test verifies the entity provides correct GPS data for zone matching
    assert state.attributes["latitude"] == 37.7749
    assert state.attributes["longitude"] == -122.4194


async def test_device_tracker_unique_id(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker has correct unique ID."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    entity_id = "device_tracker.wican_device_location"
    
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.unique_id == f"{mock_config_entry.entry_id}_device_tracker"


async def test_device_tracker_icon(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker has correct icon."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.attributes["icon"] == "mdi:map-marker"


async def test_device_tracker_source_type(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_gps_data: dict,
) -> None:
    """Test device tracker reports GPS as source type."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data.coordinator
    coordinator.handle_webhook_data(mock_gps_data)
    await hass.async_block_till_done()

    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.attributes["source_type"] == SourceType.GPS


async def test_device_tracker_gps_restoration_value_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker GPS restoration handles ValueError/TypeError."""
    from unittest.mock import Mock
    from custom_components.wican.device_tracker import WiCANDeviceTrackerEntity
    
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    entity_id = "device_tracker.wican_device_location"
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get(entity_id)
    
    # Get the entity
    entity = None
    for component in hass.data["entity_components"].values():
        for ent in component.entities:
            if ent.entity_id == entity_id:
                entity = ent
                break
    
    assert entity is not None
    
    # Mock get_last_state with invalid data types
    mock_state = Mock()
    mock_state.attributes = {
        "latitude": "invalid",  # String instead of float
        "longitude": None,  # None instead of float
        "gps_accuracy": "abc",  # Invalid int
        "altitude": {},  # Invalid type
        "speed": [],  # Invalid type
        "heading": "north",  # Invalid type
    }
    
    with patch.object(entity, "async_get_last_state", return_value=mock_state):
        await entity.async_added_to_hass()
        # Should handle all exceptions gracefully - no crash


async def test_device_tracker_gps_invalid_coordinate_logging(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker logs warning for invalid coordinates."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Update coordinator with invalid coordinates
    mock_config_entry.runtime_data.coordinator.data = {
        "status": {
            "device_id": "test_device_123",
        },
        "gps": {
            "latitude": 999.0,  # Invalid
            "longitude": 999.0,  # Invalid
        },
    }
    mock_config_entry.runtime_data.coordinator.async_set_updated_data(
        mock_config_entry.runtime_data.coordinator.data
    )
    await hass.async_block_till_done()
    
    # Verify entity didn't update with invalid data
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE


async def test_device_tracker_gps_parse_error_logging(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test device tracker logs warning for GPS parse errors."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Update coordinator with unparseable GPS data
    mock_config_entry.runtime_data.coordinator.data = {
        "status": {
            "device_id": "test_device_123",
        },
        "gps": {
            "latitude": "not_a_number",
            "longitude": {"invalid": "type"},
        },
    }
    mock_config_entry.runtime_data.coordinator.async_set_updated_data(
        mock_config_entry.runtime_data.coordinator.data
    )
    await hass.async_block_till_done()
    
    # Verify entity remains unavailable
    entity_id = "device_tracker.wican_device_location"
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE


def _obd_config_entry(**extra_data) -> MockConfigEntry:
    """Build a config entry for hardware known not to report GPS."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "fw_version": "4.21",
            "hw_version": "WiCAN-OBD",
            "device_id": "test_device_123",
            **extra_data,
        },
        unique_id="wican_test-obd",
    )


async def test_device_tracker_not_created_for_gps_less_hardware(
    hass: HomeAssistant,
) -> None:
    """No tracker is created for hardware that never reports a GPS block.

    WiCAN-OBD/-USB firmware never populates the webhook's "gps" key, so
    creating the entity unconditionally left it permanently "unknown" -
    indistinguishable from a real device that has gone offline.
    """
    entry = _obd_config_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("device_tracker.wican_device_location") is None


async def test_device_tracker_created_for_pro_hardware(
    hass: HomeAssistant,
) -> None:
    """WiCAN-PRO ships a GPS module, so the tracker is still created eagerly.

    The hw_version is the string the firmware really sends. An earlier revision
    deny-listed "obd", which matches "WiCAN-OBD-PRO" and so skipped the tracker
    on the one model that has GPS.
    """
    entry = _obd_config_entry(**{"hw_version": "WiCAN-OBD-PRO"})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("device_tracker.wican_device_location") is not None


async def test_device_tracker_created_lazily_after_first_gps_fix(
    hass: HomeAssistant,
    mock_gps_data: dict,
    hass_client,
) -> None:
    """A device that proves it can report GPS gets the tracker after all."""
    entry = _obd_config_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("device_tracker.wican_device_location") is None

    client = await hass_client()
    await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_gps_data)
    await hass.async_block_till_done()

    # The tracker exists now, and the config entry remembers this device can
    # report GPS so it's created eagerly on every future startup.
    assert hass.states.get("device_tracker.wican_device_location") is not None
    assert entry.data.get(CONF_HAS_REPORTED_GPS) is True

    # It missed the payload that triggered its own creation - the next fix
    # populates its coordinates.
    await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_gps_data)
    await hass.async_block_till_done()

    state = hass.states.get("device_tracker.wican_device_location")
    assert state.attributes["latitude"] == 37.7749
    assert state.attributes["longitude"] == -122.4194


async def test_device_tracker_created_eagerly_once_gps_seen_before(
    hass: HomeAssistant,
) -> None:
    """A device that has proven it reports GPS skips the lazy wait."""
    entry = _obd_config_entry(**{CONF_HAS_REPORTED_GPS: True})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("device_tracker.wican_device_location") is not None


async def test_device_tracker_not_created_before_hw_version_is_known(
    hass: HomeAssistant,
) -> None:
    """A fresh install has no hw_version yet, and must not guess "create it".

    Only the webhook handler writes hw_version onto the config entry, from the
    device's first POST. The platforms are set up long before that, so on a
    newly added integration this code sees an empty string. An earlier revision
    deny-listed "obd"/"usb", so an empty string fell through to eager creation
    and every device got the permanently-unavailable entity the branch exists to
    prevent - which then survived as an orphaned registry entry even once the
    real hw_version arrived.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "device_id": "test_device_123",
            # No hw_version: this is what a just-added entry looks like.
        },
        unique_id="wican_test-unknown-hw",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("device_tracker.wican_device_location") is None


async def test_config_entry_unloads_after_lazy_tracker_creation(
    hass: HomeAssistant,
    hass_client,
) -> None:
    """Unloading after a first GPS fix must succeed.

    The coordinator's remove-listener callback raises KeyError when called
    twice, and it is reachable both from the first-fix callback and from the
    entry unload. An exception out of _async_process_on_unload() aborts the
    unload, so the entry could no longer be reloaded, reconfigured or deleted -
    and async_unload() swallows it and returns False, so nothing failed loudly.
    """
    entry = _obd_config_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("device_tracker.wican_device_location") is None

    client = await hass_client()
    await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
        json={
            "status": {"device_id": "test_device_123"},
            "gps": {"latitude": 37.7749, "longitude": -122.4194, "accuracy": 5},
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.wican_device_location") is not None

    # Returns False rather than raising if _async_process_on_unload() blew up.
    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
