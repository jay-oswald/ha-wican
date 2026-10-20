"""Config flow for the WiCAN integration."""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiohttp
from homeassistant import config_entries
from homeassistant.components import onboarding
from homeassistant.const import CONF_HOST, CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol
from yarl import URL

from .const import (
    CONF_POST_INTERVAL,
    DEFAULT_POST_INTERVAL,
    DOMAIN,
    MAX_POST_INTERVAL,
    MIN_POST_INTERVAL,
)
from .helpers import resolve_webhook_url

if TYPE_CHECKING:
    from ipaddress import IPv4Address, IPv6Address

    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)

# Where every WiCAN firmware (the original and the Pro) reports who it is.
STATUS_PATH = "/check_status"

# The device is on the local network: a status request answers at once or not at all.
STATUS_TIMEOUT = aiohttp.ClientTimeout(total=5)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WiCAN discovery via Zeroconf."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.discovered_mdns: str | None = None
        self.discovered_host: str | None = None
        self.discovered_name: str | None = None
        self.discovered_unique_id: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle manual setup initiated by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            mdns = user_input.get("mdns")
            host = user_input.get(CONF_HOST)
            connection = _connection_data(mdns, host)

            # Ask the device who it is before creating the entry: its device_id is
            # the entry's identity, the same one discovery and the webhook use, so
            # one device never ends up with two entries.
            device_id = await _fetch_device_id(self.hass, connection.get(CONF_HOST), connection.get("mdns"))
            if device_id is None:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                webhook_id = uuid4().hex
                webhook_url = resolve_webhook_url(
                    self.hass,
                    webhook_id,
                    require_current_request=True,
                )
                return self.async_create_entry(
                    title=host or mdns or "WiCAN",
                    data={
                        CONF_WEBHOOK_ID: webhook_id,
                        "webhook_url": webhook_url,
                        "device_id": device_id,
                        **connection,
                    },
                    description_placeholders={
                        "webhook_url": webhook_url,
                        "docs_url": "https://github.com/meatpiHQ/wican-fw",
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_CONNECTION_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Change where Home Assistant reaches an existing device.

        The device must answer at the new address as the same device: an
        address that answers with another device_id is refused, so a
        reconfigure never moves an entry, its entities and their history onto
        another device. Reloading the entry registers the webhook again.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            connection = _connection_data(user_input.get("mdns"), user_input.get(CONF_HOST))
            device_id = await _fetch_device_id(self.hass, connection.get(CONF_HOST), connection.get("mdns"))
            if device_id is None:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device_id)
                if entry.unique_id is not None:
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                data = {key: value for key, value in entry.data.items() if key not in {"mdns", CONF_HOST}}
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=device_id,
                    data={**data, **connection, "device_id": device_id},
                )

        current = user_input or {
            "mdns": _strip_http(entry.data.get("mdns")),
            CONF_HOST: _strip_http(entry.data.get(CONF_HOST)),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_CONNECTION_SCHEMA, current),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> FlowResult:
        """Handle zeroconf discovery."""
        name = discovery_info.name or ""
        properties = discovery_info.properties or {}
        host = discovery_info.host
        host_ip = _string_ip(discovery_info.ip_address)
        hostname = getattr(discovery_info, "hostname", "") or ""
        port = discovery_info.port

        if not host and not host_ip:
            return self.async_abort(reason="no_host")

        # Accept WiCAN based on provided mDNS: instance name or hostname
        is_wican_instance = name == "WiCAN-WebServer"
        is_wican_host = hostname.lower().startswith("wican_")
        if not (is_wican_instance or is_wican_host):
            _LOGGER.debug("Ignoring zeroconf service not matching WiCAN: name=%s hostname=%s", name, hostname)
            return self.async_abort(reason="not_wican")

        # Extract MAC address and device_id from TXT records (from firmware)
        mac_address = properties.get("mac", b"").decode("utf-8") if isinstance(properties.get("mac"), bytes) else properties.get("mac", "")
        device_id = properties.get("device_id", b"").decode("utf-8") if isinstance(properties.get("device_id"), bytes) else properties.get("device_id", "")

        # The device_id is the entry's identity: manual setup reads it from the
        # device and the webhook carries it, so discovery keys on it too. The MAC
        # in the TXT record is the station address, which neither of the others
        # can see; it is used only for firmware that does not publish a device_id.
        if device_id:
            unique_id = device_id
            _LOGGER.debug("Using device_id as unique_id: %s", unique_id)
            if mac_address:
                self._rekey_entry_on_mac(mac_address.replace(":", "").lower(), device_id)
        elif mac_address:
            unique_id = mac_address.replace(":", "").lower()
            _LOGGER.debug("Using MAC address as unique_id: %s", unique_id)
        else:
            # Fallback for older firmware without MAC/device_id
            base_id = hostname if hostname else name
            unique_id = f"{base_id}-{host}:{port}"
            _LOGGER.debug("Using hostname-based unique_id: %s", unique_id)

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        # Store mdns/host for setup to attempt webhook registration later
        mdns_target = hostname or host
        mdns_url = _format_http_url(mdns_target, port)
        host_url = _format_http_url(host_ip or host, port)

        _LOGGER.info("WiCAN discovered via Zeroconf: name=%s hostname=%s url=%s", name, hostname, mdns_url)

        # Store discovery info for confirmation step
        self.discovered_mdns = mdns_url
        self.discovered_host = host_url
        self.discovered_name = hostname or name
        self.discovered_unique_id = unique_id
        self.discovered_mac = mac_address
        self.discovered_device_id = device_id

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle user confirmation of discovered WiCAN device."""
        # Auto-add during onboarding for seamless setup
        if user_input is not None or not onboarding.async_is_onboarded(self.hass):
            webhook_id = uuid4().hex
            webhook_url = resolve_webhook_url(
                self.hass,
                webhook_id,
                require_current_request=True,
            )

            return self.async_create_entry(
                title=self.discovered_name or "WiCAN",
                data={
                    k: v
                    for k, v in {
                        "mdns": self.discovered_mdns,
                        CONF_HOST: self.discovered_host,
                        CONF_WEBHOOK_ID: webhook_id,
                        "webhook_url": webhook_url,
                        "mac": self.discovered_mac,
                        "device_id": self.discovered_device_id,
                    }.items()
                    if v
                },
                description=None,
                description_placeholders={
                    "webhook_url": webhook_url,
                    "docs_url": "https://github.com/meatpiHQ/wican-fw",
                },
            )

        # Show confirmation dialog for established systems
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": self.discovered_name or "WiCAN",
                "url": self.discovered_mdns or self.discovered_host or "Unknown",
            },
        )

    def _rekey_entry_on_mac(self, mac_id: str, device_id: str) -> None:
        """Move an entry discovery keyed on the station MAC onto the device_id.

        Older discovery keyed entries on the MAC; the device_id is the identity
        every path shares now. Without this, the device would be offered again
        as a new one.
        """
        for entry in self._async_current_entries(include_ignore=False):
            if entry.unique_id == mac_id:
                self.hass.config_entries.async_update_entry(entry, unique_id=device_id)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WiCANOptionsFlow(config_entry)


class WiCANOptionsFlow(config_entries.OptionsFlow):
    """Options flow to configure WiCAN settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._initial_options = dict(config_entry.options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = dict(self.config_entry.options) if self.hass is not None else self._initial_options
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_POST_INTERVAL,
                    default=options.get(CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POST_INTERVAL, max=MAX_POST_INTERVAL),
                ),
            },
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)


_CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required("mdns"): str,
        vol.Optional(CONF_HOST): str,
    },
)


def _connection_data(mdns: str | None, host: str | None) -> dict[str, str]:
    """The entry's connection fields, as URLs, from what the user typed."""
    connection: dict[str, str] = {}
    if mdns:
        connection["mdns"] = _format_http_url(mdns, None) or mdns
    if host:
        connection[CONF_HOST] = _format_http_url(host, None) or host
    return connection


def _strip_http(url: str | None) -> str | None:
    """The address a user typed, from the URL the entry stores."""
    if not url:
        return None
    return url.removeprefix("http://").removeprefix("https://").rstrip("/")


async def _fetch_device_id(hass: HomeAssistant, *urls: str | None) -> str | None:
    """The device_id a WiCAN reports on its status endpoint.

    Args:
        hass: The Home Assistant instance.
        urls: Base URLs to try, in order, until one answers.

    Returns:
        The reported device_id, or None if no URL answered with one.
    """
    session = async_get_clientsession(hass)
    for url in urls:
        if not url:
            continue
        status_url = f"{url.rstrip('/')}{STATUS_PATH}"
        try:
            async with session.get(status_url, timeout=STATUS_TIMEOUT) as response:
                if response.status != HTTPStatus.OK:
                    _LOGGER.debug("Status request to %s returned HTTP %s", status_url, response.status)
                    continue
                status = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("Could not read device_id from %s: %s", status_url, err)
            continue
        device_id = status.get("device_id") if isinstance(status, dict) else None
        if device_id:
            return str(device_id)
        _LOGGER.debug("No device_id in the status reported by %s", status_url)
    return None


def _format_http_url(address: str | None, port: int | None) -> str | None:
    """Normalize an address into an http URL."""
    if not address:
        return None

    address = address.strip()
    if not address:
        return None

    if address.startswith(("http://", "https://")):
        return address

    try:
        return str(
            URL.build(
                scheme="http",
                host=address.strip("[]"),
                port=port,
            ),
        )
    except ValueError:
        return None


def _string_ip(ip: IPv4Address | IPv6Address | None) -> str | None:
    """Return the string form of an ipaddress object."""
    if ip is None:
        return None
    return str(ip)
