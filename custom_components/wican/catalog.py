"""Remote device catalog: declarative MeatPi device-type definitions.

New MeatPi products get first-class support (real model name, hardware
keywords, product sensors, firmware-update coordinates) without an
integration release: the catalog is a JSON document published on GitHub
(same pattern as params.json), fetched at most once per day, persisted in
Home Assistant's .storage, with a bundled copy as the offline fallback.

Everything in a catalog is *data* validated against strict bounds — it
feeds existing, hardened code paths and can never ship behavior. A
corrupt, hostile, or unreachable catalog degrades to the previous good
copy (or the bundled one), never to a broken integration. Reserved slugs
(the WiCAN family and the generic fallback) can never be redefined.

Design record: notes/MEATPI_INTEGRATION_PLAN.md (phase 9).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http import HTTPStatus
import json
import logging
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import (
    CATALOG_FETCH_TIMEOUT,
    CATALOG_MAX_BYTES,
    CATALOG_MAX_DEVICE_TYPES,
    CATALOG_MAX_FIELD_LENGTH,
    CATALOG_MAX_KEYWORD_LENGTH,
    CATALOG_MAX_KEYWORDS,
    CATALOG_MAX_MODEL_LENGTH,
    CATALOG_MAX_RECORDER_SCHEMAS,
    CATALOG_MAX_SENSORS,
    CATALOG_REFRESH_INTERVAL,
    CATALOG_SCHEMA_VERSION,
    CATALOG_STORAGE_KEY,
    CATALOG_STORAGE_VERSION,
    DEVICE_CATALOG_URL,
    DOMAIN,
    MANUFACTURER,
)
from .devices import CatalogSensorDef, MeatPiDeviceProfile, apply_catalog
from .helpers import async_read_capped

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_HASS_DATA_KEY = f"{DOMAIN}_device_catalog"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FIRMWARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STATUS_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _bounded_str(value: Any, max_length: int) -> str | None:
    """Return a stripped, bounded, non-empty string, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > max_length:
        return None
    return stripped


def _parse_keywords(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    keywords: list[str] = []
    for item in raw:
        if len(keywords) >= CATALOG_MAX_KEYWORDS:
            break
        keyword = _bounded_str(item, CATALOG_MAX_KEYWORD_LENGTH)
        if keyword and (lowered := keyword.lower()) not in keywords:
            keywords.append(lowered)
    return tuple(keywords)


def _parse_sensor(raw: Any) -> CatalogSensorDef | None:
    """Validate one catalog sensor definition, or None."""
    if not isinstance(raw, dict):
        return None
    key = _bounded_str(raw.get("key"), CATALOG_MAX_FIELD_LENGTH)
    if key is None or not _STATUS_KEY_RE.match(key):
        return None
    name = _bounded_str(raw.get("name"), CATALOG_MAX_FIELD_LENGTH) or key
    device_class = _bounded_str(raw.get("device_class"), CATALOG_MAX_FIELD_LENGTH)
    unit = _bounded_str(raw.get("unit"), CATALOG_MAX_KEYWORD_LENGTH)
    icon = _bounded_str(raw.get("icon"), CATALOG_MAX_FIELD_LENGTH)
    if icon is not None and not icon.startswith("mdi:"):
        icon = None
    diagnostic = raw.get("diagnostic", True)
    if not isinstance(diagnostic, bool):
        diagnostic = True
    return CatalogSensorDef(
        key=key,
        name=name,
        device_class=device_class,
        unit=unit,
        diagnostic=diagnostic,
        icon=icon,
    )


def _parse_device_type(slug: str, raw: Any) -> MeatPiDeviceProfile | None:
    """Validate one catalog device-type entry into a profile, or None."""
    if not _SLUG_RE.match(slug) or not isinstance(raw, dict):
        return None
    model = _bounded_str(raw.get("model"), CATALOG_MAX_MODEL_LENGTH)
    if model is None:
        return None

    firmware = raw.get("firmware")
    firmware_repo: str | None = None
    firmware_asset_pattern: str | None = None
    if isinstance(firmware, dict):
        repo = _bounded_str(firmware.get("repo"), CATALOG_MAX_FIELD_LENGTH)
        if repo and _FIRMWARE_REPO_RE.match(repo):
            firmware_repo = repo
        firmware_asset_pattern = _bounded_str(
            firmware.get("asset_pattern"), CATALOG_MAX_FIELD_LENGTH,
        )

    sensors: list[CatalogSensorDef] = []
    raw_sensors = raw.get("sensors")
    if isinstance(raw_sensors, list):
        seen: set[str] = set()
        for item in raw_sensors[:CATALOG_MAX_SENSORS]:
            sensor = _parse_sensor(item)
            if sensor is None or sensor.key in seen:
                continue
            seen.add(sensor.key)
            sensors.append(sensor)

    return MeatPiDeviceProfile(
        device_type=slug,
        model=model,
        manufacturer=MANUFACTURER,
        firmware_repo=firmware_repo,
        firmware_asset_pattern=firmware_asset_pattern,
        hw_version_keywords=_parse_keywords(raw.get("hw_keywords")),
        supports_obd_pids=bool(raw.get("supports_obd_pids", False)),
        supports_gps=bool(raw.get("supports_gps", False)),
        extra_sensors=tuple(sensors),
    )


def parse_catalog(raw: Any) -> dict[str, MeatPiDeviceProfile] | None:
    """Validate a raw catalog document into profiles.

    Returns None when the document is structurally unusable (wrong shape
    or schema) so callers keep the previous good catalog. Individually
    invalid entries are dropped, never fatal.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != CATALOG_SCHEMA_VERSION:
        _LOGGER.warning(
            "Device catalog has unsupported schema %r (expected %d); ignoring",
            raw.get("schema"),
            CATALOG_SCHEMA_VERSION,
        )
        return None
    device_types = raw.get("device_types")
    if not isinstance(device_types, dict):
        return None

    profiles: dict[str, MeatPiDeviceProfile] = {}
    dropped = 0
    for slug, entry in device_types.items():
        if len(profiles) >= CATALOG_MAX_DEVICE_TYPES:
            _LOGGER.warning(
                "Device catalog capped at %d device types", CATALOG_MAX_DEVICE_TYPES,
            )
            break
        if not isinstance(slug, str):
            dropped += 1
            continue
        profile = _parse_device_type(slug, entry)
        if profile is None:
            dropped += 1
            continue
        profiles[slug] = profile
    if dropped:
        _LOGGER.debug("Device catalog: dropped %d invalid entr(ies)", dropped)
    return profiles


@dataclass(frozen=True, slots=True)
class IntegrationPolicy:
    """What the catalog says about the integration itself (not a device).

    ``history_states``: the History fill writes into the recorder's own
    table and runs only on the recorder database versions it was tested
    with. A Home Assistant release with a new version needs a test, not an
    integration release: the catalog adds the version here once it passed,
    and can block one that turned out not to work.
    """

    history_states_schemas: frozenset[int] = frozenset()
    history_states_blocked: frozenset[int] = frozenset()


def _parse_schema_list(raw: Any) -> frozenset[int]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        item
        for item in raw[:CATALOG_MAX_RECORDER_SCHEMAS]
        if isinstance(item, int) and not isinstance(item, bool) and 0 < item < 100000
    )


def parse_integration_policy(raw: Any) -> IntegrationPolicy:
    """Read the ``integration`` section of a catalog document.

    Data like the rest of the catalog: anything that is not a list of
    version numbers counts as an empty list, never as an error.
    """
    section = raw.get("integration") if isinstance(raw, dict) else None
    states = section.get("history_states") if isinstance(section, dict) else None
    if not isinstance(states, dict):
        return IntegrationPolicy()
    return IntegrationPolicy(
        history_states_schemas=_parse_schema_list(states.get("recorder_schemas")),
        history_states_blocked=_parse_schema_list(
            states.get("blocked_recorder_schemas"),
        ),
    )


def _load_bundled_catalog() -> Any:
    """Read the bundled catalog file (executor job — blocking I/O)."""
    path = Path(__file__).parent / "data" / "device_catalog.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOGGER.exception("Failed to read the bundled device catalog")
        return None


async def _async_fetch_remote(hass: HomeAssistant) -> Any:
    """Fetch the catalog document from GitHub. Returns None on any failure."""
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(CATALOG_FETCH_TIMEOUT):
            response = await session.get(DEVICE_CATALOG_URL)
            if response.status != HTTPStatus.OK:
                _LOGGER.debug(
                    "Device catalog fetch failed: HTTP %s", response.status,
                )
                return None
            raw = await async_read_capped(response, CATALOG_MAX_BYTES)
            if raw is None:
                _LOGGER.warning("Device catalog response too large; ignoring")
                return None
            return json.loads(raw.decode("utf-8"))
    except Exception as err:  # Defensive: a fetch must never break setup.
        _LOGGER.debug("Device catalog fetch failed: %s", err)
        return None


class _CatalogManager:
    """Loads, applies, refreshes, and persists the device catalog."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, CATALOG_STORAGE_VERSION, CATALOG_STORAGE_KEY,
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._last_attempt = 0.0
        self.policy = IntegrationPolicy()

    async def async_ensure_loaded(self) -> None:
        """Apply the best available catalog; kick off a refresh if due."""
        async with self._lock:
            if not self._loaded:
                await self._async_load_local()
                self._loaded = True
        if self._refresh_due():
            self._last_attempt = time.monotonic()
            self._hass.async_create_task(self.async_refresh())

    async def _async_load_local(self) -> None:
        """Apply the stored catalog, falling back to the bundled copy."""
        stored = await self._store.async_load()
        raw = stored.get("catalog") if isinstance(stored, dict) else None
        profiles = parse_catalog(raw) if raw is not None else None
        source = "stored"
        if profiles is None:
            raw = await self._hass.async_add_executor_job(_load_bundled_catalog)
            profiles = parse_catalog(raw)
            source = "bundled"
        if profiles is None:
            _LOGGER.warning("No usable device catalog; built-in profiles only")
            return
        apply_catalog(profiles)
        self.policy = parse_integration_policy(raw)
        _LOGGER.debug(
            "Applied %s device catalog (%d device type(s))",
            source,
            len(profiles),
        )

    def _refresh_due(self) -> bool:
        if self._last_attempt:
            return (
                time.monotonic() - self._last_attempt >= CATALOG_REFRESH_INTERVAL
            )
        return True

    async def async_refresh(self) -> bool:
        """Fetch the remote catalog and apply/persist it if valid."""
        raw = await _async_fetch_remote(self._hass)
        if raw is None:
            return False
        profiles = parse_catalog(raw)
        if profiles is None:
            _LOGGER.warning("Fetched device catalog is invalid; keeping current")
            return False
        apply_catalog(profiles)
        self.policy = parse_integration_policy(raw)
        try:
            await self._store.async_save(
                {"catalog": raw, "fetched_at": time.time()},
            )
        except Exception as err:  # storage failure only loses the cache
            _LOGGER.warning("Could not persist device catalog: %s", err)
        _LOGGER.info(
            "Device catalog updated from remote (%d device type(s))",
            len(profiles),
        )
        return True


def _get_manager(hass: HomeAssistant) -> _CatalogManager:
    manager = hass.data.get(_HASS_DATA_KEY)
    if manager is None:
        manager = _CatalogManager(hass)
        hass.data[_HASS_DATA_KEY] = manager
    return cast("_CatalogManager", manager)


def integration_policy(hass: HomeAssistant) -> IntegrationPolicy:
    """The integration policy of the catalog in use (empty before it loads)."""
    manager = hass.data.get(_HASS_DATA_KEY)
    policy = getattr(manager, "policy", None)
    return policy if isinstance(policy, IntegrationPolicy) else IntegrationPolicy()


async def async_setup_catalog(hass: HomeAssistant) -> None:
    """Load and apply the device catalog (idempotent, never raises)."""
    try:
        await _get_manager(hass).async_ensure_loaded()
    except Exception:  # Defensive: the catalog must never break setup.
        _LOGGER.exception("Unexpected error setting up the device catalog")


async def async_refresh_device_catalog(hass: HomeAssistant) -> bool:
    """User-initiated catalog refresh (the Sync-definitions button).

    Returns True when a fetched catalog was applied. Never raises.
    """
    try:
        return await _get_manager(hass).async_refresh()
    except Exception:  # Defensive: a refresh must never break the caller.
        _LOGGER.exception("Unexpected error refreshing the device catalog")
        return False
