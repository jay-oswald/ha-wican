"""The webhook of a removed entry answers 410 Gone.

Removing an entry asks the device to stop posting, which only works when the
device can be reached at that moment. A car that is asleep or away cannot, and
Home Assistant answers a post to a webhook it does not know with 200: the car
went on posting for good and counted every post as delivered. The webhook id
of a removed entry is therefore kept and answers ``410 Gone`` with
``{"webhook": {"removed": true}}``; a firmware that reads the reply to its
push clears its webhook on that.
"""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.setup import async_setup_component
import pytest

from custom_components.wican.const import (
    DOMAIN,
    MAX_REMOVED_WEBHOOKS,
    REMOVED_WEBHOOKS_STORAGE_KEY,
)
from custom_components.wican.removed_webhooks import (
    REMOVED_WEBHOOK_REPLY,
    async_get_removed_webhooks,
)
from tests.device_sim import MeatPiDeviceSimulator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = [
    pytest.mark.usefixtures("one_registration_attempt"),
    pytest.mark.real_webhook_registration,
]

LOGGER = "custom_components.wican"
RESTART = "wican_removed_webhooks"


async def _device(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    *,
    reads_replies: bool = True,
) -> MeatPiDeviceSimulator:
    """A WiCAN PRO, set up and registered by the integration itself."""
    sim = MeatPiDeviceSimulator.from_preset(
        hass, hass_client, "wican_pro_v6", reads_replies=reads_replies,
    )
    sim.attach_api(aioclient_mock)
    await sim.async_setup(real_registration=True)
    assert sim.firmware.enabled
    return sim


async def _remove_while_away(hass: HomeAssistant, sim: MeatPiDeviceSimulator) -> None:
    """Remove the entry while the device is asleep or away."""
    assert sim.api is not None
    assert sim.entry is not None
    sim.api.mode = "offline"
    await hass.config_entries.async_remove(sim.entry.entry_id)
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []
    # It could not be told: it still holds the webhook.
    assert sim.firmware.enabled
    sim.api.mode = "ok"


def _stored_ids(hass_storage: dict[str, Any]) -> list[str]:
    return list(hass_storage[REMOVED_WEBHOOKS_STORAGE_KEY]["data"]["ids"])


async def _restart(hass: HomeAssistant, *webhook_ids: str) -> None:
    """Forget what a running Home Assistant holds, as a restart does."""
    for webhook_id in webhook_ids:
        webhook.async_unregister(hass, webhook_id)
    hass.data.pop(RESTART)


async def test_post_to_the_webhook_of_a_removed_entry_is_answered_gone(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Not Home Assistant's 200 for a webhook it does not know."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=False)
    await _remove_while_away(hass, sim)

    response = await sim.push(sim.status())

    assert response.status == HTTPStatus.GONE
    assert await response.json() == REMOVED_WEBHOOK_REPLY == {"webhook": {"removed": True}}


async def test_device_that_could_not_be_told_stops_at_its_next_post(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A firmware that reads the reply clears its webhook on the 410."""
    sim = await _device(hass, hass_client, aioclient_mock)
    await _remove_while_away(hass, sim)
    caplog.set_level(logging.INFO, logger=LOGGER)

    await sim.push(sim.status())

    assert sim.last_reply == "removed"
    assert sim.firmware.url == ""
    assert not sim.firmware.enabled
    assert sim.firmware.reported_rev() == ""
    assert "answered 410 Gone" in caplog.text
    # The line is said once, not at every post of an older firmware.
    caplog.clear()
    await sim.push(sim.status())
    assert "answered 410 Gone" not in caplog.text


async def test_older_firmware_gets_the_answer_and_goes_on(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A firmware that does not read replies keeps posting, to failures now."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=False)
    await _remove_while_away(hass, sim)

    first = await sim.push(sim.status())
    second = await sim.push(sim.status())

    assert (first.status, second.status) == (HTTPStatus.GONE, HTTPStatus.GONE)
    assert sim.firmware.enabled


async def test_webhook_pinned_on_the_device_survives_the_answer(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Manual override on the device: its settings are the user's own."""
    sim = await _device(hass, hass_client, aioclient_mock)
    sim.firmware.pinned = True
    await _remove_while_away(hass, sim)

    await sim.push(sim.status())

    assert sim.last_reply == "pinned"
    assert sim.firmware.enabled


async def test_device_told_at_the_removal_is_not_asked_twice(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Reached at the removal, the device is cleared there; the id is kept."""
    sim = await _device(hass, hass_client, aioclient_mock)
    assert sim.api is not None
    assert sim.entry is not None

    await hass.config_entries.async_remove(sim.entry.entry_id)
    await hass.async_block_till_done()

    assert sim.api.count("DELETE", "/api/webhook") == 1
    assert not sim.firmware.enabled
    removed = await async_get_removed_webhooks(hass)
    assert removed.ids == (sim.webhook_id,)


async def test_removed_webhook_answers_gone_after_a_restart(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    hass_storage: dict[str, Any],
) -> None:
    """The id is stored, and registered again when the integration loads."""
    sim = await _device(hass, hass_client, aioclient_mock)
    await _remove_while_away(hass, sim)
    assert _stored_ids(hass_storage) == [sim.webhook_id]

    await _restart(hass, sim.webhook_id)
    # Nothing has loaded the integration yet: Home Assistant's own answer.
    assert (await sim.push(sim.status())).status == HTTPStatus.OK
    assert sim.firmware.enabled

    # Another device's entry is set up: that loads the removed webhooks.
    other = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_usb_v6")
    other.attach_api(aioclient_mock)
    await other.async_setup(real_registration=True)

    await sim.push(sim.status())
    assert sim.last_reply == "removed"
    assert not sim.firmware.enabled


async def test_discovery_loads_the_removed_webhooks(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
) -> None:
    """With no entry left, a discovered device is what loads them."""
    sim = await _device(hass, hass_client, aioclient_mock)
    await _remove_while_away(hass, sim)
    await _restart(hass, sim.webhook_id)

    # The car comes home and announces itself.
    with patch(
        "homeassistant.components.onboarding.async_is_onboarded",
        return_value=True,
    ):
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=ZeroconfServiceInfo(
                ip_address="192.168.1.102",
                ip_addresses=["192.168.1.102"],
                hostname="wican_pro.local.",
                name="WiCAN-WebServer._wican._tcp.local.",
                port=80,
                type="_wican._tcp.local.",
                properties={"mac": b"AA:BB:CC:00:00:02"},
            ),
        )

    await sim.push(sim.status())
    assert sim.last_reply == "removed"
    assert not sim.firmware.enabled


async def test_entry_that_comes_back_with_its_webhook_takes_it_over(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    hass_storage: dict[str, Any],
) -> None:
    """A restored entry with the old webhook id gets its posts again."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=False)
    await _remove_while_away(hass, sim)
    assert (await sim.push(sim.status())).status == HTTPStatus.GONE

    await sim.async_setup(real_registration=True)

    assert (await sim.push(sim.status())).status in (HTTPStatus.OK, HTTPStatus.NO_CONTENT)
    assert _stored_ids(hass_storage) == []
    assert (await async_get_removed_webhooks(hass)).ids == ()


async def test_only_the_newest_removed_webhooks_are_kept(
    hass: HomeAssistant, hass_client: Any, hass_storage: dict[str, Any],
) -> None:
    """The list cannot grow without end; the oldest id goes first."""
    assert await async_setup_component(hass, "webhook", {})
    removed = await async_get_removed_webhooks(hass)
    for number in range(MAX_REMOVED_WEBHOOKS + 3):
        await removed.async_add(f"hook_{number}")
    # A second removal of the same id, and an id of no use, change nothing.
    await removed.async_add(f"hook_{MAX_REMOVED_WEBHOOKS + 2}")
    await removed.async_add("")

    assert len(removed.ids) == MAX_REMOVED_WEBHOOKS
    assert removed.ids[0] == "hook_3"
    assert removed.ids[-1] == f"hook_{MAX_REMOVED_WEBHOOKS + 2}"
    assert _stored_ids(hass_storage) == list(removed.ids)
    client = await hass_client()
    assert (await client.post("/api/webhook/hook_0", json={})).status == HTTPStatus.OK
    assert (await client.post("/api/webhook/hook_3", json={})).status == HTTPStatus.GONE
    # An id nobody removed is forgotten without a fuss.
    await removed.async_forget("never_removed")
    assert len(removed.ids) == MAX_REMOVED_WEBHOOKS


@pytest.mark.parametrize(
    "stored",
    [
        "not an object",
        {"ids": "not a list"},
        {"ids": [7, None, "", "x" * 500, {"id": "a"}]},
        {},
    ],
)
async def test_stored_list_of_another_shape_is_survived(
    hass: HomeAssistant, hass_storage: dict[str, Any], stored: Any,
) -> None:
    """A damaged file leaves no id, and no error."""
    hass_storage[REMOVED_WEBHOOKS_STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": REMOVED_WEBHOOKS_STORAGE_KEY,
        "data": stored,
    }

    removed = await async_get_removed_webhooks(hass)

    assert removed.ids == ()


async def test_storage_that_fails_is_survived(
    hass: HomeAssistant, hass_client: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    """No list on a read error; on a write error the id answers until a restart."""
    assert await async_setup_component(hass, "webhook", {})
    store = "custom_components.wican.removed_webhooks.Store"

    with patch(f"{store}.async_load", side_effect=OSError("read")):
        removed = await async_get_removed_webhooks(hass)
    assert removed.ids == ()
    with patch(f"{store}.async_save", side_effect=OSError("write")):
        await removed.async_add("hook_a")

    assert removed.ids == ("hook_a",)
    client = await hass_client()
    assert (await client.post("/api/webhook/hook_a", json={})).status == HTTPStatus.GONE
    assert "Could not read the removed webhooks" in caplog.text
    assert "Could not store the removed webhooks" in caplog.text


async def test_stored_ids_are_registered_once_each(
    hass: HomeAssistant, hass_client: Any, hass_storage: dict[str, Any],
) -> None:
    """A stored id answers 410; a doubled one is no error."""
    assert await async_setup_component(hass, "webhook", {})
    hass_storage[REMOVED_WEBHOOKS_STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": REMOVED_WEBHOOKS_STORAGE_KEY,
        "data": {"ids": ["old_hook", "old_hook", "other_hook"]},
    }

    removed = await async_get_removed_webhooks(hass)

    assert removed.ids == ("old_hook", "other_hook")
    client = await hass_client()
    assert (await client.post("/api/webhook/old_hook", json={})).status == HTTPStatus.GONE
    assert (await client.post("/api/webhook/other_hook", json={})).status == HTTPStatus.GONE
