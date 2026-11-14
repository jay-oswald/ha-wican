"""A device that pushes but that Home Assistant cannot reach.

Everything Home Assistant asks of a device starts at Home Assistant: the
webhook registration, the capability probe, the buttons. A car that is
asleep when Home Assistant starts, or one on LTE without a VPN, cannot be
reached that way, although the car itself reaches Home Assistant with
every push.

Two things went wrong with such a device (reproduced 2026-11-14):

- The "device unreachable" repair issue of a failed registration stayed
  open while data was flowing, and no further registration was attempted.
- A post interval changed in the options never reached the device.

A push now closes the issue. A firmware that reads push replies gets the
settings in the reply to its own push (tests/test_push_reply.py has the
details of that exchange); an older firmware gets a new registration
attempt when it pushes.

Everything runs through the device simulator and the integration's real
registration: nothing is stubbed between Home Assistant and the device.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN
from tests.device_sim import MeatPiDeviceSimulator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from tests.conftest import MockConfigEntry

# These tests drive the integration's own registration (a device that
# cannot be reached, a revision in the payload): the conftest mock of the
# registration stays out, and one attempt is enough.
pytestmark = [
    pytest.mark.usefixtures("one_registration_attempt"),
    pytest.mark.real_webhook_registration,
]

REGISTRATION = ("POST", "/api/webhook")


def _device(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    *,
    reads_replies: bool,
) -> MeatPiDeviceSimulator:
    """A WiCAN PRO with its HTTP API wired up, on either firmware generation."""
    sim = MeatPiDeviceSimulator.from_preset(
        hass, hass_client, "wican_pro_v6", reads_replies=reads_replies,
    )
    sim.attach_api(aioclient_mock)
    return sim


def _unreachable_issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"webhook_registration_failed_{entry.entry_id}",
    )


@pytest.mark.parametrize("reads_replies", [False, True])
async def test_push_closes_the_unreachable_issue(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    reads_replies: bool,
) -> None:
    """The device was asleep when Home Assistant started, then it pushes."""
    sim = _device(hass, hass_client, aioclient_mock, reads_replies=reads_replies)
    assert sim.api is not None
    sim.api.mode = "offline"
    entry = await sim.async_setup(real_registration=True)
    assert _unreachable_issue(hass, entry) is not None

    resp = await sim.push_and_settle(sim.status())

    assert resp.status in (HTTPStatus.OK, HTTPStatus.NO_CONTENT)
    assert _unreachable_issue(hass, entry) is None


async def test_push_retries_the_failed_registration(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A firmware that does not read replies is registered again when it pushes."""
    sim = _device(hass, hass_client, aioclient_mock, reads_replies=False)
    assert sim.api is not None
    sim.api.mode = "offline"
    entry = await sim.async_setup(real_registration=True)
    assert sim.firmware.registrations == []
    attempts = sim.api.count(*REGISTRATION)

    sim.api.mode = "ok"
    await sim.push_and_settle(sim.status())

    assert sim.api.count(*REGISTRATION) > attempts
    assert sim.firmware.url.endswith(f"/api/webhook/{sim.webhook_id}")
    assert sim.firmware.interval == 15
    assert _unreachable_issue(hass, entry) is None


async def test_interval_change_reaches_a_device_that_reads_replies(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The car is on LTE: the new interval travels in the reply to its push."""
    sim = _device(hass, hass_client, aioclient_mock, reads_replies=True)
    entry = await sim.async_setup(real_registration=True)
    await sim.push_and_settle(sim.status())
    await sim.push_and_settle(sim.status())
    assert sim.firmware.interval == 15

    sim.leave_home()
    hass.config_entries.async_update_entry(entry, options={CONF_POST_INTERVAL: 30})
    await hass.async_block_till_done()
    assert sim.firmware.interval == 15

    await sim.push_and_settle(sim.status())

    assert sim.firmware.interval == 30
    resp = await sim.push_and_settle(sim.status())
    assert resp.status == HTTPStatus.NO_CONTENT
    assert _unreachable_issue(hass, entry) is None


async def test_interval_change_reaches_an_older_firmware_once_it_is_back(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """An older firmware gets the new interval by registration, on a push."""
    sim = _device(hass, hass_client, aioclient_mock, reads_replies=False)
    entry = await sim.async_setup(real_registration=True)
    await sim.push_and_settle(sim.status())
    assert sim.firmware.interval == 15

    sim.leave_home()
    hass.config_entries.async_update_entry(entry, options={CONF_POST_INTERVAL: 30})
    await hass.async_block_till_done()
    await sim.push_and_settle(sim.status())
    assert sim.firmware.interval == 15

    sim.return_home()
    # The retries are five minutes apart: let that time pass.
    entry.runtime_data.last_registration_retry = 0.0
    await sim.push_and_settle(sim.status())

    assert sim.firmware.interval == 30
    assert _unreachable_issue(hass, entry) is None
