"""The device of a removed entry is asked to stop posting.

Home Assistant answers a post to a webhook it does not know with 200. A
device whose entry was removed therefore went on posting for good, and its
own status said every post was delivered. On removal the integration now
asks the device for its webhook settings and clears them when they still
name the removed entry's webhook.

Everything here runs through the device simulator and the integration's
real registration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_WEBHOOK_ID
import pytest

from custom_components.wican import _async_release_device_webhook
from custom_components.wican.const import DOMAIN
from tests.conftest import MockConfigEntry
from tests.device_sim import MeatPiDeviceSimulator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = [
    pytest.mark.usefixtures("one_registration_attempt"),
    pytest.mark.real_webhook_registration,
]

CLEAR = ("DELETE", "/api/webhook")
LOGGER = "custom_components.wican"


async def _device(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> MeatPiDeviceSimulator:
    """A WiCAN PRO, set up and registered by the integration itself."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup(real_registration=True)
    assert sim.firmware.enabled
    assert sim.firmware.url.endswith(f"/api/webhook/{sim.webhook_id}")
    return sim


async def _remove(hass: HomeAssistant, sim: MeatPiDeviceSimulator) -> None:
    """Remove the entry, as Delete on the integration's page does."""
    assert sim.entry is not None
    await hass.config_entries.async_remove(sim.entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []


def _cleared(sim: MeatPiDeviceSimulator) -> int:
    assert sim.api is not None
    return sim.api.count(*CLEAR)


def _loud_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the integration logged at warning level or above."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno >= logging.WARNING
    ]


async def test_removed_entry_clears_the_devices_webhook(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The device stops posting to the webhook that is gone."""
    sim = await _device(hass, hass_client, aioclient_mock)

    await _remove(hass, sim)

    assert _cleared(sim) == 1
    assert sim.firmware.url == ""
    assert not sim.firmware.enabled


async def test_reload_does_not_clear_the_devices_webhook(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """An unload is not a removal: the device keeps posting."""
    sim = await _device(hass, hass_client, aioclient_mock)

    await sim.async_reload()

    assert _cleared(sim) == 0
    assert sim.firmware.enabled


async def test_device_posting_elsewhere_is_left_alone(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A device registered with another Home Assistant keeps its settings."""
    sim = await _device(hass, hass_client, aioclient_mock)
    elsewhere = "http://other-ha.local:8123/api/webhook/another_hook"
    sim.firmware.changed_on_device(url=elsewhere, urls=[elsewhere])

    await _remove(hass, sim)

    assert _cleared(sim) == 0
    assert sim.firmware.url == elsewhere
    assert sim.firmware.enabled


async def test_webhook_id_that_only_starts_the_same_is_left_alone(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The whole webhook id is compared, not a part of the address."""
    sim = await _device(hass, hass_client, aioclient_mock)
    longer = f"http://ha.local:8123/api/webhook/{sim.webhook_id}_of_another_entry"
    sim.firmware.changed_on_device(url=longer, urls=[longer])

    await _remove(hass, sim)

    assert _cleared(sim) == 0
    assert sim.firmware.url == longer


async def test_webhook_named_by_the_second_address_is_cleared(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A device holds two addresses of Home Assistant: either names it."""
    sim = await _device(hass, hass_client, aioclient_mock)
    first = "https://ha.example.org/api/webhook/replaced"
    second = f"http://192.168.1.2:8123/api/webhook/{sim.webhook_id}"
    sim.firmware.changed_on_device(url=first, urls=[first, second])

    await _remove(hass, sim)

    assert _cleared(sim) == 1
    assert sim.firmware.url == ""


async def test_webhook_pinned_on_the_device_is_left_alone(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Manual override on the device: its settings are the user's own."""
    sim = await _device(hass, hass_client, aioclient_mock)
    sim.firmware.pinned = True
    caplog.set_level(logging.INFO, logger=LOGGER)

    await _remove(hass, sim)

    assert _cleared(sim) == 0
    assert sim.firmware.enabled
    assert "pinned on the device" in caplog.text


@pytest.mark.parametrize(
    "mode", ["offline", "timeout", "forbidden", "error", "garbage", "html"],
)
async def test_device_that_cannot_be_asked_does_not_hold_the_removal(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    """Asleep, away or answering nonsense: the entry goes, nothing is loud."""
    sim = await _device(hass, hass_client, aioclient_mock)
    assert sim.api is not None
    sim.api.mode = mode
    caplog.clear()
    caplog.set_level(logging.INFO, logger=LOGGER)

    await _remove(hass, sim)

    assert _cleared(sim) == 0
    assert sim.firmware.enabled
    assert "could not be reached" in caplog.text
    assert _loud_records(caplog) == []


async def test_second_address_is_asked_when_the_first_is_dead(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The stored IP no longer answers; the host name does."""
    sim = await _device(hass, hass_client, aioclient_mock)
    assert sim.api is not None
    sim.api.unreachable_hosts.add(sim.ip)

    await _remove(hass, sim)

    assert _cleared(sim) == 1
    assert sim.firmware.url == ""


@pytest.mark.parametrize(
    ("verb", "answer"),
    [
        # The settings are not an object.
        ("GET", {"text": "[]"}),
        # The settings are longer than anything a device sends.
        ("GET", {"text": "x" * 20000}),
        # The device has the settings but refuses to clear them.
        ("DELETE", {"status": 500}),
    ],
)
async def test_answer_of_another_shape_leaves_the_device_as_it_is(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    verb: str,
    answer: dict[str, Any],
) -> None:
    """Nothing is cleared on an answer the integration cannot read."""
    sim = await _device(hass, hass_client, aioclient_mock)
    assert sim.api is not None
    sim.api.webhook_answers[verb] = answer

    await _remove(hass, sim)

    assert sim.firmware.enabled
    assert sim.firmware.url.endswith(f"/api/webhook/{sim.webhook_id}")
    # Both stored addresses were asked; neither gave a usable answer.
    assert sim.api.count(verb, "/api/webhook") == 2


async def test_entry_without_webhook_or_address_is_not_asked(
    hass: HomeAssistant, aioclient_mock: Any,
) -> None:
    """An entry that never had a webhook, or has no address, asks nobody."""
    no_webhook = MockConfigEntry(domain=DOMAIN, data={"host": "http://192.168.1.9"})
    no_address = MockConfigEntry(
        domain=DOMAIN, data={CONF_WEBHOOK_ID: "some_hook", "host": "http://["},
    )

    assert await _async_release_device_webhook(hass, no_webhook) is False
    assert await _async_release_device_webhook(hass, no_address) is False
    assert aioclient_mock.call_count == 0
