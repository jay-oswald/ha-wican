"""Binary sensor platform for WiCAN integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .attributes import BINARY_SENSOR_DESCRIPTIONS, WiCANBinarySensorEntityDescription, get_sensor_attributes
from .const import DOMAIN
from .entity import WiCANEntity
from .param_loader import (
    get_param_device_class,
    get_param_display_name,
    get_param_icon,
    is_binary_sensor,
)

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import WiCANConfigEntry

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0

TRUE_STRINGS = {"enable", "true", "online"}

# A PID marked as a binary sensor in params.json reports "on"/"off" strings,
# but firmware versions and vehicle profiles vary, so accept the usual
# spellings and plain numbers too. Anything else leaves the state untouched
# rather than guessing - bool() on a non-empty string would read "off" as on.
_PID_TRUE_VALUES = {"1", "active", "enable", "enabled", "on", "online", "true", "yes"}
_PID_FALSE_VALUES = {"0", "disable", "disabled", "false", "inactive", "no", "off", "offline"}

# Entities created dynamically from webhook data, keyed by config entry id.
DYNAMIC_PID_BINARY_SENSORS = {}


def is_true_status(value: str) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in TRUE_STRINGS
    return bool(value)


def pid_value_to_is_on(value: Any) -> bool | None:  # noqa: PLR0911
    """Interpret a PID value as a binary state.

    Args:
        value: Raw value for the PID from the webhook payload.

    Returns:
        True or False, or None when the value cannot be interpreted, in which
        case the caller should keep the previous state.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0

    text = str(value).strip().lower()
    if text in _PID_TRUE_VALUES:
        return True
    if text in _PID_FALSE_VALUES:
        return False

    try:
        return float(text) != 0
    except ValueError:
        _LOGGER.debug("Cannot interpret %r as a binary sensor state", value)
        return None


def _get_pid_device_class(
    pid_key: str,
    config_class: str | None = None,
) -> BinarySensorDeviceClass | None:
    """Resolve a binary sensor device class for a PID.

    Args:
        pid_key: The PID sensor key/name from the device.
        config_class: Device class from the device config, if any.

    Returns:
        Valid BinarySensorDeviceClass or None.
    """
    device_class = config_class
    if isinstance(device_class, str) and device_class.lower() in ("", "none"):
        device_class = None

    if device_class is None:
        device_class = get_param_device_class(pid_key)

    if not device_class:
        return None

    try:
        return BinarySensorDeviceClass(str(device_class).lower())
    except ValueError:
        _LOGGER.debug(
            "Invalid binary sensor device class '%s' for %s, ignoring",
            device_class, pid_key,
        )
        return None


def _build_pid_binary_sensor(
    config_entry: WiCANConfigEntry,
    pid_key: str,
    config: dict[str, Any],
) -> WiCANPidBinarySensorEntity:
    """Build a dynamic PID binary sensor entity."""
    device_class = _get_pid_device_class(pid_key, config.get("class"))
    icon = get_param_icon(pid_key, device_class.value if device_class else None)

    _LOGGER.debug(
        "Creating PID binary sensor %s with device_class=%s, icon=%s",
        pid_key, device_class, icon,
    )

    entity_description = WiCANBinarySensorEntityDescription(
        key=pid_key,
        name=get_param_display_name(pid_key),
        device_class=device_class,
        icon=icon,
    )
    return WiCANPidBinarySensorEntity(config_entry, pid_key, entity_description)


async def async_setup_entry(  # noqa: C901
    hass: HomeAssistant,
    config_entry: WiCANConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""

    async_add_entities(
        WiCANBinarySensorEntity(config_entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )

    DYNAMIC_PID_BINARY_SENSORS[config_entry.entry_id] = {}

    @callback
    def _async_forget_entry() -> None:
        """Drop this entry's entity map so removed entries leave nothing behind."""
        DYNAMIC_PID_BINARY_SENSORS.pop(config_entry.entry_id, None)

    config_entry.async_on_unload(_async_forget_entry)

    # Restore PID binary sensors from the config entry
    stored_config = config_entry.data.get("config", {})
    restored_entities = []
    for pid_key in config_entry.data.get("pid_keys", []):
        if not is_binary_sensor(pid_key):
            # Owned by the sensor platform.
            continue
        entity = _build_pid_binary_sensor(
            config_entry, pid_key, stored_config.get(pid_key, {}),
        )
        DYNAMIC_PID_BINARY_SENSORS[config_entry.entry_id][pid_key] = entity
        restored_entities.append(entity)

    if restored_entities:
        async_add_entities(restored_entities)

    async def _async_process_pid_update(data):
        pid_data = data.get("autopid_data", {})
        if not pid_data:
            return

        # A webhook can already be queued when the entry unloads, so the map
        # may be gone by the time this task runs.
        sensors = DYNAMIC_PID_BINARY_SENSORS.get(config_entry.entry_id)
        if sensors is None:
            return

        webhook_config = data.get("config", {})
        new_entities = []
        for pid_key in pid_data:
            if pid_key in sensors or not is_binary_sensor(pid_key):
                continue
            entity = _build_pid_binary_sensor(
                config_entry, pid_key, webhook_config.get(pid_key, {}),
            )
            sensors[pid_key] = entity
            new_entities.append(entity)

        # pid_keys and config are persisted by the sensor platform, which sees
        # the same payload and stores every PID regardless of which platform
        # owns it.
        if new_entities:
            async_add_entities(new_entities)

    def handle_pid_update(webhook_id, data):
        # IMPORTANT: multiple WiCAN entries share the same dispatcher signal.
        # Filter by this entry's webhook_id to avoid cross-device entity creation.
        if webhook_id != config_entry.runtime_data.webhook_id:
            return
        hass.loop.call_soon_threadsafe(
            hass.async_create_task,
            _async_process_pid_update(data),
        )

    unsub = async_dispatcher_connect(hass, DOMAIN, handle_pid_update)
    config_entry.async_on_unload(unsub)


class WiCANBinarySensorEntity(WiCANEntity, BinarySensorEntity, RestoreEntity):
    """A binary sensor entity."""

    __slots__ = ("_attr_extra_state_attributes", "_attr_is_on")

    entity_description: WiCANBinarySensorEntityDescription

    def __init__(self, config_entry, entity_description):
        super().__init__(config_entry, entity_description)
        self._attr_unique_id = f"{config_entry.entry_id}_{entity_description.key}"
        self._attr_is_on = None
        self._attr_extra_state_attributes = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        key = self.entity_description.key
        status = self.coordinator.data.get("status", {})

        # If key not present, don't change state. Availability handled below.
        if key in status:
            self._attr_is_on = is_true_status(status[key])
            self._attr_extra_state_attributes = get_sensor_attributes(key, self.coordinator.data)

        # Availability: if we have a status dict, entity is available; if device stopped pushing,
        # HA will keep last state, but we still emit state writes on updates to ensure logbook records.
        # Write state to Home Assistant.
        self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data) -> None:
        """Handle webhook event (backward compatibility)."""
        # Coordinator update will trigger _handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Restore entity state."""
        # Restore last known state so logbook has a baseline before first push
        last_state = await self.async_get_last_state()
        if last_state is not None and self._attr_is_on is None:
            self._attr_is_on = last_state.state == "on"
        await super().async_added_to_hass()


class WiCANPidBinarySensorEntity(WiCANEntity, BinarySensorEntity, RestoreEntity):
    """Dynamic PID binary sensor entity.

    Vehicle profiles mark parameters such as CHARGING, CHARGER_CONNECTED and
    PARK_BRAKE as binary in params.json. They used to be created as regular
    sensors holding the strings "on" and "off", which cannot drive a state
    condition, a device trigger, or a plug/charging device class.
    """

    __slots__ = ("_attr_is_on", "_pid_key")

    entity_description: WiCANBinarySensorEntityDescription

    def __init__(self, config_entry, pid_key, entity_description):
        super().__init__(config_entry, entity_description)
        self._pid_key = pid_key
        self._attr_unique_id = f"{config_entry.entry_id}_pid_{pid_key}"
        self._attr_is_on = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        pid_data = self.coordinator.data.get("autopid_data", {})

        if self._pid_key in pid_data:
            is_on = pid_value_to_is_on(pid_data[self._pid_key])
            if is_on is not None:
                self._attr_is_on = is_on
                self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data) -> None:
        """Handle webhook event (backward compatibility)."""
        # Coordinator update will trigger _handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Restore entity state."""
        last_state = await self.async_get_last_state()
        if (
            last_state is not None
            and self._attr_is_on is None
            and last_state.state in (STATE_ON, STATE_OFF)
        ):
            self._attr_is_on = last_state.state == STATE_ON
        await super().async_added_to_hass()
