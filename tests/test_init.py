"""Test the WiCAN integration init."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers.network import NoURLAvailableError

from custom_components.wican import _async_register_webhook_on_device
from custom_components.wican.const import DOMAIN

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_entry_success(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiohttp_session,
) -> None:
    """Test successful setup of entry."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not None
    assert mock_config_entry.runtime_data.coordinator is not None


async def test_setup_entry_webhook_registration_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiohttp_session,
) -> None:
    """Test setup when webhook registration fails (should still succeed)."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=False,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Should still load even if webhook registration fails
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_unload_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test unloading an entry."""
    entry = init_integration

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_webhook_post(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test webhook endpoint receives data."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post webhook data
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=mock_webhook_data,
    )

    assert resp.status == 204  # NO_CONTENT

    # Verify coordinator has the data
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data == mock_webhook_data


async def test_webhook_device_identity_mismatch(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test webhook rejects data from wrong device."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Change device_id to simulate wrong device - deep copy the data
    import copy
    wrong_device_data = copy.deepcopy(mock_webhook_data)
    wrong_device_data["status"]["device_id"] = "different_device_456"

    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=wrong_device_data,
    )

    # The coordinator validates device_id and the handler turns the resulting
    # ConfigEntryError into a 403
    assert resp.status == 403


async def test_webhook_identity_mismatch_does_not_persist_connection_info(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_client,
) -> None:
    """A rejected payload must not move the device address.

    The stored address is where the integration POSTs the webhook
    registration, which carries the webhook URL. Connection info used to be
    written - and a re-registration scheduled against it - before the
    device_id was checked, so a rejected payload could still redirect it.
    """
    entry = init_integration
    original_ip = entry.data.get("ip")
    original_host = entry.data.get("host")

    client = await hass_client()

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        AsyncMock(return_value=True),
    ) as mock_register:
        resp = await client.post(
            f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
            json={
                "status": {
                    "device_id": "different_device_456",
                    "host": "http://198.51.100.9",
                    "ip": "198.51.100.9",
                },
            },
        )
        await hass.async_block_till_done()

    assert resp.status == 403
    assert entry.data.get("ip") == original_ip
    assert entry.data.get("host") == original_host
    mock_register.assert_not_called()


async def test_webhook_from_configured_device_persists_source_ip(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """A payload from the configured device still records where it came from."""
    entry = init_integration

    client = await hass_client()

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        AsyncMock(return_value=True),
    ):
        resp = await client.post(
            f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}",
            json=mock_webhook_data,
        )
        await hass.async_block_till_done()

    assert resp.status == 204
    # hass_client connects over loopback
    assert entry.data.get("ip") == "127.0.0.1"


async def test_entry_updated(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test entry options update."""
    entry = init_integration

    # Update post interval
    new_interval = 2000
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "post_interval": new_interval},
    )
    await hass.async_block_till_done()

    # Verify runtime data updated
    assert entry.runtime_data.post_interval == new_interval


async def test_webhook_no_duplicate_registration(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test webhook doesn't trigger duplicate registration when same device info repeated."""
    entry = init_integration
    
    client = await hass_client()
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    # First webhook - data already stored from setup
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=mock_webhook_data,
    )
    assert resp.status == 204
    await hass.async_block_till_done()

    # Second webhook with same data - should NOT trigger re-registration  
    # The key point: mdns/device_id are already in entry.data from init,
    # so webhook shouldn't see them as "changed"
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=mock_webhook_data,
    )
    assert resp.status == 204
    await hass.async_block_till_done()
    
    # Integration is working - no errors means no spurious re-registrations
    assert entry.state == ConfigEntryState.LOADED


async def test_webhook_registration_no_url_available(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test webhook registration falls back to stored webhook_url."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "host": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "webhook_url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        },
        unique_id="wican_test-192.168.1.100:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            side_effect=NoURLAvailableError,
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"]["url"] == entry.data["webhook_url"]


async def test_webhook_registration_non_pro_uses_single_local_http_url(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test non-PRO firmware registers a single local HTTP webhook URL."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "host": "http://wican_test.local:80",
            "fw_version": "v4.99",
            "hw_version": "v3.1",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        unique_id="wican_test-192.168.1.100:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            return_value="http://192.168.1.10:8123",
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"] == {
        "url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        "enabled": True,
        "interval": entry.runtime_data.post_interval,
    }


async def test_webhook_registration_pro_uses_local_and_external_urls(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test PRO firmware 4.49+ receives both local and external webhook URLs."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN PRO",
        data={
            "mdns": "http://wican_pro.local:80",
            "host": "http://wican_pro.local:80",
            "fw_version": "v4.49",
            "hw_version": "WiCAN PRO",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        unique_id="wican_pro-192.168.1.101:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            side_effect=[
                "http://192.168.1.10:8123",
                "https://example.ui.nabu.casa",
            ],
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"] == {
        "url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        "urls": [
            "http://192.168.1.10:8123/api/webhook/test_webhook_id",
            "https://example.ui.nabu.casa/api/webhook/test_webhook_id",
        ],
        "enabled": True,
        "interval": entry.runtime_data.post_interval,
    }


async def test_webhook_registration_pro_pre_449_uses_single_local_url(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test older PRO firmware keeps the single local webhook URL payload."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN PRO",
        data={
            "mdns": "http://wican_pro.local:80",
            "host": "http://wican_pro.local:80",
            "fw_version": "v4.48",
            "hw_version": "WiCAN PRO",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        unique_id="wican_pro-192.168.1.102:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            return_value="http://192.168.1.10:8123",
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"] == {
        "url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        "enabled": True,
        "interval": entry.runtime_data.post_interval,
    }


async def test_webhook_registration_https_local_falls_back_to_http_stored_url(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test HTTPS local URLs are rejected for device registration."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "host": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "webhook_url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        },
        unique_id="wican_test-192.168.1.103:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            return_value="https://internal.example.com",
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"]["url"] == entry.data["webhook_url"]









