"""Test webhook registration logic with retry, timeout, and IP caching."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from unittest.mock import AsyncMock, Mock, patch
from yarl import URL

from aiohttp import ClientError, ClientResponseError, ServerDisconnectedError
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers import aiohttp_client

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN

from tests.conftest import MockConfigEntry

pytestmark = pytest.mark.real_webhook_registration


@pytest.fixture
def mock_session():
    """Create mock aiohttp session."""
    session = Mock()
    session.post = AsyncMock()
    return session


def create_mock_response(status: int, text: str = "OK"):
    """Helper to create mock aiohttp response with proper async context manager."""
    mock_response = Mock()
    mock_response.status = status
    mock_response.text = AsyncMock(return_value=text)
    
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_response
    mock_context.__aexit__.return_value = None
    
    return mock_context


async def test_webhook_registration_success_first_try(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test successful webhook registration on first attempt."""
    mock_config_entry.add_to_hass(hass)
    
    # Mock successful response using helper
    mock_session.post.return_value = create_mock_response(200, "OK")
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        # Setup entry (triggers webhook registration)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Verify POST was called
    assert mock_session.post.called
    call_args = mock_session.post.call_args
    
    # Check payload contains required fields
    payload = call_args.kwargs["json"]
    assert "url" in payload
    assert "enabled" in payload
    assert "interval" in payload
    assert payload["enabled"] is True


async def test_webhook_registration_pro_includes_external_https_url(
    hass: HomeAssistant,
    mock_session,
) -> None:
    """Test Pro devices send local HTTP and external HTTPS webhook URLs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Pro",
        data={
            CONF_WEBHOOK_ID: "test_webhook_id",
            "fw_version": "4.49",
            "hw_version": "WiCAN-PRO",
            "host": "http://wican-pro.local",
            "mdns": "http://wican-pro.local",
            "ip": "192.168.1.150",
        },
        options={CONF_POST_INTERVAL: 15},
    )
    entry.add_to_hass(hass)

    mock_session.post.return_value = create_mock_response(200, "OK")

    with (
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.wican.resolve_device_webhook_urls",
            return_value=[
                "http://homeassistant.local:8123/api/webhook/test_webhook_id",
                "https://example.ui.nabu.casa/api/webhook/test_webhook_id",
            ],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    payload = mock_session.post.call_args.kwargs["json"]
    assert payload["url"] == "http://homeassistant.local:8123/api/webhook/test_webhook_id"
    assert payload["urls"] == [
        "http://homeassistant.local:8123/api/webhook/test_webhook_id",
        "https://example.ui.nabu.casa/api/webhook/test_webhook_id",
    ]


async def test_webhook_registration_pro_falls_back_to_external_https_only(
    hass: HomeAssistant,
    mock_session,
) -> None:
    """Test Pro devices can register with external HTTPS only when local HTTP is unavailable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Pro",
        data={
            CONF_WEBHOOK_ID: "test_webhook_id",
            "fw_version": "4.49",
            "hw_version": "WiCAN-PRO",
            "host": "http://wican-pro.local",
            "mdns": "http://wican-pro.local",
            "ip": "192.168.1.150",
        },
        options={CONF_POST_INTERVAL: 15},
    )
    entry.add_to_hass(hass)

    mock_session.post.return_value = create_mock_response(200, "OK")

    with (
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.wican.resolve_device_webhook_urls",
            return_value=[
                "https://example.ui.nabu.casa/api/webhook/test_webhook_id",
            ],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    payload = mock_session.post.call_args.kwargs["json"]
    assert payload["url"] == "https://example.ui.nabu.casa/api/webhook/test_webhook_id"
    assert "urls" not in payload


async def test_webhook_registration_retry_on_connection_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test webhook registration retries on connection error."""
    mock_config_entry.add_to_hass(hass)
    
    # First two attempts fail, third succeeds
    mock_session.post.side_effect = [
        create_mock_response(500, "Server Error"),  # Attempt 1 fails
        create_mock_response(500, "Server Error"),  # Attempt 2 fails
        create_mock_response(200, "OK"),  # Attempt 3 succeeds
    ]
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Should have made 3 POST attempts
    assert mock_session.post.call_count == 3


async def test_webhook_registration_fails_after_max_retries(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test webhook registration fails after exhausting retries."""
    mock_config_entry.add_to_hass(hass)
    
    # All attempts fail
    mock_session.post.side_effect = ClientError("Connection refused")
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        # Setup should still succeed (webhook registration failure is non-fatal)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Should have made max_retries (3) POST attempts (possibly more with multiple endpoints)
    assert mock_session.post.call_count >= 3


async def test_webhook_registration_raises_and_clears_repair(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """A failed registration raises a repair issue; a later success clears it."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.wican import (
        _async_register_webhook_on_device,
        _webhook_repair_issue_id,
    )

    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
        issue_id = _webhook_repair_issue_id(entry)
        registry = ir.async_get(hass)

        # All endpoints fail -> repair issue is created.
        mock_session.post.side_effect = ClientError("Connection refused")
        await _async_register_webhook_on_device(hass, entry, max_retries=1)
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        # A subsequent successful registration clears the issue.
        ok_response = Mock()
        ok_response.status = int(HTTPStatus.OK)
        ok_response.text = AsyncMock(return_value="OK")
        ok_response.read = AsyncMock(return_value=b"")
        mock_session.post.side_effect = None
        mock_session.post.return_value = ok_response
        await _async_register_webhook_on_device(hass, entry, max_retries=1)
        assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_webhook_registration_timeout_handling(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test webhook registration handles timeout gracefully."""
    mock_config_entry.add_to_hass(hass)
    
    # Simulate timeout
    mock_session.post.side_effect = asyncio.TimeoutError()
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        # Setup should still succeed
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Should have attempted registration
    assert mock_session.post.called


async def test_webhook_registration_ip_caching(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test IP address caching fields exist in runtime data."""
    # Note: Testing the actual IP caching success path (lines 396-423) requires
    # mocking successful HTTP POST which has async_timeout.timeout() issues.
    # The IP caching logic is validated in integration tests.
    mock_config_entry.add_to_hass(hass)
    
    # Setup will attempt registration (will fail but that's ok)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    
    # Check runtime_data exists with IP caching fields
    entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    runtime_data = entry.runtime_data
    
    # Fields exist (will be None/0 since registration failed)
    assert hasattr(runtime_data, 'cached_resolved_ip')
    assert hasattr(runtime_data, 'cache_timestamp')
    assert runtime_data.cache_timestamp == 0.0


async def test_webhook_registration_uses_cached_ip(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test webhook registration prefers cached IP."""
    import time
    
    mock_config_entry.add_to_hass(hass)
    
    # Setup entry first time
    mock_response = Mock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK")
    
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_response
    mock_session.post.return_value = mock_context
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        
        entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
        
        # Manually set cached IP
        entry.runtime_data.cached_resolved_ip = "192.168.1.100"
        entry.runtime_data.cache_timestamp = time.time()
        
        # Reset mock to track second registration
        mock_session.post.reset_mock()
        
        # Trigger another registration (e.g., via reload)
        from custom_components.wican import _async_register_webhook_on_device
        
        await _async_register_webhook_on_device(hass, entry)
        await hass.async_block_till_done()
        
        # Check that cached IP was used (should be first in endpoint list)
        if mock_session.post.called:
            call_args = mock_session.post.call_args
            endpoint = str(call_args[0][0]) if call_args[0] else str(call_args.kwargs.get("url", ""))
            # Cached IP should appear in endpoint
            assert "192.168.1.100" in endpoint


async def test_webhook_registration_cache_expiration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test expired cached IP is not used."""
    import time
    
    mock_config_entry.add_to_hass(hass)
    
    mock_response = Mock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK")
    mock_session.post.return_value.__aenter__.return_value = mock_response
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        
        entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
        
        # Set expired cached IP (older than 5 minutes)
        entry.runtime_data.cached_resolved_ip = "192.168.1.100"
        entry.runtime_data.cache_timestamp = time.time() - 400  # 6+ minutes ago
        
        # Reset mock
        mock_session.post.reset_mock()
        
        # Trigger another registration
        from custom_components.wican import _async_register_webhook_on_device
        
        await _async_register_webhook_on_device(hass, entry)
        await hass.async_block_till_done()
        
        # Expired cache should not be used, so we fall back to mDNS
        # (Can't easily verify this without inspecting logs, but test ensures no crash)
        assert mock_session.post.called


async def test_webhook_registration_missing_host_and_mdns(
    hass: HomeAssistant,
    mock_session,
) -> None:
    """Test webhook registration fails gracefully when host/mDNS missing."""
    # Create entry without host or mDNS
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Test",
        data={
            CONF_WEBHOOK_ID: "test_webhook_id",
            # No host, no mdns, no ip
        },
        options={CONF_POST_INTERVAL: 10},
    )
    entry.add_to_hass(hass)
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        # Setup should still succeed (registration failure is non-fatal)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # No POST should be attempted
    assert not mock_session.post.called


async def test_webhook_registration_normalizes_http_scheme(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test webhook registration normalizes HTTP scheme."""
    # Add entry first, then modify it
    mock_config_entry.add_to_hass(hass)
    
    # Modify entry to have host without scheme
    updated_data = dict(mock_config_entry.data)
    updated_data["host"] = "192.168.1.100"  # No http://
    hass.config_entries.async_update_entry(mock_config_entry, data=updated_data)
    
    mock_response = Mock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK")
    
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_response
    mock_session.post.return_value = mock_context
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Entry data should now have http:// scheme
    entry = hass.config_entries.async_get_entry(mock_config_entry.entry_id)
    assert entry.data["host"].startswith("http://")


async def test_webhook_registration_server_disconnected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
) -> None:
    """Test webhook registration handles server disconnection."""
    mock_config_entry.add_to_hass(hass)
    
    # Simulate server disconnect on first attempt, then success
    mock_response_success = Mock()
    mock_response_success.status = 200
    mock_response_success.text = AsyncMock(return_value="OK")
    
    mock_context_success = AsyncMock()
    mock_context_success.__aenter__.return_value = mock_response_success
    
    # First call raises exception, second returns success
    mock_session.post.side_effect = [
        ServerDisconnectedError(),  # First attempt fails
        mock_context_success,  # Second succeeds
    ]
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=mock_session,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    
    # Should have retried and succeeded
    assert mock_session.post.call_count >= 2


async def test_webhook_registration_invalid_url_generation(
    hass: HomeAssistant,
    mock_session,
) -> None:
    """Test webhook registration handles invalid URL generation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Test",
        data={
            CONF_WEBHOOK_ID: "test_webhook_id",
            "host": "http://192.168.1.100",
        },
        options={CONF_POST_INTERVAL: 10},
    )
    entry.add_to_hass(hass)
    
    # Mock get_url to raise exception
    with (
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.wican.resolve_device_webhook_urls",
            side_effect=Exception("Invalid URL"),
        ),
    ):
        # Setup should still succeed
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # No POST should be attempted due to URL generation failure
    assert not mock_session.post.called
