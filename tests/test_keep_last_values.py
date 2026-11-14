"""The keep-last-values option, the Connected sensor and restart staleness.

What the entities of a device show while it is away (asleep, out of Wi-Fi
range) is a per-device option:

- off (default): everything that depends on the device goes unavailable;
- on: the entities that carry a reading (status and PID sensors, the
  location) hold their last value. Entities that describe a live link
  (ECU Online, Bluetooth Enabled) or act on the device (buttons) follow
  the device either way.

The Connected sensor says the device is away in both modes, and a restart
of Home Assistant must not make a device that went quiet look connected.

Everything runs through the device simulator: real webhook, coordinator
and entities.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.wican.api import DeviceCapabilities
from custom_components.wican.const import (
    API_LEVEL_V6,
    CONF_KEEP_LAST_VALUES,
    CONF_POST_INTERVAL,
    DOMAIN,
    LAST_SEEN_KEY,
)
from custom_components.wican.coordinator import MeatPiDataUpdateCoordinator

if TYPE_CHECKING:
    from collections.abc import Generator

    from freezegun.api import FrozenDateTimeFactory

    from tests.conftest import MockConfigEntry
    from tests.device_sim import MeatPiDeviceSimulator

BATTERY = "sensor.wican_sim_battery_voltage"
UPTIME = "sensor.wican_sim_uptime"
SOC = "sensor.wican_sim_soc"
LAST_SEEN = "sensor.wican_sim_last_seen"
ECU_ONLINE = "binary_sensor.wican_sim_ecu_online"
BLUETOOTH = "binary_sensor.wican_sim_bluetooth_enabled"
CONNECTED = "binary_sensor.wican_sim_connected"
LOCATION = "device_tracker.wican_sim_location"
RESTART = "button.wican_sim_restart"

KEEP = {CONF_POST_INTERVAL: 15, CONF_KEEP_LAST_VALUES: True}
REGISTER = "custom_components.wican._async_register_webhook_on_device"


@pytest.fixture(autouse=True)
def register() -> Generator[AsyncMock]:
    """Count the webhook registrations sent to the device, off the network.

    A real attempt retries with backoff sleeps, which never end in the
    tests that freeze time: an unexpected registration must fail its
    assertion here, not hang the run.
    """
    with patch(REGISTER, return_value=True) as mock_register:
        yield mock_register


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def _drive(device: MeatPiDeviceSimulator) -> dict[str, Any]:
    """A push with a status reading, a PID and a GPS fix."""
    return {
        **device.status(batt_voltage="12.4V"),
        **device.pids({"SOC": 80}, {"SOC": {"unit": "%", "class": "battery"}}),
        **device.gps(52.52, 13.405),
    }


async def _go_away(device: MeatPiDeviceSimulator) -> None:
    """The device stops pushing and the health-check notices."""
    device.go_stale()
    await device.coordinator.async_refresh()
    await device.hass.async_block_till_done()


async def _set_options(device: MeatPiDeviceSimulator, **options: Any) -> None:
    assert device.entry is not None
    device.hass.config_entries.async_update_entry(
        device.entry, options={**device.entry.options, **options},
    )
    await device.hass.async_block_till_done()


async def _restart(
    device: MeatPiDeviceSimulator,
    freezer: FrozenDateTimeFactory,
    downtime: timedelta,
) -> None:
    """Take the entry down and up again with ``downtime`` in between.

    Unloading hands every entity's state to Home Assistant's restore
    cache, exactly what a restart leaves behind.
    """
    assert device.entry is not None
    hass = device.hass
    assert await hass.config_entries.async_unload(device.entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(downtime)
    assert await hass.config_entries.async_setup(device.entry.entry_id)
    await hass.async_block_till_done()


# ===================================================================
# While the device is away
# ===================================================================


async def test_default_everything_goes_unavailable(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """Without the option, an away device takes its readings with it."""
    await device.async_setup()
    assert device.coordinator.keep_last_values is False
    await device.push_and_settle(_drive(device))
    assert _state(hass, BATTERY) == "12.4"
    assert _state(hass, SOC) == "80"
    assert _state(hass, CONNECTED) == "on"

    await _go_away(device)

    for entity_id in (BATTERY, UPTIME, SOC, LOCATION, ECU_ONLINE, BLUETOOTH):
        assert _state(hass, entity_id) == "unavailable", entity_id
    # The two entities that describe the absence itself stay.
    assert _state(hass, CONNECTED) == "off"
    assert _state(hass, LAST_SEEN) not in ("unavailable", "unknown")

    await device.push_and_settle(device.status(batt_voltage="12.7V"))
    assert _state(hass, BATTERY) == "12.7"
    assert _state(hass, SOC) == "80"
    assert _state(hass, CONNECTED) == "on"


async def test_keep_last_values_holds_the_readings(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """With the option, readings and location stay on their last value."""
    await device.async_setup(options=KEEP)
    assert device.coordinator.keep_last_values is True
    await device.push_and_settle(_drive(device))

    await _go_away(device)

    assert _state(hass, BATTERY) == "12.4"
    assert _state(hass, UPTIME) == "01:23:45"
    assert _state(hass, SOC) == "80"
    location = hass.states.get(LOCATION)
    assert location is not None
    assert location.state != "unavailable"
    assert location.attributes["latitude"] == 52.52
    assert location.attributes["longitude"] == 13.405
    # A held "ECU online" would be a claim about now: the link sensors
    # follow the device, and Connected says it is away.
    assert _state(hass, ECU_ONLINE) == "unavailable"
    assert _state(hass, BLUETOOTH) == "unavailable"
    assert _state(hass, CONNECTED) == "off"

    await device.push_and_settle(
        {**device.status(batt_voltage="12.9V"), **device.pids({"SOC": 79})},
    )
    assert _state(hass, BATTERY) == "12.9"
    assert _state(hass, SOC) == "79"
    assert _state(hass, ECU_ONLINE) == "on"
    assert _state(hass, CONNECTED) == "on"


async def test_keep_last_values_without_a_value_is_unknown(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A reading that never arrived has nothing to hold: unknown, not a guess."""
    await device.async_setup(options=KEEP)
    await device.push_and_settle({"status": {"device_id": device.device_id}})

    await _go_away(device)

    assert _state(hass, BATTERY) == "unknown"
    # No fix was ever received: there is no location to keep.
    assert _state(hass, LOCATION) == "unavailable"


async def test_buttons_follow_the_device_with_keep_last_values(
    device: MeatPiDeviceSimulator,
    hass: HomeAssistant,
    mock_capability_probe: MagicMock,
) -> None:
    """A button cannot reach an away device, whatever the option says."""
    mock_capability_probe.return_value.async_probe.return_value = DeviceCapabilities(
        api_level=API_LEVEL_V6, components=frozenset({"wifi_manager"}),
    )
    await device.async_setup(options=KEEP)
    await device.push_and_settle(_drive(device))
    assert _state(hass, RESTART) != "unavailable"

    await _go_away(device)

    assert _state(hass, RESTART) == "unavailable"
    assert _state(hass, BATTERY) == "12.4"


# ===================================================================
# The Connected sensor
# ===================================================================


async def test_connected_is_unknown_until_the_first_push(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A device never heard from is neither connected nor disconnected."""
    await device.async_setup()
    assert device.coordinator.device_online is None
    assert _state(hass, CONNECTED) == "unknown"

    await device.push_and_settle(device.status())
    assert device.coordinator.device_online is True
    assert _state(hass, CONNECTED) == "on"


async def test_connected_is_a_diagnostic_connectivity_sensor(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    await device.async_setup()
    state = hass.states.get(CONNECTED)
    assert state is not None
    assert state.attributes["device_class"] == "connectivity"
    registry_entry = er.async_get(hass).async_get(CONNECTED)
    assert registry_entry is not None
    assert registry_entry.entity_category == er.EntityCategory.DIAGNOSTIC
    assert registry_entry.translation_key == "connected"


# ===================================================================
# Changing the option
# ===================================================================


async def test_option_applies_at_once_and_sends_nothing_to_the_device(
    device: MeatPiDeviceSimulator, hass: HomeAssistant, register: AsyncMock,
) -> None:
    """Turning the option on brings the readings back without a push.

    The option is typically changed while the device is asleep: nothing
    may be sent to it (the attempt would fail and raise a repair issue),
    and the entities must not wait for the next push to render.
    """
    await device.async_setup()
    await device.push_and_settle(_drive(device))
    await _go_away(device)
    assert _state(hass, BATTERY) == "unavailable"
    register.reset_mock()

    await _set_options(device, **{CONF_KEEP_LAST_VALUES: True})
    assert _state(hass, BATTERY) == "12.4"
    assert _state(hass, SOC) == "80"
    assert _state(hass, LOCATION) != "unavailable"
    assert _state(hass, ECU_ONLINE) == "unavailable"

    await _set_options(device, **{CONF_KEEP_LAST_VALUES: False})
    assert _state(hass, BATTERY) == "unavailable"
    assert _state(hass, SOC) == "unavailable"
    assert _state(hass, LOCATION) == "unavailable"

    register.assert_not_called()


async def test_post_interval_change_still_reaches_the_device(
    device: MeatPiDeviceSimulator, hass: HomeAssistant, register: AsyncMock,
) -> None:
    """Only a new post interval is sent to the device."""
    await device.async_setup()
    assert device.entry is not None
    register.reset_mock()

    await _set_options(device, **{CONF_POST_INTERVAL: 30})
    assert register.await_count == 1
    assert device.entry.runtime_data.post_interval == 30

    # Entry data the device itself reported is nothing to send back.
    hass.config_entries.async_update_entry(
        device.entry, data={**device.entry.data, "git_version": "abc123"},
    )
    await hass.async_block_till_done()
    assert register.await_count == 1


async def test_options_update_notifies_entities_only_on_a_change(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    coordinator = MeatPiDataUpdateCoordinator(hass, mock_config_entry)
    listener = MagicMock()
    unsubscribe = coordinator.async_add_listener(listener)

    coordinator.async_options_updated()
    listener.assert_not_called()

    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={**mock_config_entry.options, CONF_KEEP_LAST_VALUES: True},
    )
    coordinator.async_options_updated()
    assert coordinator.keep_last_values is True
    listener.assert_called_once()

    coordinator.async_options_updated()
    listener.assert_called_once()
    unsubscribe()


async def test_restored_location_follows_the_device_without_a_new_fix(
    device: MeatPiDeviceSimulator,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A tracker on a restored location still renders availability flips.

    After a restart the coordinator holds no GPS data until the next fix.
    The tracker used to skip its state write in that case, so it kept
    showing the restored location as available while the device was away.
    """
    await device.async_setup()
    await device.push_and_settle(_drive(device))
    await _restart(device, freezer, timedelta(seconds=5))
    assert "gps" not in (device.coordinator.data or {})
    assert _state(hass, LOCATION) != "unavailable"

    await _go_away(device)
    assert _state(hass, LOCATION) == "unavailable"

    await _set_options(device, **{CONF_KEEP_LAST_VALUES: True})
    location = hass.states.get(LOCATION)
    assert location is not None
    assert location.attributes["latitude"] == 52.52


# ===================================================================
# Across a restart of Home Assistant
# ===================================================================


async def test_restart_default_device_gone_quiet_starts_unavailable(
    device: MeatPiDeviceSimulator,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device that went quiet before or during the downtime is away.

    The coordinator used to start with no push time at all, so the
    health-check never failed and the restored readings showed as
    current until the next push, which for a parked car is hours away.
    """
    await device.async_setup()
    await device.push_and_settle(_drive(device))
    last_seen = _state(hass, LAST_SEEN)

    await _restart(device, freezer, timedelta(hours=6))

    assert device.coordinator.device_online is False
    for entity_id in (BATTERY, SOC, LOCATION, ECU_ONLINE):
        assert _state(hass, entity_id) == "unavailable", entity_id
    assert _state(hass, CONNECTED) == "off"
    assert _state(hass, LAST_SEEN) == last_seen

    # The car comes back: everything recovers on its first push.
    await device.push_and_settle(_drive(device))
    assert _state(hass, BATTERY) == "12.4"
    assert _state(hass, CONNECTED) == "on"
    assert _state(hass, LAST_SEEN) != last_seen


async def test_restart_keep_last_values_shows_the_restored_readings(
    device: MeatPiDeviceSimulator,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the option, the restored readings stay and Connected is off."""
    await device.async_setup(options=KEEP)
    await device.push_and_settle(_drive(device))

    await _restart(device, freezer, timedelta(hours=6))

    assert _state(hass, BATTERY) == "12.4"
    assert _state(hass, SOC) == "80"
    location = hass.states.get(LOCATION)
    assert location is not None
    assert location.attributes["latitude"] == 52.52
    assert _state(hass, ECU_ONLINE) == "unavailable"
    assert _state(hass, CONNECTED) == "off"


async def test_restart_with_a_recent_push_stays_connected(
    device: MeatPiDeviceSimulator,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A short restart while the car is running changes nothing."""
    await device.async_setup()
    await device.push_and_settle(_drive(device))

    await _restart(device, freezer, timedelta(seconds=30))

    assert device.coordinator.device_online is True
    assert _state(hass, BATTERY) == "12.4"
    assert _state(hass, CONNECTED) == "on"


async def test_first_push_after_a_restart_ends_a_gap(
    device: MeatPiDeviceSimulator,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The restored push time lets the next push count as a reconnect.

    That is the trigger for the history catch-up: a drive recorded while
    Home Assistant was restarting is a gap like any other.
    """
    await device.async_setup()
    await device.push_and_settle(_drive(device))
    await _restart(device, freezer, timedelta(hours=6))

    assert device.coordinator.handle_webhook_data(device.status()) is True
    assert device.coordinator.handle_webhook_data(device.status()) is False


# ===================================================================
# Restoring the last push time (coordinator)
# ===================================================================


def _register_last_seen(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Create the Last seen registry entry a previous run left behind."""
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{LAST_SEEN_KEY}",
            config_entry=entry,
            suggested_object_id="wican_device_last_seen",
        )
        .entity_id
    )


async def test_restore_last_push_without_a_previous_run(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> None:
    """A fresh install has nothing to restore and waits for its first push."""
    mock_config_entry.add_to_hass(hass)
    coordinator = MeatPiDataUpdateCoordinator(hass, mock_config_entry)

    # No Last seen entity registered at all.
    coordinator.async_restore_last_push()
    assert coordinator.last_push is None

    # Registered, but Home Assistant kept no state for it.
    _register_last_seen(hass, mock_config_entry)
    coordinator.async_restore_last_push()
    assert coordinator.last_push is None
    assert coordinator.device_online is None


@pytest.mark.parametrize(
    "stored",
    ["unknown", "unavailable", "not a date", "2026-13-45T00:00:00+00:00"],
)
async def test_restore_last_push_ignores_unusable_states(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, stored: str,
) -> None:
    """Anything that is not a real timestamp is left alone."""
    mock_config_entry.add_to_hass(hass)
    entity_id = _register_last_seen(hass, mock_config_entry)
    mock_restore_cache(hass, [State(entity_id, stored)])
    coordinator = MeatPiDataUpdateCoordinator(hass, mock_config_entry)

    coordinator.async_restore_last_push()

    assert coordinator.last_push is None


async def test_restore_last_push_adopts_the_stored_time(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> None:
    """The stored time is adopted once and never overrides a real push."""
    mock_config_entry.add_to_hass(hass)
    entity_id = _register_last_seen(hass, mock_config_entry)
    seen = dt_util.utcnow().replace(microsecond=0) - timedelta(hours=2)
    mock_restore_cache(hass, [State(entity_id, seen.isoformat())])
    coordinator = MeatPiDataUpdateCoordinator(hass, mock_config_entry)

    await coordinator.async_config_entry_first_refresh()
    assert coordinator.last_push == seen
    assert coordinator.device_online is False

    coordinator.handle_webhook_data({"status": {"device_id": "test_device_123"}})
    pushed = coordinator.last_push
    assert pushed is not None
    assert pushed > seen
    coordinator.async_restore_last_push()
    assert coordinator.last_push == pushed
