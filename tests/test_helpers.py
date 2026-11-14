"""Tests for helper functions (webhook URL resolution)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from custom_components.wican.helpers import (
    NoPlainHttpURLError,
    _server_base_url,
    resolve_device_webhook_urls,
    resolve_local_webhook_url,
    resolve_webhook_url,
    server_uses_ssl,
)


def test_resolve_device_webhook_urls_prefers_local_http_and_external_https(
    hass: HomeAssistant,
) -> None:
    """Test devices prefer local HTTP and append external HTTPS when available."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            return_value="http://homeassistant.local:8123/api/webhook/test",
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            return_value="https://example.ui.nabu.casa/api/webhook/test",
        ),
    ):
        urls = resolve_device_webhook_urls(
            hass,
            "test",
            device_tls=True,
        )

    assert urls == [
        "http://homeassistant.local:8123/api/webhook/test",
        "https://example.ui.nabu.casa/api/webhook/test",
    ]


def test_resolve_device_webhook_urls_falls_back_to_external_https(
    hass: HomeAssistant,
) -> None:
    """Test Pro devices can fall back to external HTTPS when local HTTP is unavailable."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            side_effect=NoURLAvailableError,
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            return_value="https://example.ui.nabu.casa/api/webhook/test",
        ),
    ):
        urls = resolve_device_webhook_urls(
            hass,
            "test",
            device_tls=True,
        )

    assert urls == ["https://example.ui.nabu.casa/api/webhook/test"]


def test_resolve_device_webhook_urls_requires_local_http_without_fallback(
    hass: HomeAssistant,
) -> None:
    """Test non-Pro devices still require a local HTTP webhook URL."""
    with patch(
        "custom_components.wican.helpers.resolve_local_webhook_url",
        side_effect=NoURLAvailableError,
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_device_webhook_urls(hass, "test")


def test_ensure_allowed_scheme_rejects_malformed_url() -> None:
    """A malformed URL raises NoURLAvailableError (covers ValueError branch)."""
    from custom_components.wican.helpers import _ensure_allowed_webhook_scheme

    with pytest.raises(NoURLAvailableError):
        _ensure_allowed_webhook_scheme("http://[bad", allowed_schemes={"http"})


def test_ensure_allowed_scheme_rejects_disallowed_scheme() -> None:
    """A disallowed scheme raises NoURLAvailableError."""
    from custom_components.wican.helpers import _ensure_allowed_webhook_scheme

    with pytest.raises(NoURLAvailableError):
        _ensure_allowed_webhook_scheme("https://x/api", allowed_schemes={"http"})


def test_resolve_local_webhook_url_uses_current_request(hass: HomeAssistant) -> None:
    """Falls back to the active request host when the normal URL is unavailable."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=[
            NoURLAvailableError,
            "http://192.168.1.10:8123",
        ],
    ):
        url = resolve_local_webhook_url(
            hass, "wid", require_current_request=True,
        )
    assert url.startswith("http://192.168.1.10:8123/api/webhook/")


def test_resolve_local_webhook_url_uses_fallback(hass: HomeAssistant) -> None:
    """Falls back to a stored HTTP fallback URL when no URL can be resolved."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=NoURLAvailableError,
    ):
        url = resolve_local_webhook_url(
            hass,
            "wid",
            fallback_url="http://192.168.1.20:8123/api/webhook/wid",
        )
    assert url == "http://192.168.1.20:8123/api/webhook/wid"


def test_resolve_local_webhook_url_raises_when_nothing_available(
    hass: HomeAssistant,
) -> None:
    """Raises the original error when no URL, request, or fallback is available."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=NoURLAvailableError,
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_local_webhook_url(hass, "wid")


def test_resolve_local_webhook_url_current_request_and_fallback_fail(
    hass: HomeAssistant,
) -> None:
    """A disallowed fallback scheme is ignored and the original error re-raised."""
    from custom_components.wican.helpers import resolve_local_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        side_effect=NoURLAvailableError,
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_local_webhook_url(
                hass,
                "wid",
                require_current_request=True,
                # A plain-http server: an https stored URL is not its own.
                fallback_url="https://example.com/api/webhook/wid",
            )


def test_resolve_external_webhook_url(hass: HomeAssistant) -> None:
    """resolve_external_webhook_url builds an http/https URL from the external base."""
    from custom_components.wican.helpers import resolve_external_webhook_url

    with patch(
        "custom_components.wican.helpers.get_url",
        return_value="https://example.ui.nabu.casa",
    ):
        url = resolve_external_webhook_url(hass, "wid")
    assert url.startswith("https://example.ui.nabu.casa/api/webhook/")


def test_resolve_device_webhook_urls_external_https_unavailable(
    hass: HomeAssistant,
) -> None:
    """Local HTTP is returned even when the external HTTPS lookup fails."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            return_value="http://homeassistant.local:8123/api/webhook/test",
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            side_effect=NoURLAvailableError,
        ),
    ):
        urls = resolve_device_webhook_urls(
            hass, "test", device_tls=True,
        )
    assert urls == ["http://homeassistant.local:8123/api/webhook/test"]


def test_resolve_device_webhook_urls_raises_when_all_unavailable(
    hass: HomeAssistant,
) -> None:
    """With fallback allowed but nothing resolvable, the local error is raised."""
    with (
        patch(
            "custom_components.wican.helpers.resolve_local_webhook_url",
            side_effect=NoURLAvailableError,
        ),
        patch(
            "custom_components.wican.helpers.resolve_external_https_webhook_url",
            side_effect=NoURLAvailableError,
        ),
    ):
        with pytest.raises(NoURLAvailableError):
            resolve_device_webhook_urls(
                hass, "test", device_tls=True,
            )


# ---- the scheme follows Home Assistant's own web server ---------------------
#
# These run Home Assistant's real get_url against a described server: the
# address by IP, the port, SSL on or off (what `ssl_certificate` under `http:`
# makes of hass.config.api), and the two URLs of Settings > System > Network.

HA_IP = "192.168.1.10"
HA_NAME = "ha.example.org"
BY_IP_HTTP = f"http://{HA_IP}:8123/api/webhook/wid"
BY_IP_HTTPS = f"https://{HA_IP}:8123/api/webhook/wid"
BY_NAME_HTTPS = f"https://{HA_NAME}:8123/api/webhook/wid"


def _server(
    hass: HomeAssistant,
    *,
    ssl: bool,
    internal: str | None = None,
    external: str | None = None,
    local_ip: str = HA_IP,
) -> None:
    """Describe Home Assistant's web server and its configured URLs."""
    hass.config.api = ApiConfig(local_ip, local_ip, 8123, ssl)
    hass.config.internal_url = internal
    hass.config.external_url = external


@pytest.mark.parametrize(
    ("internal", "external", "expected"),
    [
        # nothing configured: the server's own address, in https
        (None, None, [BY_IP_HTTPS]),
        # the usual DuckDNS shape: only the Internet URL is set
        (None, f"https://{HA_NAME}:8123", [BY_IP_HTTPS, BY_NAME_HTTPS]),
        # the Local network URL wins the first slot; same as the Internet
        # URL, so one URL
        (f"https://{HA_NAME}:8123", f"https://{HA_NAME}:8123", [BY_NAME_HTTPS]),
        (f"https://{HA_IP}:8123", None, [BY_IP_HTTPS]),
        (f"https://{HA_IP}:8123", f"https://{HA_NAME}:8123", [BY_IP_HTTPS, BY_NAME_HTTPS]),
        # a Local network URL typed with http: nothing listens there
        (f"http://{HA_IP}:8123", None, [BY_IP_HTTPS]),
    ],
)
@pytest.mark.parametrize("device_tls", [None, True])
def test_server_with_ssl_hands_out_https(
    hass: HomeAssistant,
    internal: str | None,
    external: str | None,
    expected: list[str],
    device_tls: bool | None,
) -> None:
    """A server with SSL on answers HTTPS only: every URL is https.

    A PRO and a device that has not reported itself yet get the same.
    """
    _server(hass, ssl=True, internal=internal, external=external)

    assert resolve_device_webhook_urls(hass, "wid", device_tls=device_tls) == expected
    assert resolve_webhook_url(hass, "wid") == expected[0]


def test_server_with_ssl_refuses_a_device_without_tls(hass: HomeAssistant) -> None:
    """A device known to have no TLS cannot be served by an HTTPS-only server."""
    _server(hass, ssl=True, external=f"https://{HA_NAME}:8123")

    with pytest.raises(NoPlainHttpURLError):
        resolve_device_webhook_urls(hass, "wid", device_tls=False)
    # the callers that only know "no URL" keep working
    assert issubclass(NoPlainHttpURLError, NoURLAvailableError)


def test_server_with_ssl_never_returns_the_stored_http_url(hass: HomeAssistant) -> None:
    """The URL stored while SSL was off must not come back once it is on."""
    _server(hass, ssl=True, external=f"https://{HA_NAME}:8123")

    urls = resolve_device_webhook_urls(
        hass, "wid", device_tls=True, fallback_url=BY_IP_HTTP,
    )

    assert urls == [BY_IP_HTTPS, BY_NAME_HTTPS]


def test_server_with_ssl_without_an_address_uses_the_stored_https_url(
    hass: HomeAssistant,
) -> None:
    """No network address yet (a start before the network is up)."""
    _server(hass, ssl=True, local_ip="127.0.0.1")

    assert resolve_local_webhook_url(
        hass, "wid", fallback_url=BY_IP_HTTPS,
    ) == BY_IP_HTTPS
    # a stored http URL is still not an answer
    with pytest.raises(NoURLAvailableError):
        resolve_local_webhook_url(hass, "wid", fallback_url=BY_IP_HTTP)


def test_server_with_ssl_without_an_address_falls_back_to_the_internet_url(
    hass: HomeAssistant,
) -> None:
    """Only the Internet URL is left when the server has no local address."""
    _server(hass, ssl=True, external=f"https://{HA_NAME}:8123", local_ip="127.0.0.1")

    assert resolve_device_webhook_urls(hass, "wid") == [BY_NAME_HTTPS]


def test_plain_server_is_unchanged(hass: HomeAssistant) -> None:
    """A plain-http server: local http, the Internet https URL for a PRO only."""
    _server(hass, ssl=False, external=f"https://{HA_NAME}:8123")

    assert resolve_device_webhook_urls(hass, "wid") == [BY_IP_HTTP]
    assert resolve_device_webhook_urls(hass, "wid", device_tls=False) == [BY_IP_HTTP]
    assert resolve_device_webhook_urls(hass, "wid", device_tls=True) == [
        BY_IP_HTTP,
        BY_NAME_HTTPS,
    ]


def test_plain_server_behind_an_https_local_url(hass: HomeAssistant) -> None:
    """An https Local network URL (a reverse proxy) on a plain-http server.

    The server still answers plain http on its own address, which is the
    one address every device can use.
    """
    _server(hass, ssl=False, internal=f"https://{HA_NAME}")

    assert resolve_device_webhook_urls(hass, "wid") == [BY_IP_HTTP]
    assert resolve_device_webhook_urls(hass, "wid", device_tls=True) == [
        BY_IP_HTTP,
        f"https://{HA_NAME}/api/webhook/wid",
    ]


@pytest.mark.parametrize("local_ip", ["127.0.0.1", "not-an-address"])
def test_server_base_url_needs_a_real_address(hass: HomeAssistant, local_ip: str) -> None:
    """A loopback or unusable server address is not offered to a device."""
    _server(hass, ssl=False, local_ip=local_ip)
    assert _server_base_url(hass) is None

    hass.config.api = None
    assert _server_base_url(hass) is None
    assert server_uses_ssl(hass) is False
