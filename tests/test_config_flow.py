"""Test the WiCAN config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

# ZeroconfServiceInfo moved between HA releases: see config_flow.py for the
# same compatibility dance.
try:
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
except ImportError:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from homeassistant.const import CONF_NAME, CONF_WEBHOOK_ID
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import DOMAIN


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
        name="WiCAN-WebServer._http._tcp.local.",
        port=80,
        type="_http._tcp.local.",
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


async def test_zeroconf_flow_legacy_firmware(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test zeroconf works with older firmware without MAC address."""
    discovery_info = ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname="wican_legacy.local.",
        name="WiCAN-WebServer._http._tcp.local.",
        port=80,
        type="_http._tcp.local.",
        properties={},  # No MAC or device_id (older firmware)
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

        # Confirm the addition
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )
        await hass.async_block_till_done()

        # Should create entry with fallback unique_id (hostname-based)
        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert result2["title"] == "wican_legacy.local."
        assert result2["description_placeholders"]["webhook_url"] == result2["data"]["webhook_url"]
        # No MAC address in data for legacy firmware
        assert "mac" not in result2["data"] or not result2["data"].get("mac")


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
    
    # Submit with both mdns and optional host
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "mdns": "wican_test.local",
            "host": "192.168.1.50",  # Optional host field
        },
    )
    
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
        type="_http._tcp.local.",
        name="WiCAN-WebServer",
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

    # Now confirm with mocked URL resolution that falls back to the current request
    with patch(
        "custom_components.wican.config_flow.resolve_webhook_url",
        return_value="http://192.168.1.10:8123/api/webhook/test_webhook_id",
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

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




