"""End-to-end device simulation tests for the WiCAN integration.

These tests drive the integration through a lightweight simulated WiCAN device
(:class:`MeatPiDeviceSimulator`) that exercises the *real* code paths:

* the device pushes data to Home Assistant's actual webhook endpoint via
  ``hass_client`` (so the real ``handle_webhook`` handler, coordinator, and
  entities run), and
* Home Assistant registers / OTA-updates the device through a configurable mock
  aiohttp session.

The goal is robustness: feed the integration valid data, partial data, malformed
data, hostile data, and failure conditions, and assert it never crashes and
always degrades gracefully.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientError

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN

from tests.conftest import MockConfigEntry
from tests.device_sim import MeatPiDeviceSimulator


# ===================================================================
# Group A — device pushes data to HA (real webhook handler)
# ===================================================================


async def test_happy_path_full_payload(device: MeatPiDeviceSimulator) -> None:
    """A full, well-formed payload populates entities."""
    await device.async_setup()
    resp = await device.push_and_settle(
        {
            **device.status(),
            **device.pids(
                {"rpm": 1500, "speed": 65},
                {"rpm": {"unit": "rpm", "class": ""},
                 "speed": {"unit": "km/h", "class": "speed"}},
            ),
            **device.gps(37.7749, -122.4194, accuracy=10),
        },
    )
    assert resp.status == HTTPStatus.NO_CONTENT

    data = device.coordinator.data
    assert data["status"]["batt_voltage"] == "12.6V"
    assert data["autopid_data"]["rpm"] == 1500
    assert data["gps"]["latitude"] == 37.7749


async def test_invalid_json_body_is_rejected(device: MeatPiDeviceSimulator) -> None:
    """A non-JSON body returns 422 and does not crash the handler."""
    await device.async_setup()
    resp = await device.push(raw="this is not json {{{")
    assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY
    # Integration is still alive: a good push afterwards still works.
    good = await device.push(device.status())
    assert good.status == HTTPStatus.NO_CONTENT


@pytest.mark.parametrize("body", ["[1, 2, 3]", '"a string"', "42", "true", "null"])
async def test_non_object_json_is_rejected(
    device: MeatPiDeviceSimulator, body: str,
) -> None:
    """Valid JSON that is not an object returns 422 and does not crash."""
    await device.async_setup()
    resp = await device.push(raw=body)
    assert resp.status == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_empty_object_is_accepted(device: MeatPiDeviceSimulator) -> None:
    """An empty JSON object is accepted (no data, no crash)."""
    await device.async_setup()
    resp = await device.push({})
    assert resp.status == HTTPStatus.NO_CONTENT
    # An empty push still counts as a sign of life for the health check.
    await device.hass.async_block_till_done()
    await device.coordinator.async_refresh()
    assert device.coordinator.last_update_success is True


async def test_status_wrong_type_is_ignored(device: MeatPiDeviceSimulator) -> None:
    """A ``status`` that is not an object is ignored gracefully."""
    await device.async_setup()
    resp = await device.push_and_settle({"status": "not-a-dict"})
    assert resp.status == HTTPStatus.NO_CONTENT


async def test_autopid_and_config_wrong_types(device: MeatPiDeviceSimulator) -> None:
    """Malformed autopid_data/config types never crash entity creation."""
    await device.async_setup()
    resp = await device.push_and_settle(
        {"autopid_data": ["not", "a", "dict"], "config": "nope"},
    )
    assert resp.status == HTTPStatus.NO_CONTENT
    # A subsequent well-formed PID push still creates a sensor.
    await device.push_and_settle(
        device.pids({"rpm": 900}, {"rpm": {"unit": "rpm", "class": ""}}),
    )
    assert device.hass.states.get("sensor.wican_sim_rpm") is not None


async def test_partial_status_only(device: MeatPiDeviceSimulator) -> None:
    """A status-only push works with no PID or GPS sections."""
    await device.async_setup()
    resp = await device.push_and_settle(device.status(batt_voltage="11.9V"))
    assert resp.status == HTTPStatus.NO_CONTENT
    state = device.hass.states.get("sensor.wican_sim_battery_voltage")
    assert state is not None
    assert state.state == "11.9"


async def test_partial_gps_only(device: MeatPiDeviceSimulator) -> None:
    """A GPS-only push makes the tracker available."""
    await device.async_setup()
    await device.push_and_settle(device.gps(51.5, -0.12, accuracy=5))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state is not None
    assert state.attributes["latitude"] == 51.5


async def test_gps_out_of_range_is_ignored(device: MeatPiDeviceSimulator) -> None:
    """Out-of-range GPS coordinates leave the tracker unavailable."""
    await device.async_setup()
    await device.push_and_settle(device.gps(999.0, 999.0))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state is not None
    assert state.state == "unavailable"


async def test_gps_wrong_types_do_not_crash(device: MeatPiDeviceSimulator) -> None:
    """Non-numeric / wrong-typed GPS values are handled without crashing."""
    await device.async_setup()
    resp = await device.push_and_settle(
        {"gps": {"latitude": "abc", "longitude": None, "accuracy": {"x": 1}}},
    )
    assert resp.status == HTTPStatus.NO_CONTENT
    # No garbage leaked into the tracker: it stays unavailable (no valid fix).
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state is not None
    assert state.state == "unavailable"
    # gps as a non-dict entirely:
    resp2 = await device.push_and_settle({"gps": "not-a-dict"})
    assert resp2.status == HTTPStatus.NO_CONTENT
    # A valid fix afterwards still works.
    await device.push_and_settle(device.gps(48.1, 11.6, accuracy=5))
    state = device.hass.states.get("device_tracker.wican_sim_location")
    assert state.attributes["latitude"] == 48.1


async def test_battery_voltage_formats(device: MeatPiDeviceSimulator) -> None:
    """Various battery-voltage string formats normalize to a float."""
    await device.async_setup()
    for raw_value, expected in (
        ("12.5V", "12.5"),
        ("12.5 V", "12.5"),
        ("12.5v", "12.5"),
        (" 13 V ", "13.0"),
        (13.2, "13.2"),
    ):
        await device.push_and_settle(device.status(batt_voltage=raw_value))
        state = device.hass.states.get("sensor.wican_sim_battery_voltage")
        assert state.state == expected


async def test_hostile_value_types_do_not_crash(device: MeatPiDeviceSimulator) -> None:
    """Nested objects / wrong types in status values do not crash the handler."""
    await device.async_setup()
    resp = await device.push_and_settle(
        {
            "status": {
                "device_id": device.device_id,
                "batt_voltage": {"nested": "object"},
                "ecu_status": ["a", "list"],
                "uptime": 12345,
            },
        },
    )
    assert resp.status == HTTPStatus.NO_CONTENT
    # The valid sibling value in the same push was still applied...
    assert device.hass.states.get("sensor.wican_sim_uptime").state == "12345"
    # ...while the hostile values were dropped rather than written.
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unknown"


async def test_unicode_and_special_chars(device: MeatPiDeviceSimulator) -> None:
    """Unicode / special characters in values are handled."""
    await device.async_setup()
    resp = await device.push_and_settle(
        device.status(vpn_status="Connecté ✓ — 日本語"),
    )
    assert resp.status == HTTPStatus.NO_CONTENT
    state = device.hass.states.get("sensor.wican_sim_vpn_status")
    assert state.state == "Connecté ✓ — 日本語"


async def test_large_pid_payload(device: MeatPiDeviceSimulator) -> None:
    """A large batch of PIDs is all processed."""
    await device.async_setup()
    values = {f"pid_{i:03d}": i for i in range(120)}
    config = {k: {"unit": "", "class": ""} for k in values}
    await device.push_and_settle(device.pids(values, config))
    assert device.hass.states.get("sensor.wican_sim_pid_000") is not None
    assert device.hass.states.get("sensor.wican_sim_pid_119") is not None


async def test_high_volume_cell_voltages_disabled_by_default(
    device: MeatPiDeviceSimulator,
) -> None:
    """Per-cell HV voltage PIDs are created disabled by default."""
    from homeassistant.helpers import entity_registry as er

    await device.async_setup()
    await device.push_and_settle(
        device.pids(
            {"HV_C_V_001": 3.8, "SPEED": 40},
            {"HV_C_V_001": {"unit": "V", "class": "battery"},
             "SPEED": {"unit": "km/h", "class": "speed"}},
        ),
    )
    reg = er.async_get(device.hass)
    cell = reg.async_get("sensor.wican_sim_hv_c_v_001")
    assert cell is not None
    assert cell.disabled_by is not None  # disabled by default
    # A normal PID stays enabled.
    assert device.hass.states.get("sensor.wican_sim_speed") is not None


async def test_rapid_successive_pushes(device: MeatPiDeviceSimulator) -> None:
    """Many quick pushes are all handled and the latest value wins."""
    await device.async_setup()
    for voltage in range(10):
        resp = await device.push(device.status(batt_voltage=f"{12 + voltage * 0.1:.1f}V"))
        assert resp.status == HTTPStatus.NO_CONTENT
    await device.hass.async_block_till_done()
    await device.coordinator.async_refresh()
    await device.hass.async_block_till_done()
    state = device.hass.states.get("sensor.wican_sim_battery_voltage")
    assert state.state == "12.9"


# ===================================================================
# Group B — device identity
# ===================================================================


async def test_identity_mismatch_rejected(device: MeatPiDeviceSimulator) -> None:
    """A push from a different device_id is rejected with 403."""
    await device.async_setup()
    resp = await device.push(
        {"status": {"device_id": "some_other_device", "batt_voltage": "12.0V"}},
    )
    assert resp.status == HTTPStatus.FORBIDDEN


async def test_first_device_id_is_learned(hass: HomeAssistant, hass_client: Any) -> None:
    """When no device_id is stored, the first one seen is accepted."""
    device = MeatPiDeviceSimulator(hass, hass_client, device_id="learned_id")
    await device.async_setup(store_device_id=False)
    resp = await device.push({"status": {"device_id": "learned_id", "batt_voltage": "12.1V"}})
    assert resp.status == HTTPStatus.NO_CONTENT
    # A different device is then rejected.
    resp2 = await device.push({"status": {"device_id": "intruder", "batt_voltage": "12.1V"}})
    assert resp2.status == HTTPStatus.FORBIDDEN


async def test_missing_device_id_skips_validation(device: MeatPiDeviceSimulator) -> None:
    """Legacy pushes without a device_id are accepted (no validation)."""
    await device.async_setup()
    resp = await device.push_and_settle({"status": {"batt_voltage": "12.2V"}})
    assert resp.status == HTTPStatus.NO_CONTENT


# ===================================================================
# Group C — availability / recovery
# ===================================================================


async def test_device_goes_unavailable_then_recovers(
    device: MeatPiDeviceSimulator,
) -> None:
    """Entities go unavailable when the device stops pushing, then recover."""
    await device.async_setup()
    await device.push_and_settle(device.status(batt_voltage="12.4V"))
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.4"

    # Device stops pushing -> the health-check marks it stale.
    device.go_stale()
    await device.coordinator.async_refresh()
    await device.hass.async_block_till_done()
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "unavailable"
    assert device.coordinator.last_update_success is False

    # A fresh push restores availability.
    await device.push_and_settle(device.status(batt_voltage="12.7V"))
    assert device.coordinator.last_update_success is True
    assert device.hass.states.get("sensor.wican_sim_battery_voltage").state == "12.7"


async def test_binary_sensor_truthiness(device: MeatPiDeviceSimulator) -> None:
    """ECU/BLE binary sensors interpret the device's truthy strings."""
    await device.async_setup()
    await device.push_and_settle(device.status(ecu_status="online", ble_status="enable"))
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "on"
    assert device.hass.states.get("binary_sensor.wican_sim_bluetooth_enabled").state == "on"

    await device.push_and_settle(device.status(ecu_status="Offline", ble_status="disabled"))
    assert device.hass.states.get("binary_sensor.wican_sim_ecu_online").state == "off"


# ===================================================================
# Group D — HA -> device registration / connection failures
# ===================================================================


def _mock_session_response(status: int, text: str = "OK") -> Mock:
    resp = Mock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=text.encode())
    return resp


async def _setup_entry_for_registration(hass: HomeAssistant) -> MockConfigEntry:
    """Set up an entry (registration mocked) so runtime_data exists.

    Uses a host-only entry so the real registration has a single endpoint,
    and skips the real registration during setup so it doesn't consume the
    per-test mock session.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Reg",
        data={CONF_WEBHOOK_ID: "wid_reg", "host": "http://192.168.1.5", "device_id": "d1"},
        options={CONF_POST_INTERVAL: 15},
        unique_id="regdevice",
        # As the config flow makes an entry now.
        version=1,
        minor_version=3,
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.async_update_params_from_github",
            AsyncMock(return_value=False),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.real_webhook_registration
async def test_registration_retries_then_succeeds(hass: HomeAssistant) -> None:
    """A transient failure is retried and eventually succeeds."""
    from custom_components.wican import _async_register_webhook_on_device

    entry = await _setup_entry_for_registration(hass)
    calls: list[str] = []

    async def fail_once_then_ok(url: str, **_kw: Any) -> Mock:
        calls.append(url)
        if len(calls) == 1:
            raise ClientError("boom")
        return _mock_session_response(HTTPStatus.OK)

    session = Mock()
    session.post = AsyncMock(side_effect=fail_once_then_ok)
    with (
        patch("custom_components.wican.async_get_clientsession", return_value=session),
        patch("custom_components.wican.asyncio.sleep", AsyncMock()),
    ):
        result = await _async_register_webhook_on_device(hass, entry, max_retries=3)
    assert result is True
    assert len(calls) >= 2  # it retried after the first failure


@pytest.mark.real_webhook_registration
async def test_registration_failure_raises_repair(hass: HomeAssistant) -> None:
    """When registration exhausts retries, a repair issue is raised."""
    from custom_components.wican import (
        _async_register_webhook_on_device,
        _webhook_repair_issue_id,
    )

    entry = await _setup_entry_for_registration(hass)
    session = Mock()
    session.post = AsyncMock(side_effect=ClientError("refused"))
    with (
        patch("custom_components.wican.async_get_clientsession", return_value=session),
        patch("custom_components.wican.asyncio.sleep", AsyncMock()),
    ):
        result = await _async_register_webhook_on_device(hass, entry, max_retries=1)
    assert result is False
    reg = ir.async_get(hass)
    assert reg.async_get_issue(DOMAIN, _webhook_repair_issue_id(entry)) is not None


@pytest.mark.real_webhook_registration
async def test_registration_http_error_status(hass: HomeAssistant) -> None:
    """A device returning HTTP 500 to registration is treated as failure."""
    from custom_components.wican import _async_register_webhook_on_device

    entry = await _setup_entry_for_registration(hass)
    session = Mock()
    session.post = AsyncMock(
        return_value=_mock_session_response(HTTPStatus.INTERNAL_SERVER_ERROR, "err"),
    )
    with (
        patch("custom_components.wican.async_get_clientsession", return_value=session),
        patch("custom_components.wican.asyncio.sleep", AsyncMock()),
    ):
        result = await _async_register_webhook_on_device(hass, entry, max_retries=1)
    assert result is False


async def test_unload_is_clean(device: MeatPiDeviceSimulator) -> None:
    """The entry unloads cleanly and the webhook is torn down."""
    await device.async_setup()
    await device.push_and_settle(device.status())
    assert await device.hass.config_entries.async_unload(device.entry.entry_id)
    await device.hass.async_block_till_done()
    # The webhook is unregistered: a push now no longer reaches a handler.
    resp = await device.push(device.status())
    assert resp.status in (HTTPStatus.OK, HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED)


async def test_existing_tracker_entity_id_is_preserved(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """Migration safety: an existing install keeps its tracker entity_id.

    Older releases named the tracker's device "WiCAN Device", so existing users
    have ``device_tracker.wican_device_location`` in their registry. After the
    device name change to the entry title, that entity_id must be preserved
    (HA pins entity_ids at creation) while a *new* install would get the
    title-based id.
    """
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="My Car",  # a real, non-default title
        data={
            CONF_WEBHOOK_ID: "wid_mig",
            "mdns": "http://wican_mig.local",
            "host": "http://192.168.1.9",
            "mac": "CC:CC:CC:CC:CC:CC",
            "device_id": "mig_dev",
        },
        options={CONF_POST_INTERVAL: 15},
        unique_id="cccccccccccc",
    )
    entry.add_to_hass(hass)

    # Simulate the entity already registered by an older version (legacy id).
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.entry_id}_device_tracker",
        suggested_object_id="wican_device_location",
        config_entry=entry,
    )

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The tracker reuses the legacy entity_id, NOT device_tracker.my_car_location.
    resolved = reg.async_get_entity_id(
        "device_tracker", DOMAIN, f"{entry.entry_id}_device_tracker",
    )
    assert resolved == "device_tracker.wican_device_location"
    assert hass.states.get("device_tracker.my_car_location") is None


async def test_existing_install_new_entity_is_title_consistent(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A NEW entity added on an existing install uses the title-based scheme.

    Existing users keep their pinned legacy entity_ids, but any entity created
    afterwards (e.g. a newly-reported PID) is registered fresh against the
    (title-named) device, so it is consistent with a brand-new install and does
    not disturb the existing entities.
    """
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="My Car",
        data={
            CONF_WEBHOOK_ID: "wid_new_ent",
            "mdns": "http://wican_ne.local",
            "host": "http://192.168.1.11",
            "mac": "EE:EE:EE:EE:EE:EE",
            "device_id": "ne_dev",
        },
        options={CONF_POST_INTERVAL: 15},
        unique_id="eeeeeeeeeeee",
    )
    entry.add_to_hass(hass)

    # Legacy tracker id from an older release.
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.entry_id}_device_tracker",
        suggested_object_id="wican_device_location",
        config_entry=entry,
    )

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The device now reports a brand-new PID that this install has never seen.
    client = await hass_client()
    await client.post(
        "/api/webhook/wid_new_ent",
        json={
            "status": {"device_id": "ne_dev"},
            "autopid_data": {"rpm": 1500},
            "config": {"rpm": {"unit": "rpm", "class": ""}},
        },
    )
    await hass.async_block_till_done()

    # The new PID sensor follows the title, consistent with a fresh install...
    assert hass.states.get("sensor.my_car_rpm") is not None
    # ...while the pre-existing tracker keeps its legacy entity_id.
    assert reg.async_get_entity_id(
        "device_tracker", DOMAIN, f"{entry.entry_id}_device_tracker",
    ) == "device_tracker.wican_device_location"


async def test_new_tracker_entity_id_follows_title(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A fresh install gets the consistent, title-based tracker entity_id."""
    dev = MeatPiDeviceSimulator(
        hass, hass_client, title="My Car", webhook_id="wid_new",
        device_id="new_dev", mac="DD:DD:DD:DD:DD:DD", hostname="wican_new.local",
    )
    await dev.async_setup()
    # New registry entry -> id derived from the "My Car" device name.
    assert hass.states.get("device_tracker.my_car_location") is not None


async def test_multi_device_isolation(hass: HomeAssistant, hass_client: Any) -> None:
    """Two devices push independently without cross-contaminating entities."""
    dev_a = MeatPiDeviceSimulator(
        hass, hass_client, title="WiCAN A", webhook_id="wid_a",
        device_id="dev_a", mac="AA:AA:AA:AA:AA:AA", hostname="wican_a.local",
    )
    dev_b = MeatPiDeviceSimulator(
        hass, hass_client, title="WiCAN B", webhook_id="wid_b",
        device_id="dev_b", mac="BB:BB:BB:BB:BB:BB", hostname="wican_b.local",
    )
    await dev_a.async_setup()
    await dev_b.async_setup()

    await dev_a.push_and_settle(
        dev_a.pids({"rpm": 1000}, {"rpm": {"unit": "rpm", "class": ""}}),
    )
    await dev_b.push_and_settle(
        dev_b.pids({"speed": 55}, {"speed": {"unit": "km/h", "class": "speed"}}),
    )

    assert hass.states.get("sensor.wican_a_rpm") is not None
    assert hass.states.get("sensor.wican_b_speed") is not None
    # Device A did not gain device B's PID and vice-versa.
    assert hass.states.get("sensor.wican_a_speed") is None
    assert hass.states.get("sensor.wican_b_rpm") is None
