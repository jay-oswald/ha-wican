"""Test __init__.py webhook setup and registration logic."""

from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, MagicMock, Mock
from uuid import uuid4
import time

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_WEBHOOK_ID

from custom_components.wican.const import DOMAIN, CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL
from tests.conftest import MockConfigEntry


def _accepting_session() -> Mock:
    """Return a session whose POST succeeds (the device accepts registration)."""
    response = Mock()
    response.status = 200
    response.read = AsyncMock(return_value=b"")
    session = Mock()
    session.post = AsyncMock(return_value=response)
    return session


async def test_setup_entry_generates_missing_webhook_id(hass: HomeAssistant) -> None:
    """Test that setup generates webhook_id if missing."""
    # Create entry without webhook_id (older entries)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mdns": "http://wican_test.local"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Verify no webhook_id initially
    assert CONF_WEBHOOK_ID not in entry.data
    
    # Setup should generate webhook_id
    with patch("custom_components.wican._async_register_webhook_on_device", return_value=True):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    assert result is True
    # webhook_id should be generated and added
    assert CONF_WEBHOOK_ID in entry.data
    assert len(entry.data[CONF_WEBHOOK_ID]) == 32  # uuid4().hex length


async def test_setup_entry_webhook_id_generation_failure(hass: HomeAssistant) -> None:
    """Test setup handles webhook_id generation failure."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mdns": "http://wican_test.local"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Mock uuid4 inside uuid module to raise exception
    with patch("uuid.uuid4", side_effect=Exception("UUID generation failed")):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # Setup should fail gracefully
    assert result is False


async def test_setup_failure_after_webhook_register_allows_retry(
    hass: HomeAssistant,
) -> None:
    """A failed setup unregisters its webhook so the retry can register it.

    Regression: the webhook stayed registered when platform setup raised,
    so the next setup attempt hit core's "Handler is already defined!"
    ValueError — permanently wedging the entry until HA restart.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "mdns": "http://wican_test.local",
            CONF_WEBHOOK_ID: "retry_webhook_id",
        },
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=ImportError("simulated broken platform"),
        ),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert result is False
    # The webhook must not linger after the failed setup.
    assert "retry_webhook_id" not in hass.data.get("webhook", {})

    # The retry (reload of an unloaded entry just loads it) must succeed.
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert "retry_webhook_id" in hass.data.get("webhook", {})


async def test_setup_entry_uses_custom_post_interval(hass: HomeAssistant) -> None:
    """Test setup uses custom post interval from options."""
    custom_interval = 30
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mdns": "http://wican_test.local", CONF_WEBHOOK_ID: "test_webhook"},
        options={CONF_POST_INTERVAL: custom_interval},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    with patch("custom_components.wican._async_register_webhook_on_device", return_value=True):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    assert result is True
    assert entry.runtime_data.post_interval == custom_interval


async def test_setup_entry_default_post_interval(hass: HomeAssistant) -> None:
    """Test setup uses default post interval when not specified."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mdns": "http://wican_test.local", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    with patch("custom_components.wican._async_register_webhook_on_device", return_value=True):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    assert result is True
    assert entry.runtime_data.post_interval == DEFAULT_POST_INTERVAL


async def test_webhook_registration_normalizes_mdns_scheme(hass: HomeAssistant) -> None:
    """Test webhook registration normalizes mdns to include http:// scheme."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mdns": "wican_test.local", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Mock successful webhook registration
    mock_session = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_session.post.return_value.__aenter__.return_value = mock_response
    
    with patch("custom_components.wican.async_get_clientsession", return_value=mock_session):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # mdns should be normalized to include scheme
    assert entry.data["mdns"] == "http://wican_test.local"


async def test_webhook_registration_normalizes_host_scheme(hass: HomeAssistant) -> None:
    """Test webhook registration normalizes host to include http:// scheme."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.168.1.100", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Mock successful webhook registration
    mock_session = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_session.post.return_value.__aenter__.return_value = mock_response
    
    with patch("custom_components.wican.async_get_clientsession", return_value=mock_session):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # host should be normalized to include scheme
    assert entry.data["host"] == "http://192.168.1.100"


@pytest.mark.real_webhook_registration
async def test_webhook_registration_derives_host_from_ip(hass: HomeAssistant) -> None:
    """Test webhook registration derives host from IP when host is missing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"ip": "192.168.1.50", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # The real registration runs (it derives the host) against a device that
    # accepts it, so nothing touches the network.
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=_accepting_session(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # Runtime data should have derived host from IP
    assert entry.runtime_data.device_host == "http://192.168.1.50"


@pytest.mark.real_webhook_registration
async def test_webhook_registration_caches_successful_ip(hass: HomeAssistant) -> None:
    """Test that successful webhook registration caches the resolved IP."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "http://192.168.1.100", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=_accepting_session(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    assert entry.runtime_data.device_host == "http://192.168.1.100"
    assert entry.runtime_data.cached_resolved_ip == "192.168.1.100"
    assert entry.runtime_data.cache_timestamp > 0


async def test_webhook_registration_skips_cache_for_mdns(hass: HomeAssistant) -> None:
    """Test that .local addresses are not cached."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"mdns": "http://wican_device.local", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Mock successful webhook registration
    mock_session = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_session.post.return_value.__aenter__.return_value = mock_response
    
    with patch("custom_components.wican.async_get_clientsession", return_value=mock_session):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # .local addresses should not be cached
    assert entry.runtime_data.cached_resolved_ip is None
    assert entry.runtime_data.cache_timestamp == 0.0


async def test_webhook_registration_with_ip_host(hass: HomeAssistant) -> None:
    """Test webhook registration works with IP address host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "http://192.168.1.100", CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Mock successful webhook registration
    with patch("custom_components.wican._async_register_webhook_on_device", return_value=True):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    
    # Verify setup succeeded
    assert entry.runtime_data is not None
    assert entry.runtime_data.device_host == "http://192.168.1.100"


async def test_webhook_registration_no_host_or_mdns(hass: HomeAssistant) -> None:
    """Test webhook registration fails gracefully when no host/mdns available."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_WEBHOOK_ID: "test_webhook"},
        title="WiCAN Test",
    )
    entry.add_to_hass(hass)
    
    # Setup should still succeed but webhook registration will skip
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    
    assert result is True  # Setup succeeds, just webhook registration skipped
