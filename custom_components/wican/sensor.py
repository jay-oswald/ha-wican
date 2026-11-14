"""Sensor platform for WiCAN integration."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import logging
from math import isfinite
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sensor.const import NON_NUMERIC_DEVICE_CLASSES
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .attributes import SENSOR_DESCRIPTIONS, MeatPiSensorEntityDescription, get_sensor_attributes
from .const import (
    DOMAIN,
    LAST_SEEN_KEY,
    MAX_DYNAMIC_PID_SENSORS,
    MAX_PID_CONFIG_FIELD_LENGTH,
    MAX_PID_KEY_LENGTH,
)
from .entity import MeatPiEntity
from .param_loader import (
    get_param_device_class,
    get_param_icon,
    get_param_unit,
    is_valid_class_unit_combo,
    is_valid_device_class,
)

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MeatPiConfigEntry
    from .devices import CatalogSensorDef

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


def _get_pid_unit(pid_key: str, config_unit: str | None = None) -> str | None:
    """Determine the appropriate unit for a PID sensor.

    Priority order:
    1. Config unit from device (if valid/non-empty)
    2. Fallback from params.json lookup

    This ensures consistent units even if device sometimes sends None.

    Args:
        pid_key: The PID sensor key/name from the device.
        config_unit: Unit from device config (may be None/empty/"none").

    Returns:
        Unit string (e.g., "km/h", "°C") or None if no match.
    """
    # Only a sanely sized string can be a unit; a glitching device may send
    # nested objects or huge blobs here, which would break entity creation.
    if not isinstance(config_unit, str) or len(config_unit) > MAX_PID_CONFIG_FIELD_LENGTH:
        config_unit = None

    # Normalize "none" and empty string to actual None
    if config_unit in ("none", ""):
        config_unit = None

    # If device provided a valid unit, use it
    if config_unit is not None:
        return config_unit

    # Fallback to params.json lookup
    return get_param_unit(pid_key)


def _get_pid_icon(
    pid_key: str,
    device_class: SensorDeviceClass | None,
) -> str:
    """Determine the appropriate icon for a PID sensor.

    Args:
        pid_key: The PID sensor key/name from the device.
        device_class: The resolved SensorDeviceClass, if any.

    Returns:
        MDI icon string (e.g., "mdi:engine").
    """
    device_class_str = None
    if device_class is not None:
        device_class_str = device_class.value if isinstance(device_class, SensorDeviceClass) else str(device_class)
    return get_param_icon(pid_key, device_class_str)


def _normalize_device_class(
    device_class: str | SensorDeviceClass | None,
    unit: str | None,
    pid_key: str | None = None,
) -> SensorDeviceClass | None:
    """Convert and validate device_class, filtering invalid combinations.

    Handles multiple validation scenarios:
    1. Invalid/unknown device class strings → None
    2. Mismatched class+unit combinations (e.g., speed+rpm) → None
    3. Fallback to params.json if no class provided

    Args:
        device_class: Device class from config (string or enum).
        unit: Unit of measurement for validation.
        pid_key: Optional PID key for fallback lookup.

    Returns:
        Valid SensorDeviceClass enum or None.
    """
    # Handle "none" string as None
    if isinstance(device_class, str) and device_class.lower() == "none":
        device_class = None

    # Anything other than a string/enum class is device garbage.
    if device_class is not None and not isinstance(device_class, (str, SensorDeviceClass)):
        _LOGGER.debug(
            "Ignoring non-string device class %r for %s",
            device_class, pid_key or "sensor",
        )
        device_class = None

    # Try to get fallback from params.json if no class provided
    if device_class is None and pid_key:
        device_class = get_param_device_class(pid_key)

    # Validate the device class is known to Home Assistant
    if isinstance(device_class, str):
        if not is_valid_device_class(device_class):
            _LOGGER.debug(
                "Invalid device class '%s' for %s, ignoring",
                device_class, pid_key or "sensor",
            )
            return None
        try:
            device_class = SensorDeviceClass(device_class)
        except ValueError:
            _LOGGER.debug(
                "Unknown SensorDeviceClass '%s' for %s, ignoring",
                device_class, pid_key or "sensor",
            )
            return None

    # A WiCAN device only ever reports JSON scalars, so device classes whose
    # state must be a datetime/date/enum can never be satisfied and would make
    # Home Assistant reject every state write for the sensor.
    if device_class in NON_NUMERIC_DEVICE_CLASSES:
        _LOGGER.debug(
            "Dropping unsupported device class '%s' for %s",
            device_class, pid_key or "sensor",
        )
        return None

    # Validate class+unit combination
    if device_class is not None and unit is not None:
        dc_str = device_class.value if isinstance(device_class, SensorDeviceClass) else str(device_class)
        if not is_valid_class_unit_combo(dc_str, unit):
            _LOGGER.debug(
                "Invalid device_class+unit combo: %s + %s for %s, dropping device_class",
                dc_str, unit, pid_key or "sensor",
            )
            return None

    return device_class


# High-cardinality / verbose PID keys that should be created disabled by default
# so they don't flood the entity registry. Users can enable individually.
# Examples: per-cell HV battery voltages (HV_C_V_001 .. HV_C_V_188) and any
# other explicitly per-cell metrics.
_HIGH_VOLUME_PID_PATTERN = re.compile(
    r"^HV_C_(V|D)_\d+$",  # per-cell voltage / deterioration
)


def _pid_enabled_by_default(pid_key: str) -> bool:
    """Return False for high-volume PID sensors so they default to disabled."""
    return _HIGH_VOLUME_PID_PATTERN.match(pid_key) is None


def _is_usable_pid_key(pid_key: Any) -> bool:
    """Return True when a device-reported PID key can back a real entity."""
    return (
        isinstance(pid_key, str)
        and bool(pid_key.strip())
        and len(pid_key) <= MAX_PID_KEY_LENGTH
    )


def _trimmed_pid_config(config: Any) -> dict[str, str]:
    """Reduce a device-reported per-PID config to the fields we persist.

    Only ``unit`` and ``class`` are ever read back, and the config entry is
    written to disk, so cap what a misbehaving device can store there.
    """
    if not isinstance(config, dict):
        return {}
    trimmed: dict[str, str] = {}
    for config_field in ("unit", "class"):
        value = config.get(config_field)
        if isinstance(value, str) and value and len(value) <= MAX_PID_CONFIG_FIELD_LENGTH:
            trimmed[config_field] = value
    return trimmed


def _build_pid_entity_description(
    pid_key: str,
    config: Any,
) -> MeatPiSensorEntityDescription:
    """Build the entity description for a PID sensor from device config."""
    if not isinstance(config, dict):
        config = {}
    # Use _get_pid_unit with config unit for consistent fallback handling
    unit = _get_pid_unit(pid_key, config.get("unit"))
    device_class = _normalize_device_class(config.get("class"), unit, pid_key)
    icon = _get_pid_icon(pid_key, device_class)

    _LOGGER.debug(
        "PID sensor %s described with unit=%s, device_class=%s, icon=%s",
        pid_key, unit, device_class, icon,
    )

    return MeatPiSensorEntityDescription(
        key=pid_key,
        name=pid_key,
        device_class=device_class,
        native_unit_of_measurement=unit,
        icon=icon,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=_pid_enabled_by_default(pid_key),
    )


def _coerce_numeric_value(value: Any) -> int | float | None:
    """Coerce a normalized value into a finite number, or None.

    Mirrors the conversion Home Assistant's sensor platform applies to the
    state of a sensor that declares a device class, state class, or unit —
    where a non-numeric or non-finite value raises and breaks the state
    write — so we can drop such values gracefully instead.
    """
    if isinstance(value, bool):
        # bool is an int subclass but is never a legitimate measurement.
        return None
    if isinstance(value, (int, float)):
        return value if isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, str):
        try:
            numeric: int | float = (
                int(value)
                if "." not in value and "e" not in value.lower()
                else float(value)
            )
        except ValueError:
            return None
        return numeric if isfinite(numeric) else None
    return None


DYNAMIC_PID_SENSORS: dict[str, dict[str, MeatPiPidSensorEntity]] = {}

# Entries that have already logged the dynamic-PID cap warning.
_PID_CAP_WARNED: set[str] = set()


def _warn_pid_cap_once(entry_id: str) -> None:
    """Warn (once per entry) that the dynamic PID limit was reached."""
    if entry_id in _PID_CAP_WARNED:
        return
    _PID_CAP_WARNED.add(entry_id)
    _LOGGER.warning(
        "Device reported more than %d distinct PIDs; ignoring new ones. "
        "A healthy vehicle never has this many - check the device configuration",
        MAX_DYNAMIC_PID_SENSORS,
    )


def _discover_new_pid_entities(
    config_entry: MeatPiConfigEntry,
    sensors: dict[str, MeatPiPidSensorEntity],
    pid_data: dict[str, Any],
    pid_config: dict[str, Any],
) -> list[MeatPiPidSensorEntity]:
    """Create entities for PID keys not seen before, respecting the cap."""
    new_entities: list[MeatPiPidSensorEntity] = []
    for pid_key in pid_data:
        if pid_key in sensors:
            continue
        if not _is_usable_pid_key(pid_key):
            _LOGGER.debug("Ignoring unusable PID key from device: %r", pid_key)
            continue
        # sensors already includes the entities created this round.
        if len(sensors) >= MAX_DYNAMIC_PID_SENSORS:
            _warn_pid_cap_once(config_entry.entry_id)
            break
        entity_description = _build_pid_entity_description(
            pid_key, pid_config.get(pid_key, {}),
        )
        entity = MeatPiPidSensorEntity(config_entry, pid_key, entity_description)
        new_entities.append(entity)
        sensors[pid_key] = entity
    return new_entities


def _persist_pid_entities(
    hass: HomeAssistant,
    config_entry: MeatPiConfigEntry,
    sensors: dict[str, MeatPiPidSensorEntity],
    pid_data: dict[str, Any],
    pid_config: dict[str, Any],
) -> None:
    """Persist the known PID keys and their trimmed config in the entry."""
    stored_config = config_entry.data.get("config", {})
    existing_config = dict(stored_config) if isinstance(stored_config, dict) else {}
    for pid_key in pid_data:
        # Refresh stored config for every known PID (a changed unit or class
        # takes effect after reload), but only for keys that actually back an
        # entity so junk keys never reach storage.
        if pid_key in sensors and pid_key in pid_config:
            existing_config[pid_key] = _trimmed_pid_config(pid_config[pid_key])
    new_data = dict(config_entry.data)
    new_data["pid_keys"] = list(sensors.keys())
    new_data["config"] = existing_config
    hass.config_entries.async_update_entry(config_entry, data=new_data)


def _build_catalog_sensor_description(
    definition: CatalogSensorDef,
) -> MeatPiSensorEntityDescription:
    """Build an entity description from a catalog sensor definition.

    Catalog fields are data from a remote document: the device class and
    unit go through the same validation the dynamic PID sensors use, so a
    bad catalog entry degrades to a plain sensor instead of a broken one.
    """
    unit = definition.unit
    if unit is not None and len(unit) > MAX_PID_CONFIG_FIELD_LENGTH:
        unit = None
    device_class = _normalize_device_class(definition.device_class, unit)
    return MeatPiSensorEntityDescription(
        key=definition.key,
        name=definition.name,
        device_class=device_class,
        native_unit_of_measurement=unit,
        icon=definition.icon,
        entity_category=(
            EntityCategory.DIAGNOSTIC if definition.diagnostic else None
        ),
    )


async def async_setup_entry(  # noqa: C901
    hass: HomeAssistant,
    config_entry: MeatPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""

    async_add_entities(
        [
            MeatPiSensorEntity(config_entry, description)
            for description in SENSOR_DESCRIPTIONS
        ]
        + [MeatPiLastSeenSensorEntity(config_entry)],
    )

    # Product-specific sensors declared by the device catalog (keys into
    # the pushed "status" object, like the static descriptions above).
    catalog_sensors = config_entry.runtime_data.device_profile.extra_sensors
    if catalog_sensors:
        async_add_entities(
            MeatPiSensorEntity(
                config_entry, _build_catalog_sensor_description(definition),
            )
            for definition in catalog_sensors
        )

    DYNAMIC_PID_SENSORS[config_entry.entry_id] = {}

    def _cleanup_dynamic_state() -> None:
        DYNAMIC_PID_SENSORS.pop(config_entry.entry_id, None)
        _PID_CAP_WARNED.discard(config_entry.entry_id)

    config_entry.async_on_unload(_cleanup_dynamic_state)

    # Restore PID sensors from config entry
    pid_keys = config_entry.data.get("pid_keys", [])
    if not isinstance(pid_keys, list):
        _LOGGER.warning("Stored pid_keys is not a list; skipping PID restore")
        pid_keys = []
    pid_config = config_entry.data.get("config", {})
    if not isinstance(pid_config, dict):
        pid_config = {}
    restored_entities: list[MeatPiPidSensorEntity] = []
    for pid_key in pid_keys:
        if not _is_usable_pid_key(pid_key):
            _LOGGER.debug("Skipping unusable stored PID key: %r", pid_key)
            continue
        entity_description = _build_pid_entity_description(
            pid_key, pid_config.get(pid_key, {}),
        )
        entity = MeatPiPidSensorEntity(config_entry, pid_key, entity_description)
        DYNAMIC_PID_SENSORS[config_entry.entry_id][pid_key] = entity
        restored_entities.append(entity)
    if restored_entities:
        async_add_entities(restored_entities)

    async def _async_process_pid_update(data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        pid_data = data.get("autopid_data", {})
        if not isinstance(pid_data, dict) or not pid_data:
            return

        # The platform may have been unloaded between dispatch and this task.
        sensors = DYNAMIC_PID_SENSORS.get(config_entry.entry_id)
        if sensors is None:
            return

        pid_config = data.get("config", {})
        if not isinstance(pid_config, dict):
            pid_config = {}

        new_entities = _discover_new_pid_entities(
            config_entry, sensors, pid_data, pid_config,
        )
        if new_entities:
            async_add_entities(new_entities)
        # Persist on every PID push, not only when a new PID appeared: a
        # corrected unit/class on an already-known PID must survive a
        # restart too. async_update_entry is a no-op when nothing changed.
        _persist_pid_entities(hass, config_entry, sensors, pid_data, pid_config)

    def handle_pid_update(webhook_id: str, data: dict[str, Any]) -> None:
        # IMPORTANT: multiple WiCAN entries share the same dispatcher signal.
        # Filter by this entry's webhook_id to avoid cross-device entity creation.
        if webhook_id != config_entry.runtime_data.webhook_id:
            return
        hass.loop.call_soon_threadsafe(
            hass.async_create_task,
            _async_process_pid_update(data),
        )

    # Connect the dispatcher signal to handle_pid_update
    unsub = async_dispatcher_connect(hass, DOMAIN, handle_pid_update)
    config_entry.async_on_unload(unsub)


class MeatPiSensorEntity(MeatPiEntity, RestoreSensor):
    """A sensor entity."""

    __slots__ = ("_attr_extra_state_attributes", "_attr_native_value", "_dropped_value_reported")

    entity_description: MeatPiSensorEntityDescription
    _keeps_last_value = True

    def __init__(
        self,
        config_entry: MeatPiConfigEntry,
        entity_description: MeatPiSensorEntityDescription,
    ) -> None:
        super().__init__(config_entry, entity_description)
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self._dropped_value_reported = False

    def _usable_value(self, key: str, raw_value: Any) -> Any:
        """Normalize a device value into one the state machine will accept.

        When the sensor declares a device class or unit, Home Assistant
        rejects (raises on) non-numeric and non-finite states; drop such
        values to ``None`` instead so one bad reading can never wedge the
        entity or the update pipeline.
        """
        value = self.coordinator.normalize_sensor_value(key, raw_value)
        if value is None or not self._numeric_state_expected:
            return value
        numeric = _coerce_numeric_value(value)
        if numeric is None:
            if not self._dropped_value_reported:
                self._dropped_value_reported = True
                _LOGGER.warning(
                    "Dropping non-numeric value %r for %s (numeric state expected); "
                    "further drops for this sensor will not be logged",
                    raw_value,
                    key,
                )
        else:
            self._dropped_value_reported = False
        return numeric

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        key = self.entity_description.key
        data = self.coordinator.data or {}
        status = data.get("status", {})
        if not isinstance(status, dict) or key not in status:
            # Partial pushes are normal: keep the last value. But the
            # state must still be written, or an availability flip (the
            # staleness health-check failing or recovering, the
            # keep-last-values option changing) would never render and
            # this entity would keep showing its old value as available
            # while every sibling goes unavailable.
            self.async_write_ha_state()
            return

        # Get raw value and normalize it
        self._attr_native_value = self._usable_value(key, status[key])
        self._attr_extra_state_attributes = get_sensor_attributes(
            self.entity_description, self.coordinator.data,
        )

        # Write state to Home Assistant
        self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (backward compatibility).

        This method is kept for backward compatibility during migration.
        The coordinator pattern now handles updates via _handle_coordinator_update().
        """
        # Coordinator update will trigger _handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Restore entity state."""
        # Restore last known state
        state = await self.async_get_last_sensor_data()
        if state and state.native_value is not None:
            # Normalize restored value using coordinator logic
            self._attr_native_value = self._usable_value(
                self.entity_description.key, state.native_value,
            )

        await super().async_added_to_hass()


class MeatPiLastSeenSensorEntity(MeatPiEntity, RestoreSensor):
    """When the device last pushed data.

    Deliberately stays available while the device is stale: its purpose
    is telling the user how old the readings are (unavailable, or held
    at their last value with the keep-last-values option) while the car
    is parked or the device is asleep. The value survives restarts via
    restore, so "last seen yesterday" is not lost with HA downtime; the
    coordinator reads the same restored state at startup to know whether
    the device has gone quiet (async_restore_last_push).
    """

    def __init__(self, config_entry: MeatPiConfigEntry) -> None:
        super().__init__(
            config_entry,
            MeatPiSensorEntityDescription(
                key=LAST_SEEN_KEY,
                name="Last seen",
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )
        self._attr_native_value: datetime | None = None

    @property
    def available(self) -> bool:
        """Stay available through staleness — that is the entity's point."""
        return True

    def _handle_coordinator_update(self) -> None:
        """Track the coordinator's push timestamp."""
        last_push = self.coordinator.last_push
        if last_push is not None:
            self._attr_native_value = last_push
        # Written unconditionally so health-check notifications render too.
        self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (backward compatibility)."""

    async def async_added_to_hass(self) -> None:
        """Restore the last-seen timestamp across restarts."""
        state = await self.async_get_last_sensor_data()
        if state and state.native_value is not None:
            value: Any = state.native_value
            if isinstance(value, str):
                value = dt_util.parse_datetime(value)
            if isinstance(value, datetime):
                self._attr_native_value = value
        await super().async_added_to_hass()


class MeatPiPidSensorEntity(MeatPiEntity, RestoreSensor):
    """Dynamic PID sensor entity."""

    __slots__ = (
        "_attr_native_value",
        "_dropped_value_reported",
        "_numeric_seen",
        "_pid_key",
    )

    entity_description: MeatPiSensorEntityDescription
    _keeps_last_value = True

    def __init__(
        self,
        config_entry: MeatPiConfigEntry,
        pid_key: str,
        entity_description: MeatPiSensorEntityDescription,
    ) -> None:
        _LOGGER.debug("Creating MeatPiPidSensorEntity for PID: %s", pid_key)
        super().__init__(config_entry, entity_description)
        self._pid_key = pid_key
        self._attr_unique_id = f"{config_entry.entry_id}_pid_{pid_key}"
        self._attr_native_value = None  # Initialize to None
        self._numeric_seen = False
        self._dropped_value_reported = False

    @property
    def state_class(self) -> SensorStateClass | None:
        """Return MEASUREMENT once the PID has proven to be numeric.

        PID values are usually numbers (rpm, temperature) but can be text
        (gear position, VIN). Latching the state class on the first numeric
        value keeps long-term statistics for numeric PIDs while letting text
        PIDs work instead of failing every state write.
        """
        if self._numeric_seen:
            return SensorStateClass.MEASUREMENT
        return None

    def _usable_value(self, raw_value: Any) -> Any:
        """Normalize a PID value into one the state machine will accept."""
        value = self.coordinator.normalize_sensor_value(self._pid_key, raw_value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self._numeric_seen = True
            self._dropped_value_reported = False
            return value
        if value is None or not self._numeric_state_expected:
            return value
        numeric = _coerce_numeric_value(value)
        if numeric is None:
            if not self._dropped_value_reported:
                self._dropped_value_reported = True
                _LOGGER.warning(
                    "Dropping non-numeric value %r for PID %s (numeric state "
                    "expected); further drops for this sensor will not be logged",
                    raw_value,
                    self._pid_key,
                )
        else:
            self._numeric_seen = True
            self._dropped_value_reported = False
        return numeric

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data or {}
        pid_data = data.get("autopid_data", {})
        if isinstance(pid_data, dict) and self._pid_key in pid_data:
            self._attr_native_value = self._usable_value(pid_data[self._pid_key])
        # Always write, even when this PID is absent from the push (the
        # vehicle may have stopped reporting it): availability flips from
        # the staleness health-check or the keep-last-values option must
        # render on restored PID sensors too, not leave them frozen.
        self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (backward compatibility).

        Coordinator pattern now handles updates via _handle_coordinator_update().
        """

    async def async_added_to_hass(self) -> None:
        """Restore the last known value."""
        state = await self.async_get_last_sensor_data()
        if state:
            self._attr_native_value = self._usable_value(state.native_value)
        await super().async_added_to_hass()
