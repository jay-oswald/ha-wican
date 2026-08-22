"""Binary sensor platform for WiCAN integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .attributes import (
    BINARY_SENSOR_DESCRIPTIONS,
    REPORTING_BINARY_SENSOR_DESCRIPTION,
    WiCANBinarySensorEntityDescription,
    get_sensor_attributes,
)
from .const import REPORTING_MIN_TIMEOUT, REPORTING_STALE_MULTIPLIER
from .entity import WiCANEntity

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import WiCANConfigEntry

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0

TRUE_STRINGS = {"enable", "true", "online"}

def is_true_status(value: str) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in TRUE_STRINGS
    return bool(value)

async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: WiCANConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""

    async_add_entities(
        WiCANBinarySensorEntity(config_entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )

    stale_timeout = max(
        config_entry.runtime_data.post_interval * REPORTING_STALE_MULTIPLIER,
        REPORTING_MIN_TIMEOUT,
    )
    async_add_entities(
        [
            WiCANReportingBinarySensorEntity(
                config_entry, REPORTING_BINARY_SENSOR_DESCRIPTION, stale_timeout,
            ),
        ],
    )

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


class WiCANReportingBinarySensorEntity(WiCANEntity, BinarySensorEntity, RestoreEntity):
    """Whether the device has pushed data within its expected interval.

    WiCAN is push-only, and on many installs only reachable while parked at
    home on Wi-Fi (e.g. a WiCAN wired to a switched 12V pin). Once the
    vehicle leaves, every other sensor here just keeps its last reported
    value forever - CoordinatorEntity availability never reflects that,
    since the push-based coordinator's own fallback poll always succeeds
    with cached data (see WiCANDataUpdateCoordinator._async_update_data).
    This is the "is the rest of this device's data fresh" signal those
    sensors don't provide on their own.
    """

    __slots__ = ("_timeout", "_unsub_stale")

    def __init__(self, config_entry, entity_description, timeout: float) -> None:
        super().__init__(config_entry, entity_description)
        self._attr_unique_id = f"{config_entry.entry_id}_{entity_description.key}"
        self._attr_is_on = None
        self._timeout = timeout
        self._unsub_stale: CALLBACK_TYPE | None = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Recomputed from last_webhook_time rather than treating every call
        as "a push just arrived": some Home Assistant builds also invoke
        this once when the entity is (re)added, before any push has
        happened this session, which would otherwise mark it on regardless
        of how stale the device actually is.
        """
        self._refresh_from_last_webhook()

    def _refresh_from_last_webhook(self) -> None:
        last = self.coordinator.last_webhook_time
        if last is None:
            # Nothing received yet this session - leave any restored value
            # alone rather than fabricating a state from nothing.
            self.async_write_ha_state()
            return

        age = (dt_util.utcnow() - last).total_seconds()
        if age < self._timeout:
            self._attr_is_on = True
            self._arm_stale_timer(self._timeout - age)
        else:
            self._attr_is_on = False
        self.async_write_ha_state()

    def _arm_stale_timer(self, delay: float) -> None:
        if self._unsub_stale is not None:
            self._unsub_stale()
            self._unsub_stale = None
        if delay > 0:
            self._unsub_stale = async_call_later(
                self.hass, delay, self._handle_stale_timeout,
            )

    @callback
    def _handle_stale_timeout(self, _now) -> None:
        self._unsub_stale = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data) -> None:
        """Handle webhook event (backward compatibility)."""
        # Coordinator update will trigger _handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Restore entity state."""
        last_state = await self.async_get_last_state()
        if last_state is not None and self._attr_is_on is None:
            self._attr_is_on = last_state.state == "on"
        await super().async_added_to_hass()
        if self._unsub_stale is None:
            # Nothing armed a timer above (no push has arrived this
            # session) - a restored "on" needs one anyway, or it would
            # never go stale if the device never reports again.
            self._arm_stale_timer(self._timeout)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending stale-check timer."""
        if self._unsub_stale is not None:
            self._unsub_stale()
            self._unsub_stale = None
        await super().async_will_remove_from_hass()
