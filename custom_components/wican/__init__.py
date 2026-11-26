"""MeatPi integration (WiCAN and other MeatPi devices)."""

from __future__ import annotations

import asyncio
import hashlib
from http import HTTPStatus
import ipaddress
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from aiohttp import ClientError, ClientResponseError
from aiohttp.web import Request, Response
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_WEBHOOK_ID, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.components.frontend import add_extra_js_url
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.loader import async_get_integration
from pathlib import Path
from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads
import voluptuous as vol
from yarl import URL

from .api import DeviceCapabilities, MeatPiApiClient
from .catalog import async_setup_catalog
from .const import (
    GPX_CARD_FILE,
    FRONTEND_STATIC_URL,
    DATA_FRONTEND_REGISTERED,
    ATTR_HOURS,
    COMPONENT_DATA_LOGGER,
    DEFAULT_GPX_HOURS,
    GPX_DIR,
    GPX_MAX_PAGES,
    GPX_PAGE_POINTS,
    MAX_GPX_HOURS,
    SERVICE_SAVE_GPX,
    CONF_DEVICE_TYPE,
    CONF_HELD_BY_EARLIER_RELEASE,
    CONF_HELD_WEBHOOK_REV,
    CONF_HISTORY_SYNC,
    CONF_POST_INTERVAL,
    CONF_READS_PUSH_REPLY,
    DEFAULT_HISTORY_SYNC,
    DEFAULT_POST_INTERVAL,
    DOMAIN,
    HISTORY_MAX_FOLLOWUPS,
    HISTORY_SYNC_MIN_INTERVAL,
    IP_CACHE_DURATION,
    MAX_DEVICE_INFO_FIELD_LENGTH,
    MAX_WEBHOOK_BODY_BYTES,
    MIN_FIRMWARE_BY_STREAM,
    PROBE_RETRY_INTERVAL,
    PUSH_REPLY_MAX_BYTES,
    PUSH_REPLY_REPEAT_INTERVAL,
    REGISTRATION_RETRY_INTERVAL,
    SIGNAL_CAPABILITIES_UPDATED,
    WEBHOOK_MAX_RETRIES,
    WEBHOOK_REGISTRATION_TIMEOUT,
    WEBHOOK_RELEASE_TIMEOUT,
    WEBHOOK_RETRY_DELAY_BASE,
    WEBHOOK_REV_KEY,
    WEBHOOK_REV_LENGTH,
    WEBHOOK_REV_MAX_LENGTH,
)
from .coordinator import MeatPiDataUpdateCoordinator
from .devices import (
    DEVICE_TYPE_WICAN,
    DEVICE_TYPE_WICAN_USB,
    WICAN_FAMILY_TYPES,
    get_profile,
    infer_device_type,
)
from .exceptions import MeatPiApiError, MeatPiWebhookError
from .github_releases import GitHubReleasesCoordinator, hardware_stream
from .helpers import NoPlainHttpURLError, resolve_device_webhook_urls
from .history import (
    async_kept_hours_due_in,
    async_remove_history_store,
    async_sync_history,
)
from .models import MeatPiRuntimeData
from .param_loader import async_update_params_from_github
from .removed_webhooks import async_get_removed_webhooks, async_release_webhook_at

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.UPDATE,
]

# Type alias for config entry with runtime data
MeatPiConfigEntry = ConfigEntry[MeatPiRuntimeData]

# Products without a TLS stack: they push to a plain-http address only.
_NO_TLS_DEVICE_TYPES = frozenset({DEVICE_TYPE_WICAN, DEVICE_TYPE_WICAN_USB})


class _WebhookEndpointsFailedError(MeatPiWebhookError):
    """Raised when all webhook endpoints failed for this attempt."""


class _WebhookBodyTooLargeError(MeatPiWebhookError):
    """Raised when a webhook body exceeds the decompressed size cap."""


async def _async_read_webhook_json(request: Request) -> Any:
    """Read a webhook body (plain or gzip) as JSON, bounded.

    Pushes may arrive gzip-compressed (LTE data saving); aiohttp inflates
    them transparently while only limiting the *compressed* size, so this
    accumulates at most MAX_WEBHOOK_BODY_BYTES of decompressed body —
    backpressure keeps anything past the cap un-inflated. aiohttp's payload
    layer additionally aborts suspicious streams on its own
    (RequestPayloadError), which callers treat like any undecodable body.

    Raises _WebhookBodyTooLargeError past the cap; whatever json/aiohttp
    raise for undecodable bodies otherwise.
    """
    chunks: list[bytes] = []
    body_length = 0
    while body_length <= MAX_WEBHOOK_BODY_BYTES:
        chunk = await request.content.read(
            MAX_WEBHOOK_BODY_BYTES + 1 - body_length,
        )
        if not chunk:
            break
        chunks.append(chunk)
        body_length += len(chunk)
    if body_length > MAX_WEBHOOK_BODY_BYTES:
        raise _WebhookBodyTooLargeError
    # HA's strict loader (orjson) rejects NaN/Infinity — the state machine
    # cannot represent them, so they must never enter the pipeline.
    return json_loads(b"".join(chunks))


def _parse_version(version: str | None) -> tuple[int, ...] | None:
    """Parse a version string like v4.49 into a comparable tuple."""
    # Legacy entries may hold a non-string here; treat it as unknown.
    if not version or not isinstance(version, str):
        return None

    parts = re.findall(r"\d+", version)
    if not parts:
        return None

    return tuple(int(part) for part in parts)


def _is_version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    """Return True when version is greater than or equal to minimum."""
    parsed_version = _parse_version(version)
    if parsed_version is None:
        return False

    max_len = max(len(parsed_version), len(minimum))
    padded_version = parsed_version + (0,) * (max_len - len(parsed_version))
    padded_minimum = minimum + (0,) * (max_len - len(minimum))
    return padded_version >= padded_minimum


def _supports_dual_webhook_urls(entry: MeatPiConfigEntry) -> bool:
    """Return True when the device can accept multiple webhook URLs.

    Contract v2: a PRO-stream capability, full stop. WiCAN OBD and USB
    have no TLS stack — they can NEVER take an https URL, so they always
    get exactly one plain-http local webhook URL. Firmware that ignores
    an unexpected "urls" field is unaffected by over-offering.
    """
    hw_version = str(entry.data.get("hw_version", "")).lower()
    return "pro" in hw_version


def _device_tls_support(entry: MeatPiConfigEntry) -> bool | None:
    """Return whether the device can push over HTTPS; None when not known.

    A WiCAN PRO can. WiCAN OBD and USB cannot, and that verdict needs the
    device's own word: a hardware string that names a WiCAN and is not a
    PRO. A missing or unrecognized string proves nothing, because the
    WiCAN type is also the default for an entry that has not reported
    itself yet and for a product the integration does not know.
    """
    if _supports_dual_webhook_urls(entry):
        return True
    hw_version = str(entry.data.get("hw_version") or "").lower()
    if "wican" in hw_version and infer_device_type(hw_version) in _NO_TLS_DEVICE_TYPES:
        return False
    return None


def _build_webhook_payload(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    post_interval: int,
) -> dict[str, str | bool | int | list[str]]:
    """Build the webhook registration payload for the device firmware.

    Raises NoPlainHttpURLError when Home Assistant's web server has SSL on
    and the device is known to have no TLS, NoURLAvailableError when this
    instance has no address at all.
    """
    urls = resolve_device_webhook_urls(
        hass,
        entry.runtime_data.webhook_id,
        device_tls=_device_tls_support(entry),
        fallback_url=entry.data.get("webhook_url"),
    )

    payload: dict[str, str | bool | int | list[str]] = {
        "url": urls[0],
        "enabled": True,
        "interval": post_interval,
    }

    # A second URL is resolved only for a device that may speak TLS (a
    # PRO, or any device while the server answers over HTTPS only).
    if len(urls) > 1:
        payload["urls"] = urls

    return payload


def _ensure_http_scheme(value: str | None) -> str | None:
    """Return value with http:// if no scheme is provided."""
    if not value:
        return value
    if value.startswith(("http://", "https://")):
        return value
    return f"http://{value}"


def _http_url_from_host(host: str | None, port: int | None = None) -> str | None:
    """Build an http URL from a bare host/ip string."""
    if not host:
        return None
    try:
        return str(URL.build(scheme="http", host=host, port=port))
    except ValueError:
        return None


def _build_webhook_endpoint(base: str | None) -> URL | None:
    """Return the device webhook endpoint URL constructed from a base URL."""
    if not base:
        return None

    candidate = base.strip()
    try:
        url = URL(candidate)
    except ValueError:
        return None

    if not url.scheme:
        try:
            schemed_candidate = _ensure_http_scheme(candidate)
            if schemed_candidate is None:
                return None
            url = URL(schemed_candidate)
        except ValueError:
            return None

    # Drop any path/query/fragment parts before appending the webhook path
    url = url.with_path("").with_query(None).with_fragment(None)
    return url / "api" / "webhook"


# Device-reported fields that are persisted into the config entry. Order in
# the payload: nested "status" wins over a top-level key, matching identity
# validation precedence.
_DEVICE_INFO_KEYS = (
    "fw_version",
    "hw_version",
    "device_id",
    "git_version",
    "mdns",
    "host",
    "ip",
)


def _sanitize_device_field(value: object) -> str | None:
    """Return a safe string form of a device-reported info field, or None.

    These values end up in the config entry (written to disk) and later flow
    into version parsing, URL building, and the device registry — all of which
    assume strings. A glitching or hostile device must not be able to persist
    anything else.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_DEVICE_INFO_FIELD_LENGTH:
        return None
    return value


def _normalize_ip(ip: str | None) -> str | None:
    """Normalize IPv4-mapped IPv6 strings into IPv4 when possible."""
    if not ip:
        return None
    if ip.startswith("::ffff:") and ip.count(":") >= 2:
        return ip.split("::ffff:", maxsplit=1)[-1]
    return ip


def _extract_request_ip(request: Request) -> str | None:
    """Best-effort extraction of the originating peer IP address.

    Deliberately relies on ``request.remote``: Home Assistant's HTTP
    middleware already resolves X-Forwarded-For, and only for proxies the
    user configured as trusted. Parsing the header here would let any
    client that learns the webhook path redirect webhook registration and
    control-API traffic by spoofing it.
    """
    if request.remote:
        return _normalize_ip(request.remote)

    transport = request.transport
    if transport is not None:
        peername = transport.get_extra_info("peername")
        if isinstance(peername, (tuple, list)) and peername:
            return _normalize_ip(peername[0])

    return None


def _collect_device_reported_fields(data: dict[str, Any]) -> dict[str, str]:
    """Extract sanitized device info fields from a webhook payload.

    Fields come from the nested "status" object or the top level; only
    bounded, non-empty strings are kept (a glitching device may send nested
    objects or numbers here) so nothing unusable ever reaches the config
    entry, version parsing, or URL building.
    """
    fields: dict[str, str] = {}
    status = data.get("status")
    if not isinstance(status, dict):
        status = {}
    for key in _DEVICE_INFO_KEYS:
        if key in status:
            raw_field = status[key]
        elif key in data:
            raw_field = data[key]
        else:
            continue
        field_value = _sanitize_device_field(raw_field)
        if field_value is None:
            if raw_field not in (None, ""):
                _LOGGER.debug(
                    "Ignoring unusable device-reported %s: %r", key, raw_field,
                )
            continue
        fields[key] = field_value
    return fields


# Tailscale hands out addresses from the CGNAT range.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _validated_vpn_ip(value: object) -> str | None:
    """Return a safe device VPN tunnel address, or None.

    The pushed vpn_ip tells the integration where to send control traffic,
    so it must never be able to point at an arbitrary internet host: only
    private (RFC1918/ULA) and CGNAT (Tailscale 100.64/10) addresses are
    accepted.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if address.is_loopback or address.is_multicast or address.is_link_local:
        return None
    if address.is_private or (
        address.version == 4 and address in _CGNAT_NETWORK
    ):
        return str(address)
    return None


# vpn_status values that positively indicate the tunnel is down; anything
# else (unknown wording, missing field) trusts the presence of a valid ip.
_VPN_DOWN_MARKERS = ("not", "disabled", "disconnected", "off", "down")


def _update_vpn_state(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    data: dict[str, Any],
) -> None:
    """Track the device-reported VPN tunnel address from a push payload.

    A connected VPN's address becomes the backup control endpoint (tried
    after the local address); a disconnected VPN clears it so control never
    targets a stale tunnel. Changes re-run the capability probe so the
    working endpoint rebinds.
    """
    status = data.get("status")
    if not isinstance(status, dict):
        return
    if "vpn_ip" not in status and "vpn_status" not in status:
        return  # firmware without VPN reporting: leave state untouched

    vpn_ip = _validated_vpn_ip(status.get("vpn_ip"))
    vpn_status = status.get("vpn_status")
    if isinstance(vpn_status, str) and any(
        marker in vpn_status.lower() for marker in _VPN_DOWN_MARKERS
    ):
        vpn_ip = None

    runtime = entry.runtime_data
    if vpn_ip == runtime.device_vpn_ip:
        return

    runtime.device_vpn_ip = vpn_ip
    new_data = dict(entry.data)
    if vpn_ip is None:
        new_data.pop("vpn_ip", None)
        _LOGGER.debug("Device VPN disconnected; clearing backup endpoint")
    else:
        new_data["vpn_ip"] = vpn_ip
        _LOGGER.info(
            "Device VPN address %s recorded as backup control endpoint", vpn_ip,
        )
    if new_data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=new_data)

    hass.async_create_task(_async_request_capability_probe(hass, entry))


def _merge_device_fields(
    entry: MeatPiConfigEntry,
    device_info_fields: dict[str, str],
) -> tuple[dict[str, Any], bool, bool]:
    """Merge device-reported fields into a copy of the entry data.

    Returns the merged data plus whether anything changed and whether a
    connection-relevant field (host/ip/mdns) changed.
    """
    new_data = dict(entry.data)
    connection_field_changed = False
    data_changed = False
    for key, value in device_info_fields.items():
        # Skip if value hasn't actually changed
        if new_data.get(key) == value:
            continue
        new_data[key] = value
        data_changed = True
        # Only mark as connection change if host/ip/mdns actually changed
        if key in {"host", "ip", "mdns"}:
            connection_field_changed = True
    return new_data, data_changed, connection_field_changed


def _retarget_release_stream(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    hw_version: Any,
) -> None:
    """Point the releases coordinator at a late-learned hardware stream.

    A manually added device reveals its hw_version on the first push;
    e.g. a PRO device must switch from the standard to the PRO firmware
    stream — and get the right update offered — without an entry reload.
    """
    github = entry.runtime_data.github_coordinator
    if github.set_stream(hardware_stream(hw_version)):
        hass.async_create_task(github.async_request_refresh())


def _react_to_version_changes(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    device_info_fields: dict[str, str],
    new_data: dict[str, Any],
) -> None:
    """React to device-reported firmware/hardware version changes."""
    if "hw_version" in device_info_fields:
        _retarget_release_stream(hass, entry, new_data.get("hw_version"))
    if "fw_version" in device_info_fields or "hw_version" in device_info_fields:
        _check_minimum_firmware(hass, entry)


def _persist_device_reported_info(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    data: dict[str, Any],
    request: Request,
) -> None:
    """Persist device-reported info in the config entry and react to changes."""
    device_info_fields = _collect_device_reported_fields(data)

    # Capture device IP from the inbound request as an authoritative source
    remote_ip = _extract_request_ip(request)
    if remote_ip:
        entry.runtime_data.device_ip = remote_ip
        device_info_fields["ip"] = remote_ip
        # Don't set host from IP if we already have a hostname
        # (avoid triggering re-registration when IP resolves to stored hostname)
        host_from_ip = _http_url_from_host(remote_ip)
        if host_from_ip and not entry.data.get("host"):
            entry.runtime_data.device_host = host_from_ip
            device_info_fields["host"] = host_from_ip

    if not device_info_fields:
        return

    new_data, data_changed, connection_field_changed = _merge_device_fields(
        entry, device_info_fields,
    )

    if not data_changed:
        return

    # Keep the device-type slug in sync with the reported hardware version
    # (a manually added entry learns its real product type on first push).
    # Only re-infer inside the WiCAN family: a device_type set by discovery
    # (espnetlink, meatpi, future slugs) is authoritative and must never be
    # clobbered by hardware-string guessing.
    current_type = new_data.get(CONF_DEVICE_TYPE)
    if "hw_version" in device_info_fields and (
        not current_type or current_type in WICAN_FAMILY_TYPES
    ):
        inferred_type = infer_device_type(new_data.get("hw_version"))
        if current_type != inferred_type:
            new_data[CONF_DEVICE_TYPE] = inferred_type
            entry.runtime_data.device_profile = get_profile(inferred_type)

    hass.config_entries.async_update_entry(entry, data=new_data)

    _react_to_version_changes(hass, entry, device_info_fields, new_data)

    if connection_field_changed:
        entry.runtime_data.device_host = new_data.get("host") or entry.runtime_data.device_host
        entry.runtime_data.device_ip = new_data.get("ip") or entry.runtime_data.device_ip
        # Refresh registration out-of-band so future retries use the new address
        _LOGGER.info(
            "Connection info changed for %s, re-registering webhook",
            entry.title,
        )
        hass.async_create_task(
            _async_request_webhook_registration(hass, entry, from_push=True),
        )

    # A new address or a new firmware version can change what the control
    # API offers (e.g. a device OTA-updated to V6 gains control entities).
    if connection_field_changed or "fw_version" in device_info_fields:
        hass.async_create_task(
            _async_request_capability_probe(hass, entry),
        )


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> bool:
    """Migrate old config entries to the current schema.

    1.1 → 1.2: backfill the device_type slug (brand framework). Every entry
    that predates device types is a WiCAN variant, inferred from the stored
    hardware version.

    1.2 → 1.3: an entry made before the record of the webhook settings the
    device holds is marked as holding an earlier release's settings, so a
    device asleep at the first start on this release is not reported as one
    that lacks its settings.
    """
    if entry.version > 1:
        # Downgrade from a future major version; cannot know the schema.
        return False

    if entry.minor_version < 2:
        # Load the device catalog first so inference can resolve
        # catalog-defined product types, not just the built-ins.
        await async_setup_catalog(hass)
        new_data = dict(entry.data)
        if not new_data.get(CONF_DEVICE_TYPE):
            new_data[CONF_DEVICE_TYPE] = infer_device_type(
                new_data.get("hw_version"),
            )
        hass.config_entries.async_update_entry(
            entry, data=new_data, version=1, minor_version=2,
        )
        _LOGGER.debug(
            "Migrated entry %s to 1.2 (device_type=%s)",
            entry.title,
            new_data[CONF_DEVICE_TYPE],
        )

    if entry.minor_version < 3:
        new_data = dict(entry.data)
        if CONF_HELD_WEBHOOK_REV not in new_data:
            new_data[CONF_HELD_BY_EARLIER_RELEASE] = True
        hass.config_entries.async_update_entry(
            entry, data=new_data, version=1, minor_version=3,
        )
        _LOGGER.debug("Migrated entry %s to 1.3", entry.title)

    return True


def _webhook_repair_issue_id(entry: MeatPiConfigEntry) -> str:
    """Return the repair issue id for a failed webhook registration."""
    return f"webhook_registration_failed_{entry.entry_id}"


def _raise_webhook_repair(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    endpoints: str,
) -> None:
    """Surface a repair issue when the device cannot be reached to register."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _webhook_repair_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="webhook_registration_failed",
        translation_placeholders={
            "device": entry.title,
            "endpoints": endpoints,
        },
    )


def _clear_webhook_repair(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Clear a previously raised webhook-registration repair issue."""
    ir.async_delete_issue(hass, DOMAIN, _webhook_repair_issue_id(entry))


def _https_only_repair_issue_id(entry: MeatPiConfigEntry) -> str:
    """Return the repair issue id for a no-TLS device on an HTTPS-only server."""
    return f"https_only_server_{entry.entry_id}"


def _raise_https_only_repair(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Tell the user this device cannot push to an HTTPS-only Home Assistant.

    Home Assistant's own web server has SSL on, so no plain-http address
    exists, and the device (WiCAN OBD, WiCAN USB) has no TLS stack: no URL
    can be registered on it. Clears itself at the next registration that
    finds a plain-http address.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _https_only_repair_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="https_only_server",
        translation_placeholders={"device": entry.title},
    )


def _clear_https_only_repair(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Clear a previously raised HTTPS-only-server repair issue."""
    ir.async_delete_issue(hass, DOMAIN, _https_only_repair_issue_id(entry))


def _registration_payload(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    post_interval: int,
) -> dict[str, str | bool | int | list[str]] | None:
    """Return the payload to register on the device, or None without one.

    Keeps the HTTPS-only repair issue in step with it: raised when Home
    Assistant's web server has SSL on and this device has no TLS (there is
    no address to register, and saying so beats leaving every entity
    unavailable in silence), cleared as soon as a payload can be built.
    """
    try:
        payload = _build_webhook_payload(hass, entry, post_interval)
    except NoPlainHttpURLError:
        _LOGGER.warning(
            "%s cannot push over HTTPS and Home Assistant's web server "
            "answers over HTTPS only: no webhook registered",
            entry.title,
        )
        _raise_https_only_repair(hass, entry)
        return None
    except Exception as err:
        _LOGGER.warning(
            "Cannot generate webhook URL payload for %s: %s",
            entry.entry_id,
            err,
        )
        return None
    _clear_https_only_repair(hass, entry)
    return payload


# ---- settings in the push reply (2026-11-14) -----------------------------------
#
# Home Assistant can only change a device's webhook settings (its webhook
# addresses and post interval) by calling the device. A car that is asleep
# when Home Assistant starts, or on LTE without a VPN, cannot be called,
# although the car itself reaches Home Assistant with every push. A
# firmware that reads the reply to its own push reports the revision of the
# settings it holds; when that is not the wanted one, the reply carries the
# settings (notes/device-contract/PUSH_REPLY_SETTINGS.md).

# What a revision may consist of: the device stores and reports the string
# as it was given.
_WEBHOOK_REV_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{0,{WEBHOOK_REV_MAX_LENGTH}}}")


def _reported_webhook_rev(data: dict[str, Any]) -> str | None:
    """Return the settings revision a push reports, or None without one.

    A firmware that reads push replies sends ``status.webhook_rev`` in
    every push, an empty string while it holds no revision. The key is
    absent on older firmware and on a device whose webhook the user pinned
    on the device itself: both are answered as before, without a body.
    Anything that is not a revision string counts as absent.
    """
    status = data.get("status")
    if not isinstance(status, dict):
        return None
    rev = status.get(WEBHOOK_REV_KEY)
    if not isinstance(rev, str) or _WEBHOOK_REV_PATTERN.fullmatch(rev) is None:
        return None
    return rev


def _webhook_revision(settings: Mapping[str, object]) -> str:
    """Return the revision that names one exact set of webhook settings.

    Derived from the settings themselves, so it changes only when they
    change and is the same after a restart of Home Assistant.
    """
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:WEBHOOK_REV_LENGTH]


def _remember_reply_support(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    *,
    reads_reply: bool,
) -> None:
    """Keep in the entry whether this device reads push replies.

    The registration at the next start runs before any push: it carries a
    revision only for a device known to store one.
    """
    if bool(entry.data.get(CONF_READS_PUSH_REPLY)) == reads_reply:
        return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_READS_PUSH_REPLY: reads_reply},
    )


def _remember_held_revision(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    rev: str,
) -> None:
    """Keep in the entry the revision of the webhook settings the device holds.

    A registration that cannot reach the device compares it with the
    revision it would hand over: when they are the same, the device lost
    nothing by not being reached.
    """
    if (
        entry.data.get(CONF_HELD_WEBHOOK_REV) == rev
        and CONF_HELD_BY_EARLIER_RELEASE not in entry.data
    ):
        return
    data = {**entry.data, CONF_HELD_WEBHOOK_REV: rev}
    # What the device holds is known from now on.
    data.pop(CONF_HELD_BY_EARLIER_RELEASE, None)
    hass.config_entries.async_update_entry(entry, data=data)


def _device_is_pushing(runtime: MeatPiRuntimeData) -> bool:
    """Return True while the device's pushes keep arriving.

    Such a device holds a webhook address that works, whether or not Home
    Assistant can reach it.
    """
    last_push = runtime.coordinator.last_push
    if last_push is None:
        return False
    elapsed = (dt_util.utcnow() - last_push).total_seconds()
    return elapsed <= runtime.coordinator.staleness_threshold()


def _retry_registration_after_push(
    hass: HomeAssistant, entry: MeatPiConfigEntry,
) -> None:
    """Register again on a push when the last registration missed the device.

    For a firmware that does not read push replies: the push shows it is
    awake, and a registration is the only way to hand it its settings.
    Rate-limited, since a car on LTE pushes without ever being reachable.
    """
    runtime = entry.runtime_data
    if not runtime.registration_failed:
        return
    now = time.monotonic()
    if (
        runtime.last_registration_retry != 0.0
        and now - runtime.last_registration_retry < REGISTRATION_RETRY_INTERVAL
    ):
        return
    runtime.last_registration_retry = now
    hass.async_create_task(
        _async_request_webhook_registration(hass, entry, from_push=True),
    )


def _reply_body(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    reported: str,
) -> bytes | None:
    """Return the settings to put in the reply to a push, or None.

    ``reported`` is the revision the device holds. The settings go out when
    it is not the wanted one: at once for a new difference, then at most
    every PUSH_REPLY_REPEAT_INTERVAL while the same difference remains. The
    device decides by itself whether it trusts the path a reply arrived
    on, so one that keeps its settings must not cost data on every push.
    """
    runtime = entry.runtime_data
    state = runtime.push_reply
    state.reported_rev = reported
    _remember_held_revision(hass, entry, reported)
    try:
        settings = _build_webhook_payload(hass, entry, runtime.post_interval)
    except Exception as err:
        # No address to hand out. The registration reports that (the log,
        # the repair issue); a push every few seconds must not repeat it,
        # and a failure here must never turn an accepted push into an error.
        _LOGGER.debug(
            "No webhook settings for the reply to %s: %s", entry.title, err,
        )
        state.wanted_rev = None
        return None

    wanted = _webhook_revision(settings)
    state.wanted_rev = wanted
    if reported == wanted:
        state.answered = None
        return None

    pair = (reported, wanted)
    now = time.monotonic()
    if (
        state.answered == pair
        and now - state.answered_at < PUSH_REPLY_REPEAT_INTERVAL
    ):
        return None

    body = json.dumps({"webhook": {**settings, "rev": wanted}}).encode()
    if len(body) > PUSH_REPLY_MAX_BYTES:
        _LOGGER.debug(
            "The webhook settings for %s do not fit a push reply (%d bytes)",
            entry.title,
            len(body),
        )
        return None

    state.answered = pair
    state.answered_at = now
    state.bodies_sent += 1
    state.last_body_at = dt_util.utcnow().isoformat()
    _LOGGER.debug(
        "Webhook settings %s sent to %s in the push reply (it reported %r)",
        wanted,
        entry.title,
        reported,
    )
    return body


def _answer_accepted_push(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    data: dict[str, Any],
) -> Response:
    """Answer a push that passed the identity check.

    The answer is the empty 204 it always was, unless the device reads
    push replies and does not hold the webhook settings Home Assistant
    wants it to have: then it is a 200 with those settings.
    """
    # A device that pushes holds a webhook address that works: the issue a
    # failed registration raised no longer describes it.
    _clear_webhook_repair(hass, entry)

    reported = _reported_webhook_rev(data)
    _remember_reply_support(hass, entry, reads_reply=reported is not None)
    if reported is None:
        _retry_registration_after_push(hass, entry)
        return Response(status=HTTPStatus.NO_CONTENT)

    body = _reply_body(hass, entry, reported)
    if body is None:
        return Response(status=HTTPStatus.NO_CONTENT)
    return Response(body=body, content_type="application/json")


def _note_registration_unreachable(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    runtime: MeatPiRuntimeData,
    endpoints: str,
    attempts: int,
    *,
    quiet: bool,
    holds_settings: bool,
) -> None:
    """Record that a registration could not reach the device, and say so.

    A device whose pushes keep arriving holds a webhook address that
    works, and it is handed its settings when it pushes: in the reply, or
    by a new registration attempt. A device that already holds the
    settings this registration would give it (a car asleep while Home
    Assistant starts) pushes as soon as it wakes. A device an earlier
    release registered holds that release's settings, which upgrading the
    integration did not change: it pushes as it did before, and is handed
    these settings then. "The device will not push" would be wrong for
    all three, so only a device that is silent and lacks its settings gets
    the error and the repair issue.
    """
    runtime.registration_failed = True
    if quiet or _device_is_pushing(runtime):
        _LOGGER.log(
            logging.DEBUG if quiet else logging.INFO,
            "Could not reach %s to register the webhook (tried %s); it is "
            "pushing, so its settings follow when it pushes",
            entry.title,
            endpoints,
        )
        return
    if holds_settings:
        _LOGGER.info(
            "Could not reach %s to register the webhook (tried %s); it "
            "already holds these webhook settings, so it pushes when it "
            "wakes or comes back",
            entry.title,
            endpoints,
        )
        return
    if entry.data.get(CONF_HELD_BY_EARLIER_RELEASE):
        _LOGGER.info(
            "Could not reach %s to register the webhook (tried %s); it "
            "holds the webhook settings an earlier version of this "
            "integration gave it, and is handed these when it next pushes",
            entry.title,
            endpoints,
        )
        return

    _LOGGER.error(
        "Failed to register webhook after %d attempts. "
        "Device may not send updates to Home Assistant. "
        "Please check: 1) Device is powered on and connected to network, "
        "2) Home Assistant can reach device at %s, "
        "3) Device firewall allows connections on port 80",
        attempts,
        endpoints,
    )
    # The retries can outlive the entry (unload clears the repair issue);
    # never raise one for an entry that is no longer this lifecycle.
    if getattr(entry, "runtime_data", None) is runtime:
        _raise_webhook_repair(hass, entry, endpoints)


def _firmware_repair_issue_id(entry: MeatPiConfigEntry) -> str:
    """Return the repair issue id for below-minimum firmware."""
    return f"firmware_update_required_{entry.entry_id}"


def _check_minimum_firmware(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Flag WiCAN devices below the contract-v2 minimum firmware.

    Telemetry keeps working regardless; the persistent repair issue tells
    the user a firmware update is required (legacy device accommodations
    are no longer maintained below the minimum). Clears itself once the
    device reports a supported version. Non-WiCAN products are exempt —
    their firmware lines version independently.
    """
    if entry.data.get(CONF_DEVICE_TYPE) not in WICAN_FAMILY_TYPES:
        return
    issue_id = _firmware_repair_issue_id(entry)
    fw_version = entry.data.get("fw_version")
    stream = hardware_stream(entry.data.get("hw_version"))
    minimum = MIN_FIRMWARE_BY_STREAM.get(stream)
    if (
        minimum is None
        or _parse_version(fw_version if isinstance(fw_version, str) else None)
        is None
        or _is_version_at_least(fw_version, minimum)
    ):
        # Supported, or version not yet known (don't nag before the
        # device has ever reported one).
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="firmware_update_required",
        translation_placeholders={
            "device": entry.title,
            "installed": str(fw_version),
            "minimum": f"{minimum[0]}.{minimum[1]:02d}",
        },
    )


async def async_setup_entry(  # noqa: C901, PLR0915
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> bool:
    """Set up WiCAN from a config entry."""
    # Sync PID parameter definitions (stored copy applies immediately; a
    # coalesced, ETag-cheap GitHub fetch keeps the reload-to-refresh
    # support flow instant). Best-effort: bundled/stored copy on failure.
    try:
        await async_update_params_from_github(hass)
    except Exception as err:
        _LOGGER.debug("Could not update params from GitHub: %s", err)
        # Continue with bundled/cached version

    # Ensure webhook_id exists (older entries may lack it); generate if missing
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        try:
            webhook_id = uuid4().hex
            new_data = dict(entry.data)
            new_data[CONF_WEBHOOK_ID] = webhook_id
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info("Generated missing webhook_id for entry %s", entry.title)
        except Exception:
            _LOGGER.warning("Failed to generate webhook_id; setup may fail")
            return False

    _async_setup_services(hass)
    await _async_register_frontend(hass)

    # Load the device catalog (bundled/stored copy applies immediately, a
    # remote refresh runs in the background at most once per day) so
    # catalog-defined products resolve to their real profiles.
    await async_setup_catalog(hass)

    # Resolve the device-type profile (brand framework). Migration backfills
    # device_type for old entries; this guard covers entries created by
    # flows that could not know the type yet (e.g. manual setup).
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    if not device_type:
        device_type = infer_device_type(entry.data.get("hw_version"))
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_TYPE: device_type},
        )
    device_profile = get_profile(device_type)

    # Get post interval from options
    post_interval = entry.options.get(CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL)

    # Create coordinator for this entry
    coordinator = MeatPiDataUpdateCoordinator(
        hass,
        entry,
    )

    # Track the firmware stream matching this device's hardware (PRO /
    # USB / OBD). A manually added entry may not know its hw_version yet;
    # _persist_device_reported_info re-derives the stream on first push.
    github_coordinator = GitHubReleasesCoordinator(
        hass, stream=hardware_stream(entry.data.get("hw_version")),
    )
    try:
        await github_coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "Failed to fetch GitHub releases (firmware updates unavailable): %s",
            err,
        )
        # Don't fail setup - update entity will just show as unavailable

    # Set runtime_data with all necessary data
    entry.runtime_data = MeatPiRuntimeData(
        coordinator=coordinator,
        github_coordinator=github_coordinator,
        webhook_id=webhook_id,
        post_interval=post_interval,
        device_host=entry.data.get("host"),
        device_ip=entry.data.get("ip"),
        device_vpn_ip=_validated_vpn_ip(entry.data.get("vpn_ip")),
        device_profile=device_profile,
    )

    # Perform first refresh to initialize coordinator
    # For push-based WiCAN, this succeeds immediately with empty data
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "First refresh failed for %s (push-based integration will retry): %s",
            entry.title,
            err,
        )
        # Don't fail setup - entities will update when first webhook arrives

    async def handle_webhook(  # noqa: PLR0911
        hass: HomeAssistant,
        webhook_id: str,
        request: Request,
    ) -> Response:
        """Handle incoming WiCAN webhook request."""
        _LOGGER.info("Received MeatPi device webhook: %s", webhook_id)
        try:
            data = await _async_read_webhook_json(request)
            _LOGGER.debug("Webhook payload: %s", data)
        except vol.MultipleInvalid as error:
            return Response(
                text=error.error_message, status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        except _WebhookBodyTooLargeError:
            _LOGGER.warning(
                "Rejecting oversized device webhook body (> %d bytes)",
                MAX_WEBHOOK_BODY_BYTES,
            )
            return Response(
                text="Payload too large",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except Exception as err:
            # Not valid JSON (json.JSONDecodeError is a ValueError), a broken
            # gzip stream (aiohttp raises its content-encoding errors on
            # read), or transport garbage. One clean 422 for all of it; a
            # broken device must never wedge the webhook.
            _LOGGER.warning(
                "Received device webhook with an invalid or undecodable body "
                "(%s: %s)",
                type(err).__name__,
                err,
            )
            return Response(
                text="Invalid JSON body",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        # The entry can be unloaded while the body read above was awaited
        # (runtime_data is deleted on unload). Everything below runs
        # synchronously, so this single re-check makes the whole handler
        # unload-safe: answer 503 so the device retries after the reload
        # instead of believing the push was delivered.
        if getattr(entry, "runtime_data", None) is None:
            _LOGGER.debug(
                "Dropping webhook push that raced an unload of %s", entry.title,
            )
            return Response(
                text="Integration reloading",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        # WiCAN always sends a JSON object. Reject anything else defensively so a
        # malformed device (or unrelated caller) cannot crash the handler.
        if not isinstance(data, dict):
            _LOGGER.warning(
                "Received device webhook with non-object JSON payload (%s)",
                type(data).__name__,
            )
            return Response(
                text="Expected a JSON object",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        # Validate identity and apply data BEFORE persisting any device-reported
        # connection info. Otherwise an impostor's device_id would be written to
        # the config entry first and the identity check would pass against it.
        try:
            resumed_after_gap = coordinator.handle_webhook_data(data)
        except ConfigEntryError:
            _LOGGER.exception(
                "Rejecting webhook due to device identity validation failure",
            )
            return Response(
                text="Device identity mismatch",
                status=HTTPStatus.FORBIDDEN,
            )

        # Persist any device-reported info (fw/hw versions, connection data).
        _persist_device_reported_info(hass, entry, data, request)

        # Track the device's VPN tunnel address (backup control endpoint
        # for cars on the road via WireGuard/Tailscale).
        _update_vpn_state(hass, entry, data)

        # A device that pushes is clearly awake. If the capability probe has
        # never reached it (device was asleep/unreachable at setup), retry
        # now — rate-limited — so it still gains its control entities.
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None and not runtime.probe_successful:
            now = time.monotonic()
            if (
                runtime.last_probe_retry == 0.0
                or now - runtime.last_probe_retry >= PROBE_RETRY_INTERVAL
            ):
                runtime.last_probe_retry = now
                hass.async_create_task(
                    _async_request_capability_probe(hass, entry),
                )

        # This push ended a silence gap: the device may have logged data to
        # its SD card while away — backfill it into long-term statistics.
        if resumed_after_gap:
            _schedule_history_sync(hass, entry)

        # Keep dispatcher for backward compatibility during migration
        async_dispatcher_send(hass, DOMAIN, webhook_id, data)
        return _answer_accepted_push(hass, entry, data)

    # The webhooks of entries removed earlier answer 410 Gone from here on.
    # An entry that comes back with its old webhook id (a restored backup)
    # takes it over again.
    removed_webhooks = await async_get_removed_webhooks(hass)
    await removed_webhooks.async_forget(webhook_id)

    webhook.async_register(
        hass, DOMAIN, entry.title, webhook_id, handle_webhook,
    )

    try:
        # Normalize host/mdns schemes BEFORE scheduling registration
        # to avoid triggering update listener which would cause duplicate
        # registrations
        _normalize_connection_urls(hass, entry)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        _schedule_webhook_registration(hass, entry)

        # Discover the device's control capabilities (V6 HTTP API)
        # out-of-band. A legacy or unreachable device simply keeps legacy
        # capabilities.
        hass.async_create_task(_async_request_capability_probe(hass, entry))

        # The hours a catch-up kept before a restart (the last hour of a
        # drive, the device asleep since) wait on disk: book their run.
        hass.async_create_task(_async_resume_kept_history(hass, entry))

        entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

        # Contract v2: devices below the minimum firmware keep telemetry
        # but the user is told an update is required.
        _check_minimum_firmware(hass, entry)
    except Exception:
        # A failed setup must leave no webhook behind: HA retries setup,
        # and core raises "Handler is already defined!" on a duplicate
        # register — which would wedge the entry until restart.
        webhook.async_unregister(hass, webhook_id)
        raise

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MeatPiConfigEntry,
) -> bool:
    """Unload a config entry."""
    # Platforms first: if their unload fails the entry stays loaded, and
    # unregistering the webhook up front would leave it loaded but deaf
    # to pushes.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        _cancel_history_followup(entry.runtime_data)
        webhook.async_unregister(hass, entry.runtime_data.webhook_id)
        _clear_webhook_repair(hass, entry)
        _clear_https_only_repair(hass, entry)
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: MeatPiConfigEntry,
) -> None:
    """Clean up persisted state when a config entry is removed for good."""
    ir.async_delete_issue(hass, DOMAIN, _firmware_repair_issue_id(entry))
    await async_remove_history_store(hass, entry)
    # A device that cannot be told now learns it from the answer to its
    # next post: the webhook answers 410 Gone from here on.
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if isinstance(webhook_id, str) and webhook_id:
        removed_webhooks = await async_get_removed_webhooks(hass)
        await removed_webhooks.async_add(webhook_id)
    # Not awaited: the device may be asleep, and removing the entry must
    # not wait for a connection attempt to give up.
    hass.async_create_task(
        _async_release_device_webhook(hass, entry),
        f"{DOMAIN} release device webhook {entry.entry_id}",
    )


def _stored_device_base_urls(entry: MeatPiConfigEntry) -> list[str]:
    """Return the device's addresses as the entry stores them, local first.

    For an entry that is no longer loaded (it has no runtime data).
    """
    candidates = (
        entry.data.get("host"),
        entry.data.get("mdns"),
        _http_url_from_host(_validated_vpn_ip(entry.data.get("vpn_ip"))),
    )
    unique: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in unique:
            unique.append(candidate)
    return unique


async def _async_release_device_webhook(
    hass: HomeAssistant, entry: MeatPiConfigEntry,
) -> bool:
    """Ask the device of a removed entry to stop posting to its webhook.

    Home Assistant answers a post to a webhook it no longer knows with
    200, so the device went on posting for good and counted every post
    as delivered. The device is asked for its settings first and is left
    alone unless they still name this entry's webhook: it may post to
    another Home Assistant by now, or to an entry added again since. A
    webhook pinned on the device (manual override) is the user's own.

    Best effort, never raises: a device that is asleep or away cannot be
    told here. It learns it from the 410 Gone its next post is answered
    with (removed_webhooks.py), when its firmware understands that.
    """
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not isinstance(webhook_id, str) or not webhook_id:
        return False
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(WEBHOOK_RELEASE_TIMEOUT):
            for base in _stored_device_base_urls(entry):
                endpoint = _build_webhook_endpoint(base)
                if endpoint is None:
                    continue
                try:
                    released = await async_release_webhook_at(
                        session, endpoint, (webhook_id,),
                    )
                except (ClientError, TimeoutError, ValueError) as err:
                    _LOGGER.debug("No answer about the webhook at %s: %r", endpoint, err)
                    continue
                if released is not None:
                    return released
    except TimeoutError:
        _LOGGER.debug("The device of the removed entry %s did not answer in time", entry.title)
    _LOGGER.info(
        "The device of the removed entry %s could not be reached: its next "
        "post is answered with 410 Gone, on which firmware 6.0.0-beta.11 or "
        "later stops posting",
        entry.title,
    )
    return False


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001 - required by Home Assistant
    entry: MeatPiConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a device that is no longer the configured adapter.

    The device is keyed on the adapter's device_id, or on the entry id until
    the adapter reports one. A device under any other identifier (the entry-id
    placeholder once the device_id is known, or an adapter that has been
    replaced) has no entities left and could not be deleted. The current
    adapter's device stays protected: it would come back on the next post.
    """
    current = entry.data.get("device_id") or entry.entry_id
    return (DOMAIN, current) not in device_entry.identifiers


def _normalize_connection_urls(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Normalize host/mdns URLs by ensuring they have http:// scheme.

    This is done once during setup to avoid triggering update listener
    which would cause duplicate webhook registrations.
    """
    updated_data = dict(entry.data)
    data_changed = False

    # Normalize mdns
    mdns = updated_data.get("mdns")
    if mdns:
        normalized_mdns = _ensure_http_scheme(mdns)
        if normalized_mdns != mdns:
            updated_data["mdns"] = normalized_mdns
            data_changed = True

    # Normalize host
    host = updated_data.get("host")
    if host:
        normalized_host = _ensure_http_scheme(host)
        if normalized_host != host:
            updated_data["host"] = normalized_host
            data_changed = True

    if data_changed:
        hass.config_entries.async_update_entry(entry, data=updated_data)


def _schedule_webhook_registration(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    if hass.is_running:
        hass.async_create_task(_async_request_webhook_registration(hass, entry))
        return

    cancelled = False

    @callback
    def _on_started(_: object) -> None:
        if not cancelled:
            hass.async_create_task(_async_request_webhook_registration(hass, entry))

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

    def _cancel_startup_registration() -> None:
        # A flag, not an unsubscribe: an entry can be unloaded while the
        # started event is being fired (a rediscovery that changes its
        # address reloads it). The listener is then already taken for
        # that event, removing it logged "Unable to remove unknown job
        # listener" and the registration still ran for the unloaded
        # entry. The once-listener removes itself when it fires.
        nonlocal cancelled
        cancelled = True

    entry.async_on_unload(_cancel_startup_registration)


def _device_api_base_urls(entry: MeatPiConfigEntry) -> list[str]:
    """Return control-API base URL candidates, most-local first.

    Local addresses are tried first; the device-reported VPN tunnel address
    is the backup for cars on the road (WireGuard/Tailscale); the last
    push-source IP is the final fallback.
    """
    runtime = entry.runtime_data
    candidates = [
        str(candidate)
        for candidate in (
            runtime.device_host,
            entry.data.get("host"),
            entry.data.get("mdns"),
            _http_url_from_host(runtime.device_vpn_ip),
            _http_url_from_host(runtime.device_ip),
        )
        if candidate
    ]
    # Deduplicate, keeping order.
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


async def _async_request_capability_probe(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> None:
    """Probe device capabilities, coalescing concurrent requests.

    Setup, address changes, and firmware-version changes can all request a
    probe; like webhook registration, run one at a time and fold requests
    that arrive meanwhile into a single trailing re-run.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    runtime.probe_pending = True
    if runtime.probe_running:
        return
    runtime.probe_running = True
    try:
        while runtime.probe_pending:
            runtime.probe_pending = False
            await _async_probe_capabilities(hass, entry)
    finally:
        runtime.probe_running = False


async def _async_probe_candidates(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    base_urls: list[str],
) -> tuple[MeatPiApiClient, DeviceCapabilities | None]:
    """Probe candidates in order; return (winning client, capabilities).

    Capabilities is None when no candidate answered; the returned client is
    then bound to the first (most local) candidate so later commands fail
    with a clear connection error.
    """
    session = async_get_clientsession(hass)
    api = MeatPiApiClient(session, base_urls[0])
    for base_url in base_urls:
        candidate_api = MeatPiApiClient(session, base_url)
        try:
            candidate_result = await candidate_api.async_probe()
        except Exception as err:
            # One broken candidate (odd resolver/transport error) must
            # never keep the remaining candidates from being tried.
            _LOGGER.debug(
                "Probe candidate %s failed unexpectedly: %s", base_url, err,
            )
            continue
        if candidate_result is not None:
            if base_url != base_urls[0]:
                _LOGGER.info(
                    "Device %s reachable via backup endpoint %s",
                    entry.title,
                    base_url,
                )
            return candidate_api, candidate_result
    return api, None


async def _async_probe_capabilities(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> None:
    """Probe the device control API once and publish the result.

    Never raises: a legacy or unreachable device yields legacy capabilities,
    and the integration behaves exactly as it did before control support.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    base_urls = _device_api_base_urls(entry)
    if not base_urls:
        _LOGGER.debug(
            "Entry %s has no device address; skipping capability probe",
            entry.entry_id,
        )
        return

    # Try candidates most-local first; the VPN tunnel address is the backup
    # for cars on the road. The first reachable candidate wins and becomes
    # the bound control endpoint.
    try:
        api, capabilities = await _async_probe_candidates(hass, entry, base_urls)
    except Exception:  # Defensive: a probe must never break anything else.
        _LOGGER.exception("Unexpected error probing device capabilities")
        return

    # The entry may have been unloaded while the probe was in flight.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return

    runtime.api = api

    if capabilities is None:
        # Device unreachable on every candidate (asleep, offline,
        # mid-reboot): keep last-known capabilities and let telemetry
        # pushes retry the probe later.
        runtime.probe_successful = False
        return
    runtime.probe_successful = True

    previous = runtime.capabilities
    runtime.capabilities = capabilities

    if capabilities.has_http_api and not previous.has_http_api:
        _LOGGER.info(
            "Device %s exposes the control HTTP API (%d components); "
            "control entities enabled",
            entry.title,
            len(capabilities.components),
        )

    if capabilities != previous:
        async_dispatcher_send(
            hass, f"{SIGNAL_CAPABILITIES_UPDATED}_{entry.entry_id}",
        )

    # A reachable data_logger device may hold rows recorded while HA was
    # down or the device was away — catch up (rate-limited, coalesced).
    if capabilities.has_component(COMPONENT_DATA_LOGGER):
        _schedule_history_sync(hass, entry)


async def _async_resume_kept_history(
    hass: HomeAssistant, entry: MeatPiConfigEntry,
) -> None:
    """Book the run that imports the hours a catch-up kept before a restart.

    The run needs no device: it imports from the kept copy when the device
    does not answer (history.py). Never raises.
    """
    try:
        if not entry.options.get(CONF_HISTORY_SYNC, DEFAULT_HISTORY_SYNC):
            return
        delay = await async_kept_hours_due_in(hass, entry)
        runtime = getattr(entry, "runtime_data", None)
        if delay is None or runtime is None or runtime.history_followup is not None:
            return
        _schedule_history_followup(hass, entry, delay)
    except Exception:  # Defensive: a kept hour must never break setup.
        _LOGGER.exception("Unexpected error resuming the kept history hours")


def _schedule_history_sync(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Schedule a history catch-up run if enabled, capable, and not rate-limited."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    if not entry.options.get(CONF_HISTORY_SYNC, DEFAULT_HISTORY_SYNC):
        return
    if not runtime.capabilities.has_component(COMPONENT_DATA_LOGGER):
        return
    now = time.monotonic()
    if (
        runtime.last_history_sync
        and now - runtime.last_history_sync < HISTORY_SYNC_MIN_INTERVAL
    ):
        # Too soon after the last run (a car home again right after the
        # run at Home Assistant's start, a bench): book it for the end of the
        # window instead of dropping it. A follow-up that is booked for
        # later than that (the hour still running, which is every hour once
        # the device logs its battery voltage) gives way: the run at the
        # window's end books it again. Until 2.0.0-beta1 any pending
        # follow-up made this trigger a no-op, and the pull of a return
        # waited for the top of the hour.
        wait = HISTORY_SYNC_MIN_INTERVAL - (now - runtime.last_history_sync)
        due = runtime.history_followup_due
        if (
            runtime.history_followup is None
            or due is None
            or due - time.time() > wait
        ):
            _schedule_history_followup(hass, entry, wait)
        return
    runtime.last_history_sync = now
    runtime.history_followups = 0
    hass.async_create_task(_async_request_history_sync(hass, entry))


def async_request_history_sync_now(hass: HomeAssistant, entry: MeatPiConfigEntry) -> bool:
    """Run a catch-up on request (the Catch up history button), rate limit aside.

    Returns False when the device has no data logger to read.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or not runtime.capabilities.has_component(COMPONENT_DATA_LOGGER):
        return False
    runtime.last_history_sync = time.monotonic()
    runtime.history_followups = 0
    hass.async_create_task(_async_request_history_sync(hass, entry))
    return True


def _cancel_history_followup(runtime: MeatPiRuntimeData) -> None:
    if runtime.history_followup is not None:
        runtime.history_followup()
        runtime.history_followup = None
    runtime.history_followup_due = None


def _schedule_history_followup(
    hass: HomeAssistant, entry: MeatPiConfigEntry, delay: float,
) -> None:
    """Come back for what one run could not take: the hour still running
    (imported once it has ended) or a device with more rows than one run
    reads. One timer per entry, replaced by each run, bounded in a row."""
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    _cancel_history_followup(runtime)
    if runtime.history_followups >= HISTORY_MAX_FOLLOWUPS:
        _LOGGER.warning(
            "History sync for %s: %d follow-up runs in a row; waiting for the "
            "next reconnect instead",
            entry.title,
            runtime.history_followups,
        )
        return
    runtime.history_followups += 1

    @callback
    def _fire(_now: Any) -> None:
        current = getattr(entry, "runtime_data", None)
        if current is None:
            return
        current.history_followup = None
        current.history_followup_due = None
        if not entry.options.get(CONF_HISTORY_SYNC, DEFAULT_HISTORY_SYNC):
            return
        current.last_history_sync = time.monotonic()
        hass.async_create_task(_async_request_history_sync(hass, entry))

    runtime.history_followup = async_call_later(hass, delay, _fire)
    runtime.history_followup_due = time.time() + delay


async def _async_request_history_sync(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> None:
    """Run a history catch-up, coalescing concurrent requests.

    Same pattern as webhook registration and the capability probe: one run
    at a time, requests arriving meanwhile fold into a single trailing
    re-run. The sync itself never raises. A run that leaves something for
    later (the hour still running, more rows than it could take) books a
    follow-up timer.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    runtime.history_sync_pending = True
    if runtime.history_sync_running:
        return
    runtime.history_sync_running = True
    try:
        while runtime.history_sync_pending:
            runtime.history_sync_pending = False
            result = await async_sync_history(hass, entry)
            # The entry may have been unloaded while the sync was running.
            current = getattr(entry, "runtime_data", None)
            if current is None:
                return
            current.last_history_result = result
            if not result.ran:
                # The device was not reached (asleep, out of range): this
                # run does not hold back the one its next push asks for.
                current.last_history_sync = 0.0
            if result.followup_in is not None:
                _schedule_history_followup(hass, entry, result.followup_in)
            elif result.ran:
                current.history_followups = 0
    finally:
        runtime.history_sync_running = False


async def _async_request_webhook_registration(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    *,
    from_push: bool = False,
) -> None:
    """Register the webhook on the device, coalescing concurrent requests.

    Pushes and option updates can request re-registration faster than one
    (retried, backed-off) registration attempt completes, for example a
    device that alternates between two IP addresses. Run a single registration
    at a time and fold any requests that arrive meanwhile into one trailing
    re-run, so registration attempts can never pile up against the device.

    ``from_push`` marks a request made because the device pushed (its
    address changed, or its last registration failed). Such a run is quiet:
    the device is awake and has a webhook address that works, so a failure
    is no news. A run asked for by anything else stays loud, also when a
    quiet request is folded into it.
    """
    # The entry may have been unloaded between scheduling and execution;
    # runtime_data is deleted on unload.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    runtime.registration_pending = True
    if not from_push:
        runtime.registration_loud = True
    if runtime.registration_running:
        return
    runtime.registration_running = True
    try:
        while runtime.registration_pending:
            runtime.registration_pending = False
            quiet = not runtime.registration_loud
            runtime.registration_loud = False
            await _async_register_webhook_on_device(hass, entry, quiet=quiet)
            # A registration attempt can span seconds (retries, backoff);
            # if the entry was unloaded or reloaded meanwhile this loop's
            # lifecycle is over — a new setup schedules its own run.
            current_runtime: Any = getattr(entry, "runtime_data", None)
            if current_runtime is not runtime:
                return
    finally:
        runtime.registration_running = False


async def _async_register_webhook_on_device(  # noqa: C901, PLR0912, PLR0915
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    max_retries: int = WEBHOOK_MAX_RETRIES,
    *,
    quiet: bool = False,
) -> bool:
    """Push webhook URL and interval to the WiCAN device with retry.

    ``quiet`` is a run a push asked for (see
    _async_request_webhook_registration): a failed attempt is logged at
    debug level and raises no repair issue.
    """
    # Hold a local reference: the entry can be unloaded while this
    # function awaits (runtime_data is deleted on unload), and writing
    # to a stale runtime object is harmless while re-reading the entry
    # attribute would raise.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return False

    # Prefer direct IP/host if available (similar to WLED), fallback to mDNS
    host = runtime.device_host or entry.data.get("host")
    ip = runtime.device_ip or entry.data.get("ip")
    mdns = entry.data.get("mdns")

    if not host and ip:
        host = _http_url_from_host(ip)
        runtime.device_host = host

    if not host and not mdns:
        _LOGGER.debug(
            "Entry %s missing host/mdns; cannot register webhook",
            entry.entry_id,
        )
        return False

    # URLs already normalized during setup, just ensure runtime_data is updated
    if host:
        runtime.device_host = host
    elif ip and not host:
        # Derive host from IP if needed
        derived_host = _http_url_from_host(ip)
        if derived_host:
            host = derived_host
            runtime.device_host = derived_host

    # Get post interval from runtime_data
    post_interval = runtime.post_interval

    payload = _registration_payload(hass, entry, post_interval)
    if payload is None:
        return False

    _LOGGER.info(
        "Registering device webhook %s with interval %ss",
        payload["url"],
        post_interval,
    )

    # Keep the stored URL on what is registered now. It is the fallback
    # for a start without a usable network address, so a URL from an older
    # server configuration (SSL off, another address) must not stay in it.
    if entry.data.get("webhook_url") != payload["url"]:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "webhook_url": payload["url"]},
        )
        _LOGGER.debug("Stored webhook_url updated for entry %s", entry.entry_id)

    # A device that reads push replies stores the revision of the settings
    # it is given and reports it back. Sent only to a device that has
    # reported one: firmware without the feature never sees the field.
    revision = _webhook_revision(payload)
    if entry.data.get(CONF_READS_PUSH_REPLY):
        payload = {**payload, "rev": revision}
    # A device asleep or away that holds these settings already misses
    # nothing: its failed attempts are no news either.
    holds_settings = entry.data.get(CONF_HELD_WEBHOOK_REV) == revision
    held_before = bool(entry.data.get(CONF_HELD_BY_EARLIER_RELEASE))
    attempt_level = (
        logging.DEBUG if quiet or holds_settings or held_before
        else logging.WARNING
    )

    # Use HA's shared session (reuses connections)
    session = async_get_clientsession(hass)

    # Build endpoint candidates: prefer direct host/IP over mDNS
    # Use cached resolved IP if available and fresh (< 5 minutes old)
    endpoints: list[URL] = []

    # Check if we have a cached IP and it's still valid
    if (
        runtime.cached_resolved_ip
        and runtime.cache_timestamp
        and (time.time() - runtime.cache_timestamp) < IP_CACHE_DURATION
    ):
        cached_endpoint = _build_webhook_endpoint(
            f"http://{runtime.cached_resolved_ip}",
        )
        if cached_endpoint:
            endpoints.append(cached_endpoint)
            _LOGGER.debug(
                "Using cached IP %s (age: %.1fs)",
                runtime.cached_resolved_ip,
                time.time() - runtime.cache_timestamp,
            )

    # Add host and mDNS as fallback, then the device-reported VPN tunnel
    # address (a car on the road via WireGuard/Tailscale).
    vpn_url = _http_url_from_host(runtime.device_vpn_ip)
    for candidate in (host, mdns, vpn_url):
        endpoint = _build_webhook_endpoint(candidate)
        if endpoint:
            endpoints.append(endpoint)

    if not endpoints:
        _LOGGER.debug(
            "Entry %s has no valid device endpoints after normalization",
            entry.entry_id,
        )
        return False
    _LOGGER.debug(
        "Entry %s will register webhook against endpoints: %s",
        entry.entry_id,
        ", ".join(str(ep) for ep in endpoints),
    )

    # Retry loop with exponential backoff
    for attempt in range(max_retries):
        try:
            # Add timeout protection
            async with asyncio.timeout(WEBHOOK_REGISTRATION_TIMEOUT):
                # Try each endpoint candidate until one succeeds
                for ep in endpoints:
                    try:
                        resp = await session.post(
                            str(ep),
                            json=payload,
                            headers={"Content-Type": "application/json"},
                        )

                        if resp.status < 300:
                            # Consume the (tiny) body so the connection
                            # returns to the pool instead of lingering
                            # until GC ("Unclosed response" churn).
                            await resp.read()
                            _LOGGER.info(
                                "Device webhook registered successfully at %s (attempt %d/%d)",
                                ep,
                                attempt + 1,
                                max_retries,
                            )

                            # Cache the successful IP for future registrations
                            try:
                                # Extract IP from endpoint URL
                                endpoint_host = ep.host
                                if endpoint_host and not endpoint_host.endswith(".local"):
                                    runtime.cached_resolved_ip = endpoint_host
                                    runtime.cache_timestamp = time.time()
                                    _LOGGER.debug(
                                        "Cached resolved IP: %s",
                                        endpoint_host,
                                    )
                            except Exception as cache_err:
                                _LOGGER.debug(
                                    "Failed to cache IP: %s", cache_err,
                                )

                            # Clear any prior "cannot register" repair issue.
                            _clear_webhook_repair(hass, entry)
                            runtime.registration_failed = False
                            _remember_held_revision(hass, entry, revision)
                            return True

                        text = await resp.text()
                        _LOGGER.log(
                            attempt_level,
                            "Device webhook registration failed with HTTP %d at %s: %s (attempt %d/%d)",
                            resp.status,
                            ep,
                            text,
                            attempt + 1,
                            max_retries,
                        )
                    except ClientError as err:
                        # Keep trying other endpoints if one fails to resolve/connect
                        _LOGGER.log(
                            attempt_level,
                            "Device webhook registration connection error at %s: %s (attempt %d/%d)",
                            ep,
                            err,
                            attempt + 1,
                            max_retries,
                        )

                # No endpoint succeeded during this attempt
                raise _WebhookEndpointsFailedError  # noqa: TRY301

        except TimeoutError:
            _LOGGER.log(
                attempt_level,
                "Device webhook registration timeout after %ds (attempt %d/%d)",
                WEBHOOK_REGISTRATION_TIMEOUT,
                attempt + 1,
                max_retries,
            )
        except _WebhookEndpointsFailedError:
            _LOGGER.debug(
                "All device endpoints failed for entry %s on attempt %d/%d",
                entry.entry_id,
                attempt + 1,
                max_retries,
            )
        except ClientResponseError as err:
            _LOGGER.log(
                attempt_level,
                "Device webhook registration HTTP error: %s (attempt %d/%d)",
                err,
                attempt + 1,
                max_retries,
            )
        except ClientError as err:
            _LOGGER.log(
                attempt_level,
                "Device webhook registration connection error: %s (attempt %d/%d)",
                err,
                attempt + 1,
                max_retries,
            )
        except Exception:
            _LOGGER.exception(
                "Device webhook registration unexpected error (attempt %d/%d)",
                attempt + 1,
                max_retries,
            )

        # Exponential backoff before retry (except on last attempt)
        if attempt < max_retries - 1:
            backoff_seconds = WEBHOOK_RETRY_DELAY_BASE ** attempt  # 1s, 2s, 4s
            _LOGGER.debug("Retrying in %ds...", backoff_seconds)
            await asyncio.sleep(backoff_seconds)

    # All retries failed
    _note_registration_unreachable(
        hass,
        entry,
        runtime,
        ", ".join(str(ep) for ep in endpoints),
        max_retries,
        quiet=quiet,
        holds_settings=holds_settings,
    )
    return False


async def _async_entry_updated(hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
    """Apply changed options without reloading the entry.

    A new post interval is sent to the device by re-registering the
    webhook. The keep-last-values option only changes what entities show,
    so it is applied here in Home Assistant and nothing is sent: that
    option is typically changed while the device is asleep, when a
    registration attempt can only fail and raise its repair issue.
    """
    # The listener can fire while the entry is tearing down (runtime_data
    # is deleted on unload); the reload that follows re-reads the options.
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return

    runtime.coordinator.async_options_updated()

    post_interval = entry.options.get(CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL)
    if post_interval == runtime.post_interval:
        # The listener also fires for entry data updates (device-reported
        # info, PID definitions): nothing the device needs to hear about.
        return
    runtime.post_interval = post_interval
    # The device lacks the new interval whatever an earlier release gave it.
    if CONF_HELD_BY_EARLIER_RELEASE in entry.data:
        data = dict(entry.data)
        del data[CONF_HELD_BY_EARLIER_RELEASE]
        hass.config_entries.async_update_entry(entry, data=data)

    # Re-register webhook with new interval (coalesced with any in-flight run)
    await _async_request_webhook_registration(hass, entry)


# ---- the GPS track (2026-11-06) --------------------------------------------------

SAVE_GPX_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional(ATTR_HOURS, default=DEFAULT_GPX_HOURS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_GPX_HOURS),
        ),
    },
)

_GPX_SEGMENT = re.compile(r"<trkseg>.*?</trkseg>", re.DOTALL)


def _entry_for_device(hass: HomeAssistant, device_id: str) -> MeatPiConfigEntry:
    """The loaded MeatPi config entry behind a Home Assistant device id."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="device_not_found",
        )
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == DOMAIN and getattr(
            entry, "runtime_data", None,
        ) is not None:
            return entry
    raise HomeAssistantError(
        translation_domain=DOMAIN, translation_key="device_not_found",
    )


def merge_gpx_documents(documents: list[str]) -> str:
    """One GPX document out of the device's pages (its scan caps a page).

    The first page's header and track wrapper are kept; the track segments
    of the later pages are appended before the closing tags. The device's
    trailing comments are dropped.
    """
    if not documents:
        return ""
    first = documents[0]
    first = first[: first.index("</trk>")] if "</trk>" in first else first
    segments = [
        seg for doc in documents[1:] for seg in _GPX_SEGMENT.findall(doc)
    ]
    return first + "\n".join(segments) + ("\n" if segments else "") + "</trk>\n</gpx>\n"


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@callback
async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the GPS track card and register it as a frontend resource.

    Once per Home Assistant run: the file under ``www/`` is served at
    ``/wican-static/wican-gpx-card.js`` and loaded by every frontend, so
    ``type: custom:wican-gpx-card`` works without a resource entry. The
    version in the query string defeats a stale browser cache after an
    update.
    """
    if hass.data.get(DATA_FRONTEND_REGISTERED):
        return
    http = getattr(hass, "http", None)
    if http is None or "frontend" not in hass.config.components:
        # no http or no frontend (a bare test instance, a headless setup):
        # the card is then a manual resource; neither is forced by the
        # manifest, a config entry must not start the HTTP server
        return
    hass.data[DATA_FRONTEND_REGISTERED] = True
    # the integration's own view, not a generic static path: the content
    # type is stated (a browser refuses a module script served as text,
    # which a Pi's mime table did on the bench, 2026-11-06)
    http.register_view(WicanStaticView())
    version = await _async_integration_version(hass)
    add_extra_js_url(hass, f"{FRONTEND_STATIC_URL}/{GPX_CARD_FILE}?v={version}")


class WicanStaticView(HomeAssistantView):
    """The integration's frontend files (the GPS track card)."""

    url = FRONTEND_STATIC_URL + "/{filename}"
    name = "wican:static"
    requires_auth = False

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        """Serve one of the shipped files; anything else is 404."""
        if filename != GPX_CARD_FILE:
            raise web.HTTPNotFound
        path = Path(__file__).parent / "www" / filename
        if not path.is_file():
            raise web.HTTPNotFound
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "text/javascript; charset=utf-8",
                "Cache-Control": "no-cache",
            },
        )


async def _async_integration_version(hass: HomeAssistant) -> str:
    """The integration's manifest version (for the card's cache key)."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception:  # noqa: BLE001
        return "0"
    return str(integration.version or "0")


def _async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services once (the first entry's setup)."""
    if hass.services.has_service(DOMAIN, SERVICE_SAVE_GPX):
        return

    async def _async_save_gpx(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data["device_id"])
        api = entry.runtime_data.api
        if api is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_not_available",
            )
        hours = call.data[ATTR_HOURS]
        since: str | None = str(int(time.time() - hours * 3600))
        documents: list[str] = []
        points = 0
        for _ in range(GPX_MAX_PAGES):
            try:
                page = await api.async_download_gpx(since, limit=GPX_PAGE_POINTS)
            except MeatPiApiError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="api_command_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            if page is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="gpx_not_supported",
                )
            if page.unsupported_format:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="gpx_log_format",
                )
            documents.append(page.text)
            points += page.points
            if not page.more or not page.cursor or page.cursor == since:
                break
            since = page.cursor
        if points == 0:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="gpx_no_points",
                translation_placeholders={"hours": str(hours)},
            )
        device_tag = str(entry.data.get("device_id") or entry.entry_id)
        name = f"{device_tag}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}.gpx"
        path = hass.config.path("www", GPX_DIR, name)
        await hass.async_add_executor_job(_write_text, path, merge_gpx_documents(documents))
        _LOGGER.info("GPS track of %s saved: %s (%d points)", entry.title, path, points)
        return {"path": path, "url": f"/local/{GPX_DIR}/{name}", "points": points, "hours": hours}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_GPX,
        _async_save_gpx,
        schema=SAVE_GPX_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
