"""Config flow for the MeatPi integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant import config_entries
from homeassistant.components import onboarding
from homeassistant.const import CONF_HOST, CONF_WEBHOOK_ID
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError
import voluptuous as vol
from yarl import URL

from .const import (
    CONF_DEVICE_TYPE,
    CONF_HISTORY_PREFER_LIVE,
    CONF_HISTORY_STATES,
    CONF_HISTORY_STATES_STEP,
    CONF_HISTORY_SYNC,
    CONF_KEEP_LAST_VALUES,
    CONF_POST_INTERVAL,
    DEFAULT_HISTORY_PREFER_LIVE,
    DEFAULT_HISTORY_STATES,
    DEFAULT_HISTORY_STATES_STEP,
    DEFAULT_HISTORY_SYNC,
    DEFAULT_KEEP_LAST_VALUES,
    DEFAULT_POST_INTERVAL,
    DOMAIN,
    MAX_HISTORY_STATES_STEP,
    MAX_POST_INTERVAL,
    MIN_HISTORY_STATES_STEP,
    MIN_POST_INTERVAL,
)
from .devices import DEVICE_TYPE_GENERIC, is_known_device_type
from .helpers import resolve_webhook_url
from .removed_webhooks import async_get_removed_webhooks

if TYPE_CHECKING:
    from ipaddress import IPv4Address, IPv6Address

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)


def _address_tokens(*values: Any) -> set[str]:
    """Reduce address strings (URLs, hostnames, IPs) to comparable hosts.

    "http://wican_1.local:80", "wican_1.local." and "WICAN_1.LOCAL" all
    reduce to "wican_1.local", so the same physical device is recognized
    across the manual flow (URL-keyed) and zeroconf (MAC-keyed).
    """
    tokens: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip().lower().rstrip(".")
        if not candidate:
            continue
        try:
            url = URL(candidate if "://" in candidate else f"http://{candidate}")
            host = url.host
        except ValueError:
            host = None
        tokens.add(host.rstrip(".") if host else candidate)
    return tokens


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MeatPi device discovery via Zeroconf."""

    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.discovered_mdns: str | None = None
        self.discovered_host: str | None = None
        self.discovered_name: str | None = None
        self.discovered_unique_id: str | None = None
        self.discovered_device_type: str | None = None

    def _entry_matching_address(
        self, tokens: set[str],
    ) -> config_entries.ConfigEntry | None:
        """Find an existing entry that reaches one of these addresses.

        Manual entries are keyed by connection URL while zeroconf entries
        are keyed by MAC, so unique_id comparison alone cannot see that
        both describe the same physical device (M4).
        """
        if not tokens:
            return None
        for entry in self._async_current_entries(include_ignore=False):
            known = _address_tokens(
                entry.data.get("mdns"),
                entry.data.get("host"),
                entry.data.get("ip"),
                entry.unique_id,
            )
            if tokens & known:
                return entry
        return None

    def _adopt_matching_entry(
        self,
        tokens: set[str],
        unique_id: str,
        connection_updates: dict[str, str],
    ) -> bool:
        """Upgrade an address-matching entry to this discovery's unique_id.

        Returns True when an existing entry adopted the discovery (the
        flow should abort as already configured).
        """
        matching = self._entry_matching_address(tokens)
        if matching is None:
            return False
        self.hass.config_entries.async_update_entry(
            matching,
            unique_id=unique_id,
            data={**matching.data, **connection_updates},
        )
        self.hass.config_entries.async_schedule_reload(matching.entry_id)
        return True

    def _refresh_configured_entry(self, connection_updates: dict[str, str]) -> None:
        """Refresh an already configured entry's address from a discovery.

        ``_abort_if_unique_id_configured(updates=...)`` would also do this,
        but its built-in reload is not allowed for entries that keep an
        update listener (ours re-registers the webhook when options
        change), so the reload is scheduled here.
        """
        if self.unique_id is None or not connection_updates:
            return
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            self.handler, self.unique_id,
        )
        if entry is None:
            return
        if (
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **connection_updates},
            )
            and entry.state is config_entries.ConfigEntryState.LOADED
        ):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual setup initiated by the user."""
        if user_input is not None:
            mdns = user_input.get("mdns")
            host = user_input.get(CONF_HOST)
            title = host or mdns or "MeatPi device"

            # Prevent duplicate manual setups of the same device. A manually
            # entered device has no MAC/device_id yet, so key off the normalized
            # connection address the user provided.
            manual_unique_id = _format_http_url(mdns or host, None) or (mdns or host)
            if manual_unique_id:
                await self.async_set_unique_id(manual_unique_id)
                self._abort_if_unique_id_configured()

            # The same device may already exist under a MAC-keyed unique_id
            # (zeroconf discovery): match by connection address so a manual
            # re-add cannot create a duplicate entry with its own webhook.
            if self._entry_matching_address(_address_tokens(mdns, host)):
                return self.async_abort(reason="already_configured")

            webhook_id = uuid4().hex
            try:
                webhook_url = resolve_webhook_url(
                    self.hass,
                    webhook_id,
                    require_current_request=True,
                )
            except NoURLAvailableError:
                # This instance has no address at all a device could push
                # to (no network address yet and no URL configured). A
                # web server with SSL on does not end here: the resolver
                # hands out its https address. A friendly abort beats
                # "Unknown error occurred".
                return self.async_abort(reason="no_url_available")
            entry_data = {
                CONF_WEBHOOK_ID: webhook_id,
                "webhook_url": webhook_url,
            }
            if mdns:
                entry_data["mdns"] = _format_http_url(mdns, None) or mdns
            if host:
                entry_data["host"] = _format_http_url(host, None) or host

            return self.async_create_entry(
                title=title,
                data=entry_data,
                description_placeholders={
                    "webhook_url": webhook_url,
                    "docs_url": "https://github.com/meatpiHQ/wican-fw",
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required("mdns"): str,
                vol.Optional(CONF_HOST): str,
            },
        )
        return self.async_show_form(step_id="user", data_schema=data_schema)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        name = discovery_info.name or ""
        properties = discovery_info.properties or {}
        host = discovery_info.host
        host_ip = _string_ip(discovery_info.ip_address)
        # Zeroconf gives the host name as a fully qualified one, with a
        # trailing dot ("wican_<id>.local."); the device reports its own
        # address without it. Stored with the dot, every push changed the
        # address back and every start of Home Assistant changed it again:
        # a reload and two more registrations per start, and a dot at the
        # end of the entry's title.
        hostname = (getattr(discovery_info, "hostname", "") or "").rstrip(".")
        port = discovery_info.port

        if not host and not host_ip:
            return self.async_abort(reason="no_host")

        # Contract v2: only the dedicated MeatPi service types are
        # accepted (_wican._tcp / _meatpi._tcp). The legacy _http._tcp
        # instance-name/hostname matching is gone — firmware below the
        # supported minimum must be updated (a manual add still works and
        # surfaces the firmware-update repair issue).
        service_type = (getattr(discovery_info, "type", "") or "").lower()
        if not service_type.startswith(("_wican.", "_meatpi.")):
            _LOGGER.debug(
                "Ignoring zeroconf service not matching a MeatPi device: name=%s hostname=%s type=%s",
                name, hostname, service_type,
            )
            return self.async_abort(reason="not_wican")

        # Extract MAC address and device_id from TXT records (from firmware)
        mac_address = properties.get("mac", b"").decode("utf-8") if isinstance(properties.get("mac"), bytes) else properties.get("mac", "")
        device_id = properties.get("device_id", b"").decode("utf-8") if isinstance(properties.get("device_id"), bytes) else properties.get("device_id", "")

        # Device type from TXT records (newer firmware). Built-in and
        # catalog-defined slugs are accepted; an unknown slug on a MeatPi
        # service maps to the generic profile; legacy advertisements leave
        # it unset and setup infers it from the reported hw_version.
        raw_device_type = properties.get("device_type", "")
        if isinstance(raw_device_type, bytes):
            raw_device_type = raw_device_type.decode("utf-8", errors="replace")
        if is_known_device_type(raw_device_type):
            device_type: str | None = raw_device_type
        elif service_type.startswith("_meatpi."):
            device_type = DEVICE_TYPE_GENERIC
        else:
            device_type = None

        # Use MAC address as unique_id (most stable), fallback to device_id, then hostname
        if mac_address:
            # Normalize MAC address format (remove colons for unique_id)
            unique_id = mac_address.replace(":", "").lower()
            _LOGGER.debug("Using MAC address as unique_id: %s", unique_id)
        elif device_id:
            unique_id = device_id
            _LOGGER.debug("Using device_id as unique_id: %s", unique_id)
        else:
            # Fallback for older firmware without MAC/device_id
            base_id = hostname if hostname else name
            unique_id = f"{base_id}-{host}:{port}"
            _LOGGER.debug("Using hostname-based unique_id: %s", unique_id)

        # Store mdns/host for setup to attempt webhook registration later
        mdns_target = hostname or host
        mdns_url = _format_http_url(mdns_target, port)
        host_url = _format_http_url(host_ip or host, port)

        await self.async_set_unique_id(unique_id)
        # If already configured, refresh the stored connection info from this
        # discovery so IP/hostname changes propagate (discovery-update-info).
        connection_updates = {
            k: v
            for k, v in {"mdns": mdns_url, CONF_HOST: host_url}.items()
            if v
        }
        self._refresh_configured_entry(connection_updates)
        self._abort_if_unique_id_configured()

        # The device may already exist as a manual entry keyed by its
        # connection URL rather than this MAC: adopt the stable MAC-based
        # unique_id on that entry instead of creating a duplicate.
        if self._adopt_matching_entry(
            _address_tokens(hostname, host, host_ip), unique_id, connection_updates,
        ):
            return self.async_abort(reason="already_configured")

        _LOGGER.info("MeatPi device discovered via Zeroconf: name=%s hostname=%s url=%s", name, hostname, mdns_url)

        # Store discovery info for confirmation step
        self.discovered_mdns = mdns_url
        self.discovered_host = host_url
        self.discovered_name = hostname or name
        self.discovered_unique_id = unique_id
        self.discovered_mac = mac_address
        self.discovered_device_id = device_id
        self.discovered_device_type = device_type
        # A device discovered while it still posts to an entry removed here
        # is answered 410 Gone. With no entry left nothing else loads the
        # removed webhooks after a restart of Home Assistant.
        await async_get_removed_webhooks(self.hass)

        # The card on the integrations page: without a name every discovered
        # device stood there under the integration's name alone, and two
        # devices could only be told apart by opening each.
        self.context["title_placeholders"] = {
            "name": self.discovered_name or "MeatPi device",
        }

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle user confirmation of discovered WiCAN device."""
        # Auto-add during onboarding for seamless setup
        if user_input is not None or not onboarding.async_is_onboarded(self.hass):
            webhook_id = uuid4().hex
            try:
                webhook_url = resolve_webhook_url(
                    self.hass,
                    webhook_id,
                    require_current_request=True,
                )
            except NoURLAvailableError:
                return self.async_abort(reason="no_url_available")

            return self.async_create_entry(
                title=self.discovered_name or "MeatPi device",
                data={
                    k: v
                    for k, v in {
                        "mdns": self.discovered_mdns,
                        CONF_HOST: self.discovered_host,
                        CONF_WEBHOOK_ID: webhook_id,
                        "webhook_url": webhook_url,
                        "mac": self.discovered_mac,
                        "device_id": self.discovered_device_id,
                        CONF_DEVICE_TYPE: self.discovered_device_type,
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
                "name": self.discovered_name or "MeatPi device",
                "url": self.discovered_mdns or self.discovered_host or "Unknown",
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle reconfiguration of an existing entry (host/mDNS)."""
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            data = dict(reconfigure_entry.data)
            mdns = user_input.get("mdns")
            host = user_input.get(CONF_HOST)
            if mdns:
                data["mdns"] = _format_http_url(mdns, None) or mdns
            if host:
                data["host"] = _format_http_url(host, None) or host
            # Scheduled explicitly: async_update_reload_and_abort is not
            # allowed for entries that keep an update listener (ours
            # re-registers the webhook when options change).
            self.hass.config_entries.async_update_entry(
                reconfigure_entry, data=data,
            )
            self.hass.config_entries.async_schedule_reload(
                reconfigure_entry.entry_id,
            )
            return self.async_abort(reason="reconfigure_successful")

        current = reconfigure_entry.data
        data_schema = vol.Schema(
            {
                vol.Required(
                    "mdns", default=current.get("mdns", ""),
                ): str,
                vol.Optional(
                    CONF_HOST, default=current.get("host", ""),
                ): str,
            },
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> MeatPiOptionsFlow:
        return MeatPiOptionsFlow(config_entry)


class MeatPiOptionsFlow(config_entries.OptionsFlow):
    """Options flow to configure WiCAN settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._initial_options = dict(config_entry.options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
                vol.Required(
                    CONF_HISTORY_SYNC,
                    default=options.get(CONF_HISTORY_SYNC, DEFAULT_HISTORY_SYNC),
                ): bool,
                vol.Required(
                    CONF_HISTORY_PREFER_LIVE,
                    default=options.get(
                        CONF_HISTORY_PREFER_LIVE, DEFAULT_HISTORY_PREFER_LIVE,
                    ),
                ): bool,
                vol.Required(
                    CONF_HISTORY_STATES,
                    default=options.get(CONF_HISTORY_STATES, DEFAULT_HISTORY_STATES),
                ): bool,
                vol.Required(
                    CONF_HISTORY_STATES_STEP,
                    default=options.get(
                        CONF_HISTORY_STATES_STEP, DEFAULT_HISTORY_STATES_STEP,
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_HISTORY_STATES_STEP, max=MAX_HISTORY_STATES_STEP,
                    ),
                ),
                vol.Required(
                    CONF_KEEP_LAST_VALUES,
                    default=options.get(
                        CONF_KEEP_LAST_VALUES, DEFAULT_KEEP_LAST_VALUES,
                    ),
                ): bool,
            },
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)


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
