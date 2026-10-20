"""The device_id is the entry's identity: manual setup, reconfigure, discovery and setup agree on it."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_WEBHOOK_ID
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import DOMAIN

from .conftest import TEST_DEVICE_ID

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

OTHER_DEVICE_ID = "f5e4d3c2b1a0"


def _entry(hass: HomeAssistant, *, unique_id: str | None = TEST_DEVICE_ID, **data: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN",
        unique_id=unique_id,
        data={CONF_WEBHOOK_ID: uuid4().hex, "mdns": "http://wican_old.local", **data},
    )
    entry.add_to_hass(hass)
    return entry


async def _start_user(hass: HomeAssistant, user_input: dict[str, str]):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, str]):
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_user_flow_refuses_a_device_already_added(hass: HomeAssistant, device_answers) -> None:
    """The same device added again by another address is recognised by its device_id."""
    _entry(hass)
    device_answers("http://192.168.1.50")

    result = await _start_user(hass, {"mdns": "192.168.1.50"})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_user_flow_asks_the_next_address_when_one_does_not_answer(
    hass: HomeAssistant,
    mock_aiohttp_session,
    aioclient_mock,
    device_answers,
) -> None:
    """The alternative address is asked first; the name is asked when it does not answer."""
    aioclient_mock.get("http://192.168.1.50/check_status", status=404)
    device_answers("http://wican_test.local")

    result = await _start_user(hass, {"mdns": "wican_test.local", CONF_HOST: "192.168.1.50"})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == TEST_DEVICE_ID


async def test_reconfigure_moves_the_entry_to_the_new_address(
    hass: HomeAssistant,
    mock_aiohttp_session,
    device_answers,
) -> None:
    """The device answers at the new address as itself: the entry follows it and reloads."""
    entry = _entry(hass, host="http://192.168.1.10")
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    device_answers("http://wican.example.org")

    with patch("custom_components.wican.async_setup_entry", return_value=True) as setup:
        result = await _reconfigure(hass, entry, {"mdns": "wican.example.org"})
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["mdns"] == "http://wican.example.org"
    assert CONF_HOST not in entry.data  # not given this time: the old address is not kept
    assert entry.data[CONF_WEBHOOK_ID] == webhook_id
    assert entry.unique_id == TEST_DEVICE_ID
    assert setup.called  # reloaded, so the webhook is registered on the device again


async def test_reconfigure_suggests_the_address_the_entry_has(hass: HomeAssistant) -> None:
    """The form opens on the addresses the entry already uses, as typed."""
    entry = _entry(hass, host="http://192.168.1.10")

    result = await entry.start_reconfigure_flow(hass)

    suggested = {str(key): key.description["suggested_value"] for key in result["data_schema"].schema}
    assert suggested == {"mdns": "wican_old.local", CONF_HOST: "192.168.1.10"}


async def test_reconfigure_refuses_another_device(hass: HomeAssistant, device_answers) -> None:
    """An address where another WiCAN answers never takes over this entry."""
    entry = _entry(hass)
    device_answers("http://wican.example.org", device_id=OTHER_DEVICE_ID)

    result = await _reconfigure(hass, entry, {"mdns": "wican.example.org"})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data["mdns"] == "http://wican_old.local"
    assert entry.unique_id == TEST_DEVICE_ID


async def test_reconfigure_says_when_the_device_does_not_answer(hass: HomeAssistant, aioclient_mock) -> None:
    """A device asleep or gone leaves the entry as it was and the form open."""
    entry = _entry(hass)
    aioclient_mock.get("http://wican.example.org/check_status", exc=TimeoutError())

    result = await _reconfigure(hass, entry, {"mdns": "wican.example.org"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data["mdns"] == "http://wican_old.local"


async def test_reconfigure_gives_an_entry_without_identity_its_device_id(
    hass: HomeAssistant,
    mock_aiohttp_session,
    device_answers,
) -> None:
    """An entry an older manual setup made has no identity: reconfigure gives it the device's."""
    entry = _entry(hass, unique_id=None)
    device_answers("http://wican.example.org")

    with patch("custom_components.wican.async_setup_entry", return_value=True):
        result = await _reconfigure(hass, entry, {"mdns": "wican.example.org"})
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == TEST_DEVICE_ID
    assert entry.data["device_id"] == TEST_DEVICE_ID


async def test_discovery_keys_on_the_device_id(hass: HomeAssistant) -> None:
    """Discovery keys on the device_id, the identity manual setup and the webhook share."""
    discovery = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_test.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:FF", "device_id": TEST_DEVICE_ID.encode()},
    )

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery,
    )

    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["unique_id"] == TEST_DEVICE_ID


async def test_setup_keys_an_entry_on_the_device_id_it_stores(hass: HomeAssistant, mock_aiohttp_session) -> None:
    """An entry made before the device_id was its identity takes it at setup."""
    entry = _entry(hass, unique_id=None, device_id=TEST_DEVICE_ID)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.unique_id == TEST_DEVICE_ID


async def test_setup_names_a_duplicate_rather_than_merging_it(
    hass: HomeAssistant,
    mock_aiohttp_session,
    caplog,
) -> None:
    """Two entries for one device: the second keeps its identity and the log names both."""
    _entry(hass)
    duplicate = _entry(hass, unique_id=None, device_id=TEST_DEVICE_ID)

    await hass.config_entries.async_setup(duplicate.entry_id)
    await hass.async_block_till_done()

    assert duplicate.unique_id is None
    assert "remove the duplicate" in caplog.text


async def test_the_first_webhook_gives_the_entry_its_identity(
    hass: HomeAssistant,
    mock_aiohttp_session,
    mock_webhook_data,
    hass_client_no_auth,
) -> None:
    """An entry that has never heard from its device takes the device_id the first webhook carries."""
    entry = _entry(hass, unique_id=None)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.unique_id is None

    client = await hass_client_no_auth()
    response = await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=mock_webhook_data)
    await hass.async_block_till_done()

    assert response.status < 300
    assert entry.unique_id == mock_webhook_data["status"]["device_id"]


async def test_a_webhook_does_not_overwrite_where_the_device_is_reached(
    hass: HomeAssistant,
    mock_aiohttp_session,
    mock_webhook_data,
    hass_client_no_auth,
) -> None:
    """The address set at setup or by a reconfigure stays: the device's own mDNS name does not replace it."""
    entry = _entry(hass, unique_id=None, host="http://192.0.2.31")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    payload = {**mock_webhook_data, "status": {**mock_webhook_data["status"], "mdns": "wican_a0b1c2d3e4f5.local"}}

    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=payload)
    await hass.async_block_till_done()

    assert entry.data["mdns"] == "http://wican_old.local"
    assert entry.data[CONF_HOST] == "http://192.0.2.31"


async def test_a_webhook_fills_in_an_address_the_entry_lacks(
    hass: HomeAssistant,
    mock_aiohttp_session,
    mock_webhook_data,
    hass_client_no_auth,
) -> None:
    """An entry with no name for the device learns the one the device reports."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN",
        data={CONF_WEBHOOK_ID: uuid4().hex, CONF_HOST: "http://192.0.2.31"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    payload = {**mock_webhook_data, "status": {**mock_webhook_data["status"], "mdns": "wican_a0b1c2d3e4f5.local"}}

    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=payload)
    await hass.async_block_till_done()

    assert entry.data["mdns"] == "wican_a0b1c2d3e4f5.local"
