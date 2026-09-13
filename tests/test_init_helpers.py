"""Test __init__.py helper functions and edge cases."""

from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from aiohttp.web import Request

from custom_components.wican import (
    _ensure_http_scheme,
    _http_url_from_host,
    _build_webhook_endpoint,
    _normalize_ip,
    _extract_request_ip,
)


def test_ensure_http_scheme_with_none():
    """Test _ensure_http_scheme with None value."""
    assert _ensure_http_scheme(None) is None


def test_ensure_http_scheme_with_empty_string():
    """Test _ensure_http_scheme with empty string."""
    assert _ensure_http_scheme("") == ""


def test_ensure_http_scheme_already_http():
    """Test _ensure_http_scheme with http:// prefix."""
    assert _ensure_http_scheme("http://example.com") == "http://example.com"


def test_ensure_http_scheme_already_https():
    """Test _ensure_http_scheme with https:// prefix."""
    assert _ensure_http_scheme("https://example.com") == "https://example.com"


def test_ensure_http_scheme_adds_http():
    """Test _ensure_http_scheme adds http:// prefix."""
    assert _ensure_http_scheme("example.com") == "http://example.com"
    assert _ensure_http_scheme("192.168.1.1") == "http://192.168.1.1"


def test_http_url_from_host_with_none():
    """Test _http_url_from_host with None."""
    assert _http_url_from_host(None) is None


def test_http_url_from_host_with_empty():
    """Test _http_url_from_host with empty string."""
    assert _http_url_from_host("") is None


def test_http_url_from_host_simple():
    """Test _http_url_from_host with simple hostname."""
    result = _http_url_from_host("wican.local")
    assert result == "http://wican.local"


def test_http_url_from_host_with_port():
    """Test _http_url_from_host with port."""
    result = _http_url_from_host("wican.local", 8080)
    assert result == "http://wican.local:8080"


def test_http_url_from_host_with_ip():
    """Test _http_url_from_host with IP address."""
    result = _http_url_from_host("192.168.1.100")
    assert result == "http://192.168.1.100"


def test_http_url_from_host_invalid():
    """Test _http_url_from_host with invalid host."""
    # Invalid characters that would cause URL.build to raise ValueError
    result = _http_url_from_host("invalid host with spaces")
    # Should return None on ValueError
    assert result is None


def test_build_webhook_endpoint_with_none():
    """Test _build_webhook_endpoint with None."""
    assert _build_webhook_endpoint(None) is None


def test_build_webhook_endpoint_with_empty():
    """Test _build_webhook_endpoint with empty string."""
    assert _build_webhook_endpoint("") is None


def test_build_webhook_endpoint_simple():
    """Test _build_webhook_endpoint with simple URL."""
    result = _build_webhook_endpoint("http://wican.local")
    assert result == URL("http://wican.local/api/webhook")


def test_build_webhook_endpoint_no_scheme():
    """Test _build_webhook_endpoint without scheme."""
    result = _build_webhook_endpoint("wican.local")
    assert result == URL("http://wican.local/api/webhook")


def test_build_webhook_endpoint_with_path():
    """Test _build_webhook_endpoint strips existing path."""
    result = _build_webhook_endpoint("http://wican.local/old/path")
    assert result == URL("http://wican.local/api/webhook")


def test_build_webhook_endpoint_with_query():
    """Test _build_webhook_endpoint strips query string."""
    result = _build_webhook_endpoint("http://wican.local?query=param")
    assert result == URL("http://wican.local/api/webhook")


def test_build_webhook_endpoint_invalid_url():
    """Test _build_webhook_endpoint with invalid URL."""
    # URL with invalid characters that will cause ValueError in URL()
    # Using characters that yarl.URL rejects
    result = _build_webhook_endpoint("http://[invalid:url")
    assert result is None


def test_build_webhook_endpoint_no_scheme_invalid():
    """Test _build_webhook_endpoint with invalid URL after adding scheme."""
    # Test the second ValueError catch when adding scheme fails
    with patch("custom_components.wican._ensure_http_scheme", side_effect=ValueError):
        result = _build_webhook_endpoint("no-scheme-host")
        assert result is None


def test_normalize_ip_with_none():
    """Test _normalize_ip with None."""
    assert _normalize_ip(None) is None


def test_normalize_ip_with_empty():
    """Test _normalize_ip with empty string."""
    result = _normalize_ip("")
    # Empty string is falsy, returns None
    assert result is None


def test_normalize_ip_ipv4():
    """Test _normalize_ip with IPv4 address."""
    assert _normalize_ip("192.168.1.100") == "192.168.1.100"


def test_normalize_ip_ipv6():
    """Test _normalize_ip with IPv6 address."""
    assert _normalize_ip("2001:db8::1") == "2001:db8::1"


def test_normalize_ip_ipv4_mapped():
    """Test _normalize_ip with IPv4-mapped IPv6 address."""
    assert _normalize_ip("::ffff:192.168.1.100") == "192.168.1.100"


def test_normalize_ip_ipv4_mapped_alternate():
    """Test _normalize_ip with alternate IPv4-mapped format."""
    assert _normalize_ip("::ffff:c0a8:0164") == "c0a8:0164"


def test_extract_request_ip_from_remote():
    """Test _extract_request_ip from request.remote."""
    request = MagicMock(spec=Request)
    request.headers = {}
    request.remote = "172.16.0.1"

    result = _extract_request_ip(request)
    assert result == "172.16.0.1"


def test_extract_request_ip_normalizes_ipv4_mapped_remote():
    """Test _extract_request_ip normalizes an IPv4-mapped IPv6 remote."""
    request = MagicMock(spec=Request)
    request.headers = {}
    request.remote = "::ffff:10.0.0.1"

    result = _extract_request_ip(request)
    assert result == "10.0.0.1"


def test_extract_request_ip_no_ip_found():
    """Test _extract_request_ip when no IP is available."""
    request = MagicMock(spec=Request)
    request.headers = {}
    request.remote = None

    result = _extract_request_ip(request)
    assert result is None


def test_extract_request_ip_ignores_x_forwarded_for():
    """An untrusted X-Forwarded-For must not override the peer address.

    The stored IP is where the integration POSTs the webhook registration,
    which carries the webhook URL. Home Assistant resolves X-Forwarded-For
    into request.remote itself, but only when the operator configured
    use_x_forwarded_for with trusted_proxies; honouring the raw header would
    let any client that can reach the webhook redirect that POST.
    """
    request = MagicMock(spec=Request)
    request.headers = {"X-Forwarded-For": "203.0.113.1, 198.51.100.1"}
    request.remote = "192.168.1.50"

    result = _extract_request_ip(request)
    assert result == "192.168.1.50"


def test_extract_request_ip_ignores_transport_peername():
    """The raw transport bypasses the forwarded middleware, so it is not used."""
    request = MagicMock(spec=Request)
    request.headers = {}
    transport = MagicMock()
    transport.get_extra_info.return_value = ("198.51.100.9", 12345)
    request.transport = transport
    request.remote = "192.168.1.50"

    result = _extract_request_ip(request)
    assert result == "192.168.1.50"
