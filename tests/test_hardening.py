"""Targeted tests for the defensive layers added for unbreakability.

These exercise the guard branches directly — listener isolation, sanitizer
edge types, lifecycle races — complementing the end-to-end scenarios in
``test_device_chaos.py``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican import _async_request_webhook_registration
from custom_components.wican.const import DOMAIN
from custom_components.wican.sensor import (
    DYNAMIC_PID_SENSORS,
    _build_pid_entity_description,
    _coerce_numeric_value,
    _normalize_device_class,
    _trimmed_pid_config,
)
from tests.device_sim import MeatPiDeviceSimulator

if TYPE_CHECKING:
    import pytest

# ===================================================================
# Coordinator guards
# ===================================================================


async def test_listener_isolation_lets_later_listeners_run(
    device: MeatPiDeviceSimulator,
) -> None:
    """A raising listener must not stop listeners registered after it."""
    await device.async_setup()
    ran: list[str] = []

    def _raiser() -> None:
        ran.append("raiser")
        raise RuntimeError("entity blew up")

    def _recorder() -> None:
        ran.append("recorder")

    # Registered in order: the raiser runs first.
    device.coordinator.async_add_listener(_raiser)
    device.coordinator.async_add_listener(_recorder)

    device.coordinator.handle_webhook_data({"status": {"uptime": "x"}})
    assert ran == ["raiser", "recorder"]


async def test_notify_failure_does_not_abort_webhook_flow(
    device: MeatPiDeviceSimulator,
) -> None:
    """If notifying entities blows up entirely, the handler still finishes.

    Device-info persistence and PID discovery run after the notify call, so
    the push must still return 204 and persist the reported firmware version.
    """
    await device.async_setup()
    device.reboot(fw_version="4.77")
    with patch.object(
        device.coordinator,
        "async_set_updated_data",
        side_effect=RuntimeError("machinery failure"),
    ):
        resp = await device.push(device.status())
    assert resp.status == HTTPStatus.NO_CONTENT
    await device.hass.async_block_till_done()
    assert device.entry.data["fw_version"] == "4.77"


async def test_coordinator_ignores_non_dict_data(
    device: MeatPiDeviceSimulator,
) -> None:
    """Non-object data handed to the coordinator directly is ignored."""
    await device.async_setup()
    before = dict(device.coordinator.data or {})
    device.coordinator.handle_webhook_data(["not", "a", "dict"])  # type: ignore[arg-type]
    assert (device.coordinator.data or {}) == before


async def test_legacy_numeric_stored_device_id_still_matches(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """An old entry that stored the id as a number matches a string id."""
    await device.async_setup()
    hass.config_entries.async_update_entry(
        device.entry, data={**device.entry.data, "device_id": 4242},
    )
    await hass.async_block_till_done()

    resp = await device.push({"status": {"device_id": "4242", "uptime": "ok"}})
    assert resp.status == HTTPStatus.NO_CONTENT


async def test_normalize_keeps_exotic_digit_strings(
    device: MeatPiDeviceSimulator,
) -> None:
    """Unicode digits that pass isdigit() but fail int() stay untouched."""
    await device.async_setup()
    assert device.coordinator.normalize_sensor_value("some_key", "²") == "²"


# ===================================================================
# Sensor helper guards (pure unit tests)
# ===================================================================


def test_normalize_device_class_rejects_non_string() -> None:
    assert _normalize_device_class(123, None, "PID") is None  # type: ignore[arg-type]
    assert _normalize_device_class({"a": 1}, None, "PID") is None  # type: ignore[arg-type]


def test_trimmed_pid_config_handles_non_dict() -> None:
    assert _trimmed_pid_config("junk") == {}
    assert _trimmed_pid_config(None) == {}
    assert _trimmed_pid_config({"unit": "V", "class": "voltage", "x": 1}) == {
        "unit": "V",
        "class": "voltage",
    }


def test_build_pid_description_handles_non_dict_config() -> None:
    description = _build_pid_entity_description("SOME_PID", "not-a-dict")
    assert description.key == "SOME_PID"
    assert description.native_unit_of_measurement is None


def test_coerce_numeric_value_edge_types() -> None:
    assert _coerce_numeric_value(Decimal("1.5")) == 1.5
    assert _coerce_numeric_value(Decimal("NaN")) is None
    assert _coerce_numeric_value("1e999") is None  # parses to inf
    assert _coerce_numeric_value("nan") is None
    assert _coerce_numeric_value(True) is None
    assert _coerce_numeric_value(object()) is None
    assert _coerce_numeric_value(float("inf")) is None
    assert _coerce_numeric_value("  42 ") == 42


# ===================================================================
# PID lifecycle races and restore hygiene
# ===================================================================


async def test_corrupt_entries_in_stored_pid_keys_are_skipped(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """Junk entries inside a stored pid_keys list are skipped on restore."""
    await device.async_setup()
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        hass.config_entries.async_update_entry(
            device.entry,
            data={
                **device.entry.data,
                "pid_keys": ["GOOD", "", 123, "K" * 500],
                "config": {},
            },
        )
        await hass.async_block_till_done()

    await device.async_reload()
    assert set(DYNAMIC_PID_SENSORS[device.entry.entry_id]) == {"GOOD"}


async def test_dispatcher_junk_payloads_are_ignored(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """Junk sent over the shared dispatcher signal never breaks PID discovery."""
    await device.async_setup()
    webhook_id = device.entry.runtime_data.webhook_id

    # Entirely non-dict payload.
    async_dispatcher_send(hass, DOMAIN, webhook_id, "not-a-dict")
    await hass.async_block_till_done()

    # Valid autopid with a non-dict config section.
    async_dispatcher_send(
        hass, DOMAIN, webhook_id, {"autopid_data": {"XPID": 5}, "config": "junk"},
    )
    await hass.async_block_till_done()
    assert "XPID" in DYNAMIC_PID_SENSORS[device.entry.entry_id]


async def test_pid_update_after_unload_race_is_harmless(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A PID task that runs after the entry's state is gone does nothing."""
    await device.async_setup()
    webhook_id = device.entry.runtime_data.webhook_id

    # Simulate the unload race: bookkeeping removed while a dispatch is in flight.
    DYNAMIC_PID_SENSORS.pop(device.entry.entry_id)
    async_dispatcher_send(
        hass, DOMAIN, webhook_id, {"autopid_data": {"LATE": 1}, "config": {}},
    )
    await hass.async_block_till_done()
    assert device.entry.entry_id not in DYNAMIC_PID_SENSORS


async def test_pid_numeric_string_latches_statistics(
    device: MeatPiDeviceSimulator,
) -> None:
    """A numeric-as-string PID value is coerced and latches measurement."""
    await device.async_setup()
    await device.push_and_settle(
        device.pids({"FUEL": "47.5"}, {"FUEL": {"unit": "%", "class": ""}}),
    )
    state = device.hass.states.get("sensor.wican_sim_fuel")
    assert state.state == "47.5"
    assert state.attributes.get("state_class") == "measurement"


async def test_restored_garbage_status_value_is_dropped(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A garbage value restored from a pre-hardening install is discarded."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import (
        mock_restore_cache_with_extra_data,
    )

    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State("sensor.wican_sim_battery_voltage", "garbage"),
                {"native_value": "garbage", "native_unit_of_measurement": "V"},
            ),
        ),
    )

    device = MeatPiDeviceSimulator(hass, hass_client)
    await device.async_setup()

    state = hass.states.get("sensor.wican_sim_battery_voltage")
    assert state is not None
    assert state.state == "unknown"


# ===================================================================
# Registration lifecycle
# ===================================================================


async def test_startup_deferred_registration_fires_once_started(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """With HA still starting, registration waits for the started event."""
    hass.set_state(CoreState.not_running)
    device = MeatPiDeviceSimulator(hass, hass_client)
    await device.async_setup()

    register = AsyncMock(return_value=True)
    with patch(
        "custom_components.wican._async_register_webhook_on_device", register,
    ):
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

    assert register.await_count == 1


async def test_unload_before_startup_cancels_deferred_registration(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """Unloading before HA finishes starting cancels the pending listener."""
    hass.set_state(CoreState.not_running)
    device = MeatPiDeviceSimulator(hass, hass_client)
    await device.async_setup()

    assert await hass.config_entries.async_unload(device.entry.entry_id)
    await hass.async_block_till_done()

    register = AsyncMock(return_value=True)
    with patch(
        "custom_components.wican._async_register_webhook_on_device", register,
    ):
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

    # The listener was removed on unload: nothing fires.
    assert register.await_count == 0


async def test_unload_while_the_started_event_fires_is_quiet(
    hass: HomeAssistant, hass_client: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry unloaded by another listener of the started event stays quiet.

    A rediscovery that changes the entry's address reloads it while Home
    Assistant fires its started event. The deferred registration's listener
    was then removed although it was already taken for that event: an
    "Unable to remove unknown job listener" error at every start (seen on
    real instances), and the registration ran for the unloaded entry.
    """
    hass.set_state(CoreState.not_running)
    cancel: list[Any] = []

    @callback
    def _unload_first(_event: Any) -> None:
        # Listening before the entry is set up, so called before the
        # integration's own listener: what an unload runs at its end.
        for run in cancel:
            run()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _unload_first)
    device = MeatPiDeviceSimulator(hass, hass_client)
    with patch.object(
        MockConfigEntry, "async_on_unload", autospec=True,
        side_effect=lambda _entry, func: cancel.append(func),
    ):
        await device.async_setup()

    register = AsyncMock(return_value=True)
    with patch(
        "custom_components.wican._async_register_webhook_on_device", register,
    ):
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

    assert "Unable to remove unknown job listener" not in caplog.text
    assert register.await_count == 0


async def test_registration_request_after_unload_is_a_noop(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A late registration request against an unloaded entry does nothing."""
    await device.async_setup()
    entry = device.entry
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # runtime_data is gone; the request must simply return.
    await _async_request_webhook_registration(hass, entry)


async def test_push_racing_unload_gets_503_and_no_error(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A push whose body read is in flight during unload is dropped cleanly.

    Regression: the handler resumed into deleted runtime_data and raised
    AttributeError, which HA's webhook component swallowed while answering
    200 — the device believed delivery succeeded and the push was lost.
    A 503 makes the device retry after the reload.
    """
    await device.async_setup()
    entry = device.entry

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def _slow_read(request):
        entered.set()
        await gate.wait()
        return {"status": {"device_id": device.device_id, "uptime": "x"}}

    with patch(
        "custom_components.wican._async_read_webhook_json",
        side_effect=_slow_read,
    ):
        push_task = asyncio.ensure_future(device.push({"ignored": True}))
        await asyncio.wait_for(entered.wait(), timeout=5)

        # Unload while the handler is awaiting the body.
        assert await hass.config_entries.async_unload(entry.entry_id)
        gate.set()
        resp = await asyncio.wait_for(push_task, timeout=5)

    assert resp.status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_unload_during_running_registration_is_clean(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """Unloading mid-registration neither raises nor re-runs the loop.

    Regression: a registration attempt spanning an unload (retries and
    backoff can take seconds against an offline device) re-entered
    entry.runtime_data unguarded on the coalescing loop's trailing
    iteration and raised AttributeError in a fire-and-forget task.
    """
    await device.async_setup()
    entry = device.entry

    entered = asyncio.Event()
    gate = asyncio.Event()
    calls = 0

    async def _blocked_register(*args, **kwargs) -> bool:
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        return False

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        side_effect=_blocked_register,
    ):
        reg_task = asyncio.ensure_future(
            _async_request_webhook_registration(hass, entry),
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # A push requests another registration while one is running,
        # then the entry is unloaded before the attempt finishes.
        entry.runtime_data.registration_pending = True
        assert await hass.config_entries.async_unload(entry.entry_id)
        gate.set()
        await asyncio.wait_for(reg_task, timeout=5)

    # The trailing re-run must not have happened after the unload.
    assert calls == 1


async def test_failed_platform_unload_keeps_webhook_registered(
    device: MeatPiDeviceSimulator, hass: HomeAssistant,
) -> None:
    """A failed platform unload leaves the entry loaded AND reachable.

    Regression (L9): the webhook was unregistered before platforms
    unloaded; if that unload failed, the entry stayed loaded but was
    deaf to pushes.
    """
    from custom_components.wican import async_unload_entry

    await device.async_setup()
    entry = device.entry
    webhook_id = entry.data["webhook_id"]
    assert webhook_id in hass.data["webhook"]

    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False,
    ):
        assert await async_unload_entry(hass, entry) is False

    # Entry still loaded and still receiving pushes.
    assert webhook_id in hass.data["webhook"]
    resp = await device.push(
        {"status": {"device_id": device.device_id, "uptime": "ok"}},
    )
    assert resp.status == HTTPStatus.NO_CONTENT
