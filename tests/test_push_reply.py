"""Settings in the push reply.

A firmware that reads the reply to its own push reports, in every push,
the revision of the webhook settings it holds (``status.webhook_rev``).
Home Assistant gives the settings it wants the device to have a revision
of its own. When the two differ, the reply to the push is a 200 with the
settings; when they match, or the device reports no revision, it is the
empty 204 it always was.

The device decides by itself whether it trusts a reply, and a device whose
webhook the user pinned reports no revision at all, so Home Assistant only
ever compares two strings: there is no acknowledgement to track.

tests/test_unreachable_device.py holds the two situations this exists for.
Everything here runs through the device simulator and the integration's
real registration.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError
import pytest

from custom_components.wican import (
    _async_request_webhook_registration,
    _build_webhook_payload,
    _reported_webhook_rev,
    _webhook_revision,
)
from custom_components.wican.const import (
    CONF_POST_INTERVAL,
    CONF_READS_PUSH_REPLY,
    DOMAIN,
    PUSH_REPLY_MAX_BYTES,
    PUSH_REPLY_REPEAT_INTERVAL,
    REGISTRATION_RETRY_INTERVAL,
)
from custom_components.wican.diagnostics import async_get_config_entry_diagnostics
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
REGISTER = "custom_components.wican._async_register_webhook_on_device"
BUILD = "custom_components.wican._build_webhook_payload"
LOGGER = "custom_components.wican"


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
    return sim


def _entry(sim: MeatPiDeviceSimulator) -> MockConfigEntry:
    assert sim.entry is not None
    return sim.entry


def _unreachable_issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"webhook_registration_failed_{entry.entry_id}",
    )


def _loud_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the integration logged at warning level or above."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno >= logging.WARNING
    ]


async def _set_interval(
    hass: HomeAssistant, entry: MockConfigEntry, interval: int,
) -> None:
    """Change the post interval in the options, as the options flow does."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_POST_INTERVAL: interval},
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# The revision
# ---------------------------------------------------------------------------


def test_revision_names_one_set_of_settings() -> None:
    """Eight hex characters that change with the settings and nothing else."""
    settings = {"url": "http://ha.local:8123/api/webhook/x", "enabled": True, "interval": 15}
    rev = _webhook_revision(settings)

    assert re.fullmatch(r"[0-9a-f]{8}", rev)
    assert _webhook_revision(dict(reversed(settings.items()))) == rev
    assert _webhook_revision({**settings, "interval": 16}) != rev
    assert _webhook_revision({**settings, "url": "http://ha.local:8123/api/webhook/y"}) != rev
    assert _webhook_revision({**settings, "urls": [settings["url"], "https://x"]}) != rev


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": {"webhook_rev": "1a2b3c4d"}}, "1a2b3c4d"),
        ({"status": {"webhook_rev": ""}}, ""),
        ({"status": {"webhook_rev": "Rev_16-chars-max"}}, "Rev_16-chars-max"),
        ({"status": {}}, None),
        ({"status": {"webhook_rev": None}}, None),
        ({"status": {"webhook_rev": 12345678}}, None),
        ({"status": {"webhook_rev": "seventeen-chars-x"}}, None),
        ({"status": {"webhook_rev": "has space"}}, None),
        ({"status": {"webhook_rev": "semi;colon"}}, None),
        ({"status": "not an object"}, None),
        ({}, None),
        ({"webhook_rev": "1a2b3c4d"}, None),
    ],
)
def test_reported_revision(payload: dict[str, Any], expected: str | None) -> None:
    """Only a revision string in the status object counts as one."""
    assert _reported_webhook_rev(payload) == expected


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------


async def test_first_push_gets_the_settings_and_the_next_one_nothing(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The device holds no revision yet, takes the settings, reports them."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    wanted_settings = _build_webhook_payload(hass, entry, 15)
    wanted = _webhook_revision(wanted_settings)
    # The first registration came before any push: it carried no revision.
    assert sim.firmware.registrations == [wanted_settings]
    assert sim.firmware.rev == ""

    resp = await sim.push_and_settle(sim.status())

    assert resp.status == HTTPStatus.OK
    assert resp.content_type == "application/json"
    raw = await resp.read()
    assert len(raw) <= PUSH_REPLY_MAX_BYTES
    assert json.loads(raw) == {"webhook": {**wanted_settings, "rev": wanted}}
    assert "urls" not in wanted_settings
    assert sim.last_reply == "applied"
    assert sim.firmware.rev == wanted
    assert entry.data[CONF_READS_PUSH_REPLY] is True

    resp = await sim.push_and_settle(sim.status())

    assert resp.status == HTTPStatus.NO_CONTENT
    assert await resp.read() == b""
    assert entry.runtime_data.push_reply.bodies_sent == 1


async def test_reply_carries_both_addresses_when_there_is_an_internet_url(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The reply is the registration: the local address, then the Internet one."""
    hass.config.external_url = "https://ha.example.org"
    sim = await _device(hass, hass_client, aioclient_mock)

    resp = await sim.push_and_settle(sim.status())

    webhook = (await resp.json())["webhook"]
    assert webhook["urls"][0] == webhook["url"]
    assert webhook["urls"][1] == f"https://ha.example.org/api/webhook/{sim.webhook_id}"
    assert sim.firmware.urls == webhook["urls"]


async def test_push_without_a_revision_is_answered_as_before(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Firmware that does not read replies never receives a body."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=False)
    entry = _entry(sim)

    resp = await sim.push_and_settle(sim.status())

    assert resp.status == HTTPStatus.NO_CONTENT
    assert await resp.read() == b""
    assert CONF_READS_PUSH_REPLY not in entry.data
    assert entry.runtime_data.push_reply.bodies_sent == 0


async def test_something_that_is_not_a_revision_gets_no_settings(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A status key of the right name and the wrong shape is no revision."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=False)

    resp = await sim.push_and_settle(sim.status(webhook_rev={"rev": "1a2b3c4d"}))

    assert resp.status == HTTPStatus.NO_CONTENT
    assert CONF_READS_PUSH_REPLY not in _entry(sim).data


async def test_unchanged_difference_is_repeated_every_five_minutes(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A device that does not trust the reply does not cost data on every push."""
    sim = await _device(hass, hass_client, aioclient_mock)
    sim.firmware.trusts_replies = False
    state = _entry(sim).runtime_data.push_reply

    first = await sim.push_and_settle(sim.status())
    assert first.status == HTTPStatus.OK
    assert sim.last_reply == "untrusted"
    for _ in range(3):
        resp = await sim.push_and_settle(sim.status())
        assert resp.status == HTTPStatus.NO_CONTENT
    assert state.bodies_sent == 1

    state.answered_at -= PUSH_REPLY_REPEAT_INTERVAL + 1
    again = await sim.push_and_settle(sim.status())

    assert again.status == HTTPStatus.OK
    assert await again.json() == await first.json()
    assert state.bodies_sent == 2
    assert sim.firmware.rev == ""
    assert sim.firmware.reply_verdicts == ["untrusted", "untrusted"]


async def test_new_difference_is_answered_at_once(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """New wanted settings, or a new reported revision, do not wait."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    sim.firmware.trusts_replies = False
    assert (await sim.push_and_settle(sim.status())).status == HTTPStatus.OK
    assert (await sim.push_and_settle(sim.status())).status == HTTPStatus.NO_CONTENT

    # Home Assistant wants something else (the device cannot be reached).
    sim.leave_home()
    await _set_interval(hass, entry, 30)
    resp = await sim.push_and_settle(sim.status())
    assert resp.status == HTTPStatus.OK
    assert (await resp.json())["webhook"]["interval"] == 30
    assert (await sim.push_and_settle(sim.status())).status == HTTPStatus.NO_CONTENT

    # The device holds something else (changed in its own web interface).
    sim.firmware.rev = "someone-elses"
    resp = await sim.push_and_settle(sim.status())
    assert resp.status == HTTPStatus.OK
    assert entry.runtime_data.push_reply.bodies_sent == 3


async def test_settings_changed_on_the_device_are_put_back(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Home Assistant's options win over a change made on the device."""
    sim = await _device(hass, hass_client, aioclient_mock)
    await sim.push_and_settle(sim.status())
    await sim.push_and_settle(sim.status())
    assert sim.firmware.interval == 15

    sim.firmware.changed_on_device(interval=120)
    resp = await sim.push_and_settle(sim.status())

    assert resp.status == HTTPStatus.OK
    assert sim.firmware.interval == 15
    assert (await sim.push_and_settle(sim.status())).status == HTTPStatus.NO_CONTENT


async def test_device_pinned_by_its_user_is_left_alone(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A pinned device reports no revision: no body, no revision registered."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    await sim.push_and_settle(sim.status())
    assert entry.data[CONF_READS_PUSH_REPLY] is True
    bodies = entry.runtime_data.push_reply.bodies_sent

    sim.firmware.pinned = True
    resp = await sim.push_and_settle(sim.status())
    assert resp.status == HTTPStatus.NO_CONTENT
    assert entry.data[CONF_READS_PUSH_REPLY] is False

    await _set_interval(hass, entry, 30)
    assert "rev" not in sim.firmware.registrations[-1]
    assert sim.firmware.registrations[-1]["interval"] == 30
    assert sim.firmware.interval == 15
    assert (await sim.push_and_settle(sim.status())).status == HTTPStatus.NO_CONTENT
    assert entry.runtime_data.push_reply.bodies_sent == bodies


async def test_registration_carries_the_revision_once_the_device_reported_one(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A reachable device gets the new settings and their revision at once."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    await sim.push_and_settle(sim.status())

    await _set_interval(hass, entry, 30)

    wanted_settings = _build_webhook_payload(hass, entry, 30)
    wanted = _webhook_revision(wanted_settings)
    assert sim.firmware.registrations[-1] == {**wanted_settings, "rev": wanted}
    assert sim.firmware.interval == 30
    assert sim.firmware.rev == wanted
    resp = await sim.push_and_settle(sim.status())
    assert resp.status == HTTPStatus.NO_CONTENT
    assert entry.runtime_data.push_reply.bodies_sent == 1


async def test_registration_at_the_next_start_carries_the_same_revision(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The entry remembers the device, and the revision outlives a restart."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    await sim.push_and_settle(sim.status())
    before = sim.firmware.rev
    registrations = len(sim.firmware.registrations)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(sim.firmware.registrations) == registrations + 1
    assert sim.firmware.registrations[-1]["rev"] == before
    resp = await sim.push_and_settle(sim.status())
    assert resp.status == HTTPStatus.NO_CONTENT
    assert entry.runtime_data.push_reply.bodies_sent == 0


async def test_rejected_push_gets_no_settings(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Only an accepted push is answered with settings."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)

    impostor = await sim.push(sim.status(device_id="someone_else"))
    garbage = await sim.push(raw=b"{not json")
    await hass.async_block_till_done()

    assert impostor.status == HTTPStatus.FORBIDDEN
    assert garbage.status == HTTPStatus.UNPROCESSABLE_ENTITY
    assert b"webhook" not in await impostor.read()
    assert b"webhook" not in await garbage.read()
    assert entry.runtime_data.push_reply.bodies_sent == 0
    assert sim.firmware.rev == ""


async def test_no_settings_while_home_assistant_has_no_address(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing to hand out: the push is delivered and answered with a 204."""
    sim = await _device(hass, hass_client, aioclient_mock)
    state = _entry(sim).runtime_data.push_reply
    caplog.clear()

    with patch(BUILD, side_effect=NoURLAvailableError):
        resp = await sim.push_and_settle(sim.status())

    assert resp.status == HTTPStatus.NO_CONTENT
    assert state.reported_rev == ""
    assert state.wanted_rev is None
    assert state.bodies_sent == 0
    assert _loud_records(caplog) == []


async def test_settings_that_do_not_fit_a_reply_are_not_sent(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """The device ignores a body over 2048 bytes, so none is sent."""
    sim = await _device(hass, hass_client, aioclient_mock)
    oversized = {
        "url": f"http://{'h' * PUSH_REPLY_MAX_BYTES}.example/api/webhook/x",
        "enabled": True,
        "interval": 15,
    }

    with patch(BUILD, return_value=oversized):
        resp = await sim.push_and_settle(sim.status())

    assert resp.status == HTTPStatus.NO_CONTENT
    assert _entry(sim).runtime_data.push_reply.bodies_sent == 0


# ---------------------------------------------------------------------------
# A registration that cannot reach the device
# ---------------------------------------------------------------------------


async def test_retries_after_a_push_are_spaced_and_quiet(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A car on LTE pushes for hours: one quiet attempt every five minutes."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    api = sim.attach_api(aioclient_mock)
    api.mode = "offline"
    entry = await sim.async_setup(real_registration=True)
    runtime = entry.runtime_data
    assert runtime.registration_failed is True
    attempts = api.count(*REGISTRATION)
    per_run = attempts  # one run tries each of the device's addresses once
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    await sim.push_and_settle(sim.status())
    assert api.count(*REGISTRATION) == attempts + per_run

    for _ in range(3):
        await sim.push_and_settle(sim.status())
    assert api.count(*REGISTRATION) == attempts + per_run

    runtime.last_registration_retry -= REGISTRATION_RETRY_INTERVAL + 1
    await sim.push_and_settle(sim.status())
    assert api.count(*REGISTRATION) == attempts + 2 * per_run

    assert _unreachable_issue(hass, entry) is None
    assert _loud_records(caplog) == []
    assert "its settings follow when it pushes" in caplog.text


@pytest.mark.parametrize("reads_replies", [False, True])
async def test_failed_registration_is_no_issue_while_the_device_pushes(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
    reads_replies: bool,
) -> None:
    """An option changed while the car is away: no error, no repair issue."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=reads_replies)
    entry = _entry(sim)
    await sim.push_and_settle(sim.status())
    sim.leave_home()
    caplog.clear()
    caplog.set_level(logging.INFO, logger=LOGGER)

    await _set_interval(hass, entry, 30)

    assert entry.runtime_data.registration_failed is True
    assert _unreachable_issue(hass, entry) is None
    assert not [
        record
        for record in caplog.records
        if record.name == LOGGER and record.levelno >= logging.ERROR
    ]
    assert "its settings follow when it pushes" in caplog.text


async def test_failed_registration_of_a_silent_device_is_an_issue(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """A device that stopped pushing and cannot be reached: the user is told."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    await sim.push_and_settle(sim.status())
    sim.go_stale()
    sim.leave_home()

    await _set_interval(hass, entry, 30)

    assert _unreachable_issue(hass, entry) is not None


async def test_registration_after_an_address_change_is_quiet(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A push from a new address asks for a registration that may fail quietly."""
    sim = await _device(hass, hass_client, aioclient_mock, reads_replies=False)
    entry = _entry(sim)
    assert sim.api is not None
    sim.api.add_base("http://10.0.0.9")
    sim.api.unreachable_hosts.add("10.0.0.9")
    sim.leave_home()
    caplog.clear()

    resp = await sim.push_and_settle(sim.status(mdns="http://10.0.0.9"))

    assert resp.status == HTTPStatus.NO_CONTENT
    assert entry.runtime_data.registration_failed is True
    assert _unreachable_issue(hass, entry) is None
    assert _loud_records(caplog) == []


async def test_folded_requests_keep_their_loudness(
    hass: HomeAssistant, device: MeatPiDeviceSimulator,
) -> None:
    """A request made by a push is quiet; one folded into it later is not."""
    entry = await device.async_setup()
    gate = asyncio.Event()
    quiet_runs: list[bool] = []

    async def _register(*_args: Any, quiet: bool = False, **_kwargs: Any) -> bool:
        quiet_runs.append(quiet)
        await gate.wait()
        return True

    with patch(REGISTER, side_effect=_register):
        first = asyncio.ensure_future(
            _async_request_webhook_registration(hass, entry, from_push=True),
        )
        await asyncio.sleep(0)
        # Both arrive while the first run is in flight: one trailing run.
        await _async_request_webhook_registration(hass, entry)
        await _async_request_webhook_registration(hass, entry, from_push=True)
        gate.set()
        await first

    assert quiet_runs == [True, False]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics_show_the_exchange_without_the_address(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Both revisions and the bodies sent, and nothing of the webhook address."""
    sim = await _device(hass, hass_client, aioclient_mock)
    entry = _entry(sim)
    await sim.push_and_settle(sim.status())
    await sim.push_and_settle(sim.status())

    section = (await async_get_config_entry_diagnostics(hass, entry))["push_reply"]

    last_body = section.pop("last_reply_body")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d.*", last_body)
    assert section == {
        "device_reads_replies": True,
        "reported_revision": sim.firmware.rev,
        "wanted_revision": sim.firmware.rev,
        "reply_bodies_sent": 1,
        "registration_failed": False,
    }
    assert sim.webhook_id not in json.dumps(section)
