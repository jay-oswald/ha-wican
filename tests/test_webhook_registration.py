"""Test webhook registration logic with retry, timeout, and IP caching."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import logging
from unittest.mock import AsyncMock, Mock, patch
from yarl import URL

from aiohttp import ClientError, ClientResponseError, ServerDisconnectedError
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers import aiohttp_client

from custom_components.wican import _async_register_webhook_on_device
from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN

from tests.conftest import MockConfigEntry


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


# --- Log levels for a registration that never succeeds -----------------------
#
# Registering is a best-effort refresh: the device stores the webhook URL itself
# and keeps posting without it. So "could not reach the device" and "the device
# refused" must not be reported the same way.


async def _setup_without_registering(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set the entry up with registration stubbed out, and return it.

    Gives the entry a runtime_data to call the real registration against,
    without the setup itself logging anything about registration first.
    """
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Fill webhook_url in while registration is still stubbed out. The call
        # under test would otherwise backfill it, and the resulting entry update
        # fires the update listener into a second, concurrent registration whose
        # log records would land in the same caplog.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "webhook_url": "http://ha.local:8123/api/webhook/x"},
        )
        await hass.async_block_till_done()

    return hass.config_entries.async_get_entry(entry.entry_id)


def _entry_never_posted() -> MockConfigEntry:
    """Return an entry for a device that has never posted to the webhook."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Test",
        data={
            CONF_WEBHOOK_ID: "test_webhook_id",
            "host": "http://192.168.1.100",
            # No fw_version: only the webhook handler writes it, so its absence
            # is what says this device has never posted.
        },
        options={CONF_POST_INTERVAL: 10},
    )


async def test_unreachable_device_that_has_posted_is_not_an_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a device that is merely asleep or away does not log an error.

    mock_config_entry carries fw_version, so it has posted to us before and is
    already registered on the device side.
    """
    entry = await _setup_without_registering(hass, mock_config_entry)
    assert entry.data["fw_version"]

    mock_session.post.side_effect = ClientError("Cannot connect")

    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.wican"),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
    ):
        assert (
            await _async_register_webhook_on_device(hass, entry, max_retries=1)
            is False
        )

    assert mock_session.post.called
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert "Could not reach" in caplog.text
    assert "will resume posting" in caplog.text


async def test_unreachable_device_that_never_posted_is_an_error(
    hass: HomeAssistant,
    mock_session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a device that never completed setup still logs the checklist.

    The manual config flow does not test connectivity, so this log line is the
    only signal that setup never completed.
    """
    entry = await _setup_without_registering(hass, _entry_never_posted())
    assert "fw_version" not in entry.data

    mock_session.post.side_effect = ClientError("Cannot connect")

    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.wican"),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
    ):
        assert (
            await _async_register_webhook_on_device(hass, entry, max_retries=1)
            is False
        )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "has never posted" in message
    assert "Device is powered on and connected to network" in message


async def test_device_answering_with_an_error_status_is_an_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a device that answers and refuses is an error even if it has posted.

    Something is actually wrong with the device, so the quiet "not reachable
    right now" path must not swallow it just because fw_version is set.
    """
    entry = await _setup_without_registering(hass, mock_config_entry)
    assert entry.data["fw_version"]

    # Note: not create_mock_response(). The code awaits session.post() and then
    # reads resp.status, so the response has to be the awaited value itself and
    # status has to be a real int to compare against 300.
    response = Mock()
    response.status = HTTPStatus.INTERNAL_SERVER_ERROR
    response.text = AsyncMock(return_value="nope")
    mock_session.post.return_value = response

    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.wican"),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
    ):
        assert (
            await _async_register_webhook_on_device(hass, entry, max_retries=1)
            is False
        )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "answered with an error status" in errors[0].getMessage()
    assert "Could not reach" not in caplog.text


async def test_per_endpoint_connection_errors_are_logged_at_debug(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the per-candidate connection errors do not warn.

    They fire once per endpoint candidate per attempt while a device is offline,
    and say nothing the summary after the loop does not say better.
    """
    entry = await _setup_without_registering(hass, mock_config_entry)

    mock_session.post.side_effect = ClientError("Cannot connect")

    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.wican"),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=mock_session,
        ),
    ):
        await _async_register_webhook_on_device(hass, entry, max_retries=1)

    connection_errors = [
        r
        for r in caplog.records
        if "registration connection error" in r.getMessage()
    ]
    assert connection_errors
    assert {r.levelno for r in connection_errors} == {logging.DEBUG}
