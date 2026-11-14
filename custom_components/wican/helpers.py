"""Helper functions and decorators for WiCAN integration."""

from __future__ import annotations

from contextlib import suppress
from ipaddress import ip_address
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import webhook
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util.network import is_loopback
from yarl import URL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Chunk size for capped body reads; small enough that the cap overshoots
# by at most one chunk, large enough not to slow normal bodies down.
_READ_CHUNK_BYTES = 64 * 1024


async def async_read_capped(response: Any, max_bytes: int) -> bytes | None:
    """Read an HTTP response body, refusing anything past ``max_bytes``.

    A declared Content-Length above the cap is refused before any read;
    the body is then streamed chunk by chunk so a chunked response (no
    length header) cannot balloon memory before a post-read length check —
    the read stops at the first chunk past the cap. Returns None when the
    body is (or would grow) too large.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        response.close()
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            # The rest of the body is deliberately unread: drop the
            # connection rather than leave it half-consumed in the pool.
            response.close()
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def build_webhook_url(base_url: str, webhook_id: str) -> str:
    """Build an absolute webhook URL from a base URL and webhook id."""
    return str(URL(base_url) / webhook.async_generate_path(webhook_id).lstrip("/"))


def _ensure_allowed_webhook_scheme(url: str, *, allowed_schemes: set[str]) -> str:
    """Validate webhook URL scheme against an allowed set."""
    try:
        parsed = URL(url)
    except ValueError as err:
        raise NoURLAvailableError from err

    if parsed.scheme not in allowed_schemes:
        raise NoURLAvailableError

    return str(parsed)


class NoPlainHttpURLError(NoURLAvailableError):
    """Home Assistant answers over HTTPS only and the device has no TLS."""


def server_uses_ssl(hass: HomeAssistant) -> bool:
    """Return True when Home Assistant's own web server has SSL on.

    With ``ssl_certificate`` under ``http:`` the server answers HTTPS only
    on its port: this instance then has no plain-http address at all.
    """
    api = hass.config.api
    return api is not None and api.use_ssl


def _server_base_url(hass: HomeAssistant) -> str | None:
    """Return the web server's own address by IP, in the scheme it serves.

    Home Assistant's ``get_url`` hands this address out for a plain-http
    server only: with SSL on, the certificate does not name the IP. A
    MeatPi device takes it all the same: its firmware verifies the
    certificate chain and skips the name check for an https address by IP.
    """
    api = hass.config.api
    if api is None:
        return None
    try:
        if is_loopback(ip_address(api.local_ip)):
            return None
    except ValueError:
        return None
    return str(
        URL.build(
            scheme="https" if api.use_ssl else "http",
            host=api.local_ip,
            port=api.port,
        ),
    )


def _local_base_urls(
    hass: HomeAssistant,
    *,
    require_current_request: bool,
) -> Iterator[str]:
    """Yield this instance's base URLs for a device at home, best first."""
    # What Home Assistant itself hands out: the Local network URL when one
    # is set, else its address by IP (a plain-http server only). With SSL
    # on, the Internet URL stays out of this slot: it is the failover URL.
    with suppress(NoURLAvailableError):
        yield get_url(
            hass,
            allow_cloud=False,
            allow_external=not server_uses_ssl(hass),
            allow_ip=True,
            prefer_external=False,
        )
    # The web server's own address: the one left when the Local network
    # URL is not set (SSL on) or is not in the scheme the server serves.
    if (own_address := _server_base_url(hass)) is not None:
        yield own_address
    if require_current_request:
        # A config flow: the address of the browser tab it runs in.
        with suppress(NoURLAvailableError):
            yield get_url(
                hass,
                require_current_request=True,
                allow_cloud=False,
                allow_ip=True,
                prefer_external=False,
            )


def resolve_local_webhook_url(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    fallback_url: str | None = None,
    require_current_request: bool = False,
) -> str:
    """Resolve the webhook URL a device on the local network pushes to.

    The URL has the scheme the web server serves. A plain-http server
    gives an http URL. A server with SSL on answers HTTPS only, so it
    gives an https URL, and an http one (a Local network URL typed with
    http, an address stored before SSL was turned on) is refused: nothing
    listens there. ``fallback_url`` is the URL stored at the last
    registration, for a start without a usable network address.
    """
    allowed_schemes = {"https" if server_uses_ssl(hass) else "http"}
    for base_url in _local_base_urls(
        hass, require_current_request=require_current_request,
    ):
        with suppress(NoURLAvailableError):
            return _ensure_allowed_webhook_scheme(
                build_webhook_url(base_url, webhook_id),
                allowed_schemes=allowed_schemes,
            )

    if fallback_url:
        with suppress(NoURLAvailableError):
            return _ensure_allowed_webhook_scheme(
                fallback_url,
                allowed_schemes=allowed_schemes,
            )

    raise NoURLAvailableError


def resolve_external_webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Resolve the preferred external webhook URL for cloud or remote access."""
    external_base = get_url(
        hass,
        allow_ip=False,
        prefer_external=True,
    )
    return _ensure_allowed_webhook_scheme(
        build_webhook_url(external_base, webhook_id),
        allowed_schemes={"http", "https"},
    )


def resolve_external_https_webhook_url(hass: HomeAssistant, webhook_id: str) -> str:
    """Resolve the preferred external HTTPS webhook URL for remote access."""
    external_base = get_url(
        hass,
        allow_ip=False,
        prefer_external=True,
    )
    return _ensure_allowed_webhook_scheme(
        build_webhook_url(external_base, webhook_id),
        allowed_schemes={"https"},
    )


def resolve_device_webhook_urls(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    device_tls: bool | None = None,
    fallback_url: str | None = None,
    require_current_request: bool = False,
) -> list[str]:
    """Resolve the ordered webhook URLs to register on a device.

    ``device_tls`` says whether the device can push over HTTPS: True for a
    WiCAN PRO, False for a device known to have no TLS stack (WiCAN OBD,
    WiCAN USB), None while the device has not reported what it is.

    A plain-http server: the local http URL, and for a TLS device the
    Internet https URL behind it as the failover (alone when there is no
    local http URL).

    A server with SSL on answers HTTPS only: the local https URL, then the
    Internet https URL. A device that is not known yet is offered them
    too, since no other address exists. A device known to have no TLS
    cannot be served at all: NoPlainHttpURLError.
    """
    uses_ssl = server_uses_ssl(hass)
    if uses_ssl and device_tls is False:
        raise NoPlainHttpURLError

    offer_https = uses_ssl or device_tls is True

    try:
        local_url = resolve_local_webhook_url(
            hass,
            webhook_id,
            fallback_url=fallback_url,
            require_current_request=require_current_request,
        )
    except NoURLAvailableError:
        if not offer_https:
            raise
        # Only the Internet URL is left (NoURLAvailableError without one).
        return [resolve_external_https_webhook_url(hass, webhook_id)]

    urls = [local_url]
    if offer_https:
        with suppress(NoURLAvailableError):
            external_url = resolve_external_https_webhook_url(hass, webhook_id)
            if external_url != local_url:
                urls.append(external_url)
    return urls


def resolve_webhook_url(
    hass: HomeAssistant,
    webhook_id: str,
    *,
    fallback_url: str | None = None,
    require_current_request: bool = False,
) -> str:
    """Resolve the webhook URL shown and stored when an entry is created.

    The device is not known at that point, so this is the first URL a
    device of unknown type would be given; the registration at setup
    resolves again for the device it then talks to.
    """
    return resolve_device_webhook_urls(
        hass,
        webhook_id,
        fallback_url=fallback_url,
        require_current_request=require_current_request,
    )[0]
