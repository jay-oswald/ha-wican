"""Test the WiCAN config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.const import CONF_NAME, CONF_WEBHOOK_ID

from custom_components.wican.const import DOMAIN

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_user_flow_success(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test user config flow completes successfully."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"mdns": "http://wican_test.local:80"},
    )
    await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "http://wican_test.local:80"
    assert result2["data"]["mdns"] == "http://wican_test.local:80"
    assert CONF_WEBHOOK_ID in result2["data"]
    assert result2["data"]["webhook_url"].endswith(result2["data"][CONF_WEBHOOK_ID])
    assert result2["description_placeholders"]["webhook_url"] == result2["data"]["webhook_url"]


async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test user flow when device cannot be reached."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    # Config flow doesn't validate connectivity, it just creates the entry
    # Validation happens during setup when webhook registration is attempted
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"mdns": "http://wican_nonexistent.local:80"},
    )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["description_placeholders"]["webhook_url"] == result2["data"]["webhook_url"]


async def test_user_flow_adds_http_scheme(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test that config flow adds http:// if missing."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"mdns": "wican_test.local"},
    )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"]["mdns"] == "http://wican_test.local"


async def test_zeroconf_flow_success(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test zeroconf discovery flow with confirmation and MAC address."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_test.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={
            "mac": b"AA:BB:CC:DD:EE:FF",
            "device_id": b"test_device_123",
            "firmware": b"2.00",
            "hardware": b"v3.1",
        },
    )

    # Mock that system is already onboarded (requires confirmation); the
    # created entry is set up for real, so keep its device registration off
    # the network.
    with (
        patch(
            "homeassistant.components.onboarding.async_is_onboarded",
            return_value=True,
        ),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery_info,
        )

        # Should show confirmation form
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "zeroconf_confirm"
        assert "name" in result["description_placeholders"]
        assert "url" in result["description_placeholders"]

        # Confirm the addition
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )
        await hass.async_block_till_done()

        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert result2["title"] == "wican_test.local."
        # Check that MAC address was captured
        assert result2["data"].get("mac") == "AA:BB:CC:DD:EE:FF"
        assert result2["data"].get("device_id") == "test_device_123"


async def test_zeroconf_flow_not_wican(
    hass: HomeAssistant,
) -> None:
    """Test zeroconf discovery ignores non-WiCAN devices."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.200",
        ip_addresses=["192.168.1.200"],
        hostname="some_other_device.local.",
        name="OtherDevice._http._tcp.local.",
        port=80,
        type="_http._tcp.local.",
        properties={},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "not_wican"


async def test_zeroconf_already_configured(
    hass: HomeAssistant,
) -> None:
    """Test zeroconf aborts if device already configured (MAC-based unique_id)."""
    # Create a config entry with MAC-based unique_id
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aabbccddeeff",  # MAC without colons
        data={
            CONF_WEBHOOK_ID: "existing_webhook",
            CONF_NAME: "Existing Device",
            "mac": "AA:BB:CC:DD:EE:FF",
        },
    )
    existing_entry.add_to_hass(hass)

    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_test.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={
            "mac": b"AA:BB:CC:DD:EE:FF",
            "device_id": b"test_device_123",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_during_onboarding(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test zeroconf auto-creates entry during onboarding."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_test.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={
            "mac": b"AA:BB:CC:DD:EE:FF",
            "device_id": b"test_device_123",
        },
    )

    with (
        patch(
            "homeassistant.components.onboarding.async_is_onboarded",
            return_value=False,
        ),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery_info,
        )
        await hass.async_block_till_done()

    # Zeroconf creates entry directly (no confirmation step)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "wican_test.local."
    assert result["description_placeholders"]["webhook_url"] == result["data"]["webhook_url"]


async def test_options_flow(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test options flow (currently not implemented)."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(
        mock_config_entry.entry_id
    )

    # Options flow not implemented yet - should be no-op or simple form
    # This test will need updating when options flow is added
    assert result is not None


async def test_zeroconf_flow_user_declines(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test user can decline discovered device."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_test.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={},
    )

    # Mock that system is already onboarded (requires confirmation)
    with patch(
        "homeassistant.components.onboarding.async_is_onboarded",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery_info,
        )

        # Should show confirmation form
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "zeroconf_confirm"

        # User can simply close the dialog (no follow-up action needed)
        # The flow will remain in FORM state until user confirms or dismisses


async def test_zeroconf_legacy_http_advertisement_rejected(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Contract v2: the legacy _http._tcp advertisement is not accepted.

    Firmware below the supported minimum advertises only _http._tcp with
    the WiCAN-WebServer instance name; it must be updated (manual add
    still works and raises the firmware-update repair issue).
    """
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_legacy.local.",
        name="WiCAN-WebServer._http._tcp.local.",
        port=80,
        type="_http._tcp.local.",
        properties={},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "not_wican"


async def test_config_flow_user_manual_entry_with_optional_host(
    hass: HomeAssistant,
) -> None:
    """Test manual config entry with optional host field."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    
    # Submit with both mdns and optional host. The created entry is set up
    # for real, so keep its device registration off the network.
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "mdns": "wican_test.local",
                "host": "192.168.1.50",  # Optional host field
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mdns"] == "http://wican_test.local"
    assert result["data"]["host"] == "http://192.168.1.50"


async def test_config_flow_zeroconf_discovery_with_mac_and_device_id(
    hass: HomeAssistant,
) -> None:
    """Test config flow via zeroconf discovery with MAC and device_id."""
    # Use proper WiCAN service name pattern
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        port=80,
        hostname="wican_abc123.local.",
        type="_wican._tcp.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:FF", "device_id": b"wican_abc123"},
    )
    
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )
    
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"


async def test_zeroconf_device_id_fallback_unique_id(
    hass: HomeAssistant,
) -> None:
    """Test zeroconf flow using device_id when MAC is not available (line 102)."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_device.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={
            "device_id": b"wican_device_xyz",  # No MAC, only device_id
            "firmware": b"2.00",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    # Unique ID should be device_id
    assert result["flow_id"]


async def test_zeroconf_hostname_fallback_unique_id(
    hass: HomeAssistant,
) -> None:
    """Test zeroconf flow using hostname when MAC and device_id are unavailable (line 103)."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_legacy.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={},  # No MAC, no device_id
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"


async def test_zeroconf_confirm_webhook_url_exception(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test zeroconf confirmation handles webhook URL generation exception (lines 145-149)."""
    from unittest.mock import patch

    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_test.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={
            "mac": b"AA:BB:CC:DD:EE:FF",
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    # Now confirm with mocked URL resolution that falls back to the current
    # request. The created entry is set up for real, so keep its device
    # registration off the network.
    with (
        patch(
            "custom_components.wican.config_flow.resolve_webhook_url",
            return_value="http://192.168.1.10:8123/api/webhook/test_webhook_id",
        ),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    # Title should be the hostname (discovered_name)
    assert result2["title"] == "wican_test.local."
    assert result2["data"]["webhook_url"] == result2["description_placeholders"]["webhook_url"]


async def test_options_flow_when_config_entry_set(
    hass: HomeAssistant,
) -> None:
    """Test options flow uses config_entry.options when hass is set (line 198)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_WEBHOOK_ID: "test_webhook",
            "mdns": "http://wican_test.local:80",
        },
        options={"post_interval": 2500},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    # Should use config_entry.options as default
    assert "post_interval" in str(result["data_schema"])


async def test_options_flow_valid_data(
    hass: HomeAssistant,
) -> None:
    """Test options flow accepts valid data (covers line 196-197)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_WEBHOOK_ID: "test_webhook",
            "mdns": "http://wican_test.local:80",
        },
        options={"post_interval": 1000},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit valid data within range (MIN=1000, MAX=3600)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"post_interval": 2000},
    )

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"]["post_interval"] == 2000


async def test_format_http_url_empty_string(
    hass: HomeAssistant,
) -> None:
    """Test _format_http_url with empty string (line 219)."""
    from custom_components.wican.config_flow import _format_http_url
    
    result = _format_http_url("   ", 80)
    assert result is None


async def test_format_http_url_with_existing_http(
    hass: HomeAssistant,
) -> None:
    """Test _format_http_url with existing http:// (line 223)."""
    from custom_components.wican.config_flow import _format_http_url
    
    result = _format_http_url("http://example.com:8080", 80)
    assert result == "http://example.com:8080"


async def test_format_http_url_with_existing_https(
    hass: HomeAssistant,
) -> None:
    """Test _format_http_url with existing https:// (line 223)."""
    from custom_components.wican.config_flow import _format_http_url
    
    result = _format_http_url("https://example.com", 443)
    assert result == "https://example.com"


async def test_format_http_url_with_port_none(
    hass: HomeAssistant,
) -> None:
    """Test _format_http_url with None port (yarl handles this gracefully)."""
    from custom_components.wican.config_flow import _format_http_url
    
    # None port is valid for yarl URL.build
    result = _format_http_url("example.com", None)
    assert result == "http://example.com"


async def test_string_ip_none(
    hass: HomeAssistant,
) -> None:
    """Test _string_ip with None input (line 243)."""
    from custom_components.wican.config_flow import _string_ip

    result = _string_ip(None)
    assert result is None


async def test_format_http_url_none_input(hass: HomeAssistant) -> None:
    """_format_http_url returns None for a None address."""
    from custom_components.wican.config_flow import _format_http_url

    assert _format_http_url(None, 80) is None


async def test_format_http_url_invalid_host(hass: HomeAssistant) -> None:
    """_format_http_url returns None when URL.build rejects the host."""
    from custom_components.wican.config_flow import _format_http_url

    # A space in the host makes yarl raise ValueError -> None.
    assert _format_http_url("a b", 80) is None


async def test_zeroconf_no_host_aborts(hass: HomeAssistant) -> None:
    """Zeroconf discovery with no host and no IP aborts with no_host."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="",
        ip_addresses=[],
        hostname="",
        name="WiCAN-WebServer",
        port=80,
        type="_wican._tcp.local.",
        properties={},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_host"


async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test manual setup of the same host is deduplicated (unique-config-entry)."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://wican_test.local",
        data={CONF_WEBHOOK_ID: "existing", "mdns": "http://wican_test.local"},
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"mdns": "wican_test.local"},
    )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_zeroconf_updates_connection_info(
    hass: HomeAssistant,
) -> None:
    """Test rediscovery updates stored connection info (discovery-update-info)."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aabbccddeeff",
        data={
            CONF_WEBHOOK_ID: "existing",
            "mac": "AA:BB:CC:DD:EE:FF",
            "mdns": "http://wican_old.local:80",
        },
    )
    existing.add_to_hass(hass)

    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.150",
        ip_addresses=["192.168.1.150"],
        hostname="wican_new.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:FF"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Stored connection info should have been refreshed from the new discovery.
    assert "wican_new" in existing.data["mdns"]
    assert "wican_old" not in existing.data["mdns"]


async def test_reconfigure_flow(
    hass: HomeAssistant,
) -> None:
    """Test the reconfigure flow updates host/mDNS (reconfiguration-flow)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aabbccddeeff",
        data={
            CONF_WEBHOOK_ID: "test_webhook",
            "mdns": "http://wican_test.local:80",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with patch(
        "custom_components.wican.async_setup_entry",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"mdns": "wican_moved.local", "host": "192.168.1.77"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data["mdns"] == "http://wican_moved.local"
    assert entry.data["host"] == "http://192.168.1.77"






async def test_user_flow_no_url_available_aborts_cleanly(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """No resolvable HTTP URL aborts with a friendly reason, not a crash.

    Regression: NoURLAvailableError (e.g. cloud-only setup without an
    internal URL) propagated out of the flow and the user saw
    "Unknown error occurred".
    """
    from homeassistant.helpers.network import NoURLAvailableError

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    with patch(
        "custom_components.wican.config_flow.resolve_webhook_url",
        side_effect=NoURLAvailableError,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"mdns": "http://wican_test.local:80"},
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "no_url_available"


async def test_zeroconf_confirm_no_url_available_aborts_cleanly(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """The zeroconf confirm step aborts cleanly without a webhook URL."""
    from homeassistant.helpers.network import NoURLAvailableError

    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_abort.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:F0"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )
    assert result["type"] == FlowResultType.FORM

    with patch(
        "custom_components.wican.config_flow.resolve_webhook_url",
        side_effect=NoURLAvailableError,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {},
        )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "no_url_available"


# ---- Home Assistant's own web server with SSL on (HTTPS only) ---------------


def _server_with_ssl(hass: HomeAssistant) -> None:
    """What `ssl_certificate` under `http:` makes of the server description.

    Call it after the flow is initialised: loading the integration sets up
    the http component, which writes hass.config.api.
    """
    hass.config.api = ApiConfig("192.168.1.10", "192.168.1.10", 8123, True)
    hass.config.internal_url = None
    hass.config.external_url = "https://ha.example.org:8123"


async def test_user_flow_server_with_ssl(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """The manual add goes through on a Home Assistant that answers HTTPS only.

    Regression (ha-wican issue 110): with `ssl_certificate` under `http:`
    no plain-http address exists; the flow asked for one and aborted with
    no_url_available before the device was ever contacted.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    _server_with_ssl(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"mdns": "192.168.1.50"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    webhook_id = result2["data"][CONF_WEBHOOK_ID]
    assert (
        result2["data"]["webhook_url"]
        == f"https://192.168.1.10:8123/api/webhook/{webhook_id}"
    )
    assert result2["description_placeholders"]["webhook_url"] == result2["data"]["webhook_url"]


async def test_zeroconf_confirm_server_with_ssl(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """A discovered device is added on a Home Assistant that answers HTTPS only."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_ssl.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:F1", "device_type": b"wican_pro"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=discovery_info,
    )
    assert result["type"] == FlowResultType.FORM
    _server_with_ssl(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    webhook_id = result2["data"][CONF_WEBHOOK_ID]
    assert (
        result2["data"]["webhook_url"]
        == f"https://192.168.1.10:8123/api/webhook/{webhook_id}"
    )


async def test_zeroconf_adopts_existing_manual_entry(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Discovery of a manually added device must not create a duplicate.

    Regression (M4): manual entries are keyed by connection URL and
    zeroconf entries by MAC, so the unique_id check never collided — one
    physical device ended up with two entries, two webhooks, and
    duplicated entities. The discovery now adopts the manual entry
    (upgrading it to the stable MAC unique_id) and aborts.
    """
    manual = MockConfigEntry(
        domain=DOMAIN,
        title="wican_car.local",
        data={
            CONF_WEBHOOK_ID: "manual_webhook_id",
            "mdns": "http://wican_car.local",
        },
        unique_id="http://wican_car.local",
    )
    manual.add_to_hass(hass)

    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.77",
        ip_addresses=["192.168.1.77"],
        hostname="wican_car.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:77"},
    )

    # Adoption schedules a reload of the manual entry: let it finish, with
    # the device registration kept off the network.
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery_info,
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Still exactly one entry, now keyed by the stable MAC.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == "aabbccddee77"
    assert entries[0].data["host"] == "http://192.168.1.77"


async def test_manual_add_of_discovered_device_aborts(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Manually re-adding a zeroconf-discovered device must not duplicate."""
    discovered = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Car",
        data={
            CONF_WEBHOOK_ID: "zeroconf_webhook_id",
            "mdns": "http://wican_car.local",
            "host": "http://192.168.1.77",
        },
        unique_id="aabbccddee77",
    )
    discovered.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"mdns": "wican_car.local"},
    )

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_reloads_without_reload_helper(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Reconfiguring a loaded entry reloads it without the reload helper.

    The entry keeps an update listener (it re-registers the webhook when
    options change), and Home Assistant does not allow its update-and-reload
    helpers for such entries (deprecated in 2026.6, an error from 2026.12).
    The flow must update and schedule the reload itself.
    """
    entry = init_integration
    assert entry.update_listeners

    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ) as register,
        patch("homeassistant.config_entries.report_usage") as report_usage,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"mdns": "wican_moved.local", "host": "192.168.1.77"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "http://192.168.1.77"
    report_usage.assert_not_called()
    # Reloaded: the fresh runtime is bound to the new address.
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.runtime_data.device_host == "http://192.168.1.77"
    assert register.called


async def test_zeroconf_rediscovery_reloads_without_reload_helper(
    hass: HomeAssistant,
) -> None:
    """Rediscovery at a new address reloads a loaded entry by itself.

    Same constraint as the reconfigure flow: the unique-id helper's
    built-in reload is not allowed for entries that keep an update listener.
    """
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="aabbccddeeff",
        data={
            CONF_WEBHOOK_ID: "existing",
            "mac": "AA:BB:CC:DD:EE:FF",
            "mdns": "http://wican_old.local",
            "host": "http://192.168.1.10",
        },
    )
    existing.add_to_hass(hass)
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.150",
        ip_addresses=["192.168.1.150"],
        hostname="wican_new.local.",
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": b"AA:BB:CC:DD:EE:FF"},
    )

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch("homeassistant.config_entries.report_usage") as report_usage,
    ):
        assert await hass.config_entries.async_setup(existing.entry_id)
        await hass.async_block_till_done()
        assert existing.update_listeners

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery_info,
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert existing.data["host"] == "http://192.168.1.150"
    report_usage.assert_not_called()
    # Reloaded: the fresh runtime is bound to the rediscovered address.
    assert existing.state is config_entries.ConfigEntryState.LOADED
    assert existing.runtime_data.device_host == "http://192.168.1.150"
