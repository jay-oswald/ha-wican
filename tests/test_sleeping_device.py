"""A car whose adapter sleeps while it is parked.

A WiCAN in a car goes to sleep a few minutes after the engine stops and
wakes with the car. Two ordinary things followed from that and were
logged as errors (seen on a WiCAN OBD, 2026-11-23 to 11-25):

- Home Assistant started while the car was parked. The registration at
  start could not reach the device: a WARNING per attempt, then "Failed
  to register webhook after 3 attempts. Device may not send updates",
  and a repair issue. The device held the webhook settings it was last
  given, the same ones, and pushed again as soon as the car was driven.
- Five minutes after each parking the health-check found no push and the
  coordinator logged "Error fetching wican data: No data received from
  the device".

A registration that cannot reach a device holding the settings it would
give is now logged at info level, without the repair issue; a device
that lacks them (a first setup, a changed post interval) still gets the
error. Going quiet and pushing again are logged once each at info level.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN
from tests.device_sim import MeatPiDeviceSimulator

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from tests.conftest import MockConfigEntry

pytestmark = [
    pytest.mark.usefixtures("one_registration_attempt"),
    pytest.mark.real_webhook_registration,
]


def _device(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    *,
    reads_replies: bool = False,
) -> MeatPiDeviceSimulator:
    sim = MeatPiDeviceSimulator.from_preset(
        hass, hass_client, "wican_pro_v6", reads_replies=reads_replies,
    )
    sim.attach_api(aioclient_mock)
    # The firmware update entity asks GitHub at every setup.
    aioclient_mock.get(
        "https://api.github.com/repos/meatpiHQ/wican-fw/releases", json=[],
    )
    return sim


def _unreachable_issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"webhook_registration_failed_{entry.entry_id}",
    )


def _loud(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The integration's warnings and errors about the device."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.wican")
        # The firmware releases on GitHub are not the device's business.
        and record.name != "custom_components.wican.github_releases"
        and record.levelno >= logging.WARNING
    ]


def _info(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.wican")
        and record.levelno == logging.INFO
    ]


async def _restart_while_asleep(
    hass: HomeAssistant,
    sim: MeatPiDeviceSimulator,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> MockConfigEntry:
    """Park the car for the night, then set the entry up again."""
    assert sim.api is not None
    assert sim.entry is not None
    freezer.tick(timedelta(hours=8))
    sim.api.mode = "offline"
    caplog.clear()
    assert await hass.config_entries.async_reload(sim.entry.entry_id)
    await hass.async_block_till_done()
    return sim.entry


async def test_start_while_asleep_with_its_settings_is_no_error(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The device was given its settings, then Home Assistant starts at night."""
    caplog.set_level(logging.INFO, logger="custom_components.wican")
    sim = _device(hass, hass_client, aioclient_mock)
    await sim.async_setup(real_registration=True)
    assert sim.firmware.url.endswith(f"/api/webhook/{sim.webhook_id}")
    await sim.push_and_settle(sim.status())

    entry = await _restart_while_asleep(hass, sim, freezer, caplog)

    assert _loud(caplog) == []
    assert _unreachable_issue(hass, entry) is None
    assert any("already holds these webhook settings" in m for m in _info(caplog))


async def test_settings_learned_from_a_push_count_as_held(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A firmware that reads replies got its settings in a reply, never registered."""
    caplog.set_level(logging.INFO, logger="custom_components.wican")
    sim = _device(hass, hass_client, aioclient_mock, reads_replies=True)
    assert sim.api is not None
    sim.api.mode = "offline"
    await sim.async_setup(real_registration=True)
    await sim.push_and_settle(sim.status())
    await sim.push_and_settle(sim.status())
    assert sim.firmware.reply_verdicts
    await sim.push_and_settle(sim.status())

    entry = await _restart_while_asleep(hass, sim, freezer, caplog)

    assert _loud(caplog) == []
    assert _unreachable_issue(hass, entry) is None


async def test_start_without_settings_is_still_an_error(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A device never given its settings cannot push: that is worth an error."""
    sim = _device(hass, hass_client, aioclient_mock)
    assert sim.api is not None
    sim.api.mode = "offline"

    entry = await sim.async_setup(real_registration=True)

    assert any("Failed to register webhook" in m for m in _loud(caplog))
    assert _unreachable_issue(hass, entry) is not None


async def test_changed_settings_for_a_sleeping_device_stay_an_error(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new post interval the device does not hold yet is still reported."""
    sim = _device(hass, hass_client, aioclient_mock)
    entry = await sim.async_setup(real_registration=True)
    await sim.push_and_settle(sim.status())
    assert sim.api is not None
    freezer.tick(timedelta(hours=8))
    sim.api.mode = "offline"
    caplog.clear()

    hass.config_entries.async_update_entry(entry, options={CONF_POST_INTERVAL: 30})
    await hass.async_block_till_done()

    assert any("Failed to register webhook" in m for m in _loud(caplog))
    assert _unreachable_issue(hass, entry) is not None


async def test_going_quiet_and_coming_back_are_info(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The car is parked, the health-check fires on its own timer, then a drive."""
    caplog.set_level(logging.INFO, logger="custom_components.wican")
    sim = _device(hass, hass_client, aioclient_mock)
    await sim.async_setup(real_registration=True)
    await sim.push_and_settle(sim.status())
    caplog.clear()

    for _ in range(3):
        freezer.tick(timedelta(minutes=5, seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert sim.coordinator.last_update_success is False
    assert _loud(caplog) == []
    quiet = [m for m in _info(caplog) if "has not pushed" in m]
    assert len(quiet) == 1

    await sim.push(sim.status())
    await hass.async_block_till_done()

    assert sim.coordinator.last_update_success is True
    assert _loud(caplog) == []
    assert [m for m in _info(caplog) if "is pushing again" in m] != []
