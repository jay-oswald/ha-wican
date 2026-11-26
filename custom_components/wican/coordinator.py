"""DataUpdateCoordinator for WiCAN integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from math import isfinite
from typing import TYPE_CHECKING, Any

from homeassistant.const import MAX_LENGTH_STATE_STATE, Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import async_get as async_get_restore_state
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_KEEP_LAST_VALUES,
    CONF_POST_INTERVAL,
    DEFAULT_KEEP_LAST_VALUES,
    DEFAULT_POST_INTERVAL,
    DEVICE_STALE_FACTOR,
    DOMAIN,
    HISTORY_GAPS_MAX,
    LAST_SEEN_KEY,
    MAX_COORDINATOR_KEYS,
    MIN_DEVICE_STALE_SECONDS,
    WICAN_DATA_UPDATE_INTERVAL,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.restore_state import StoredState

    from . import MeatPiConfigEntry

_LOGGER = logging.getLogger(__name__)

# WiCAN is push-based via webhooks, so we don't need frequent polling
# This is just for fallback/health check
UPDATE_INTERVAL = timedelta(seconds=WICAN_DATA_UPDATE_INTERVAL)


def _stored_state(hass: HomeAssistant, entity_id: str) -> StoredState | None:
    """Return the state Home Assistant kept for an entity across its restart.

    Home Assistant 2026.11 keys the stored states by entity registry id
    and reads them through ``async_get_stored_state``; before that they
    sat in ``last_states``, by entity id. Reading the old attribute on a
    new Home Assistant raised, which failed the first refresh and left a
    device that had gone quiet looking connected after a restart.
    """
    data = async_get_restore_state(hass)
    read = getattr(data, "async_get_stored_state", None)
    if read is not None:
        stored: StoredState | None = read(entity_id)
        return stored
    return data.last_states.get(entity_id)


class MeatPiDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching WiCAN data."""

    config_entry: MeatPiConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MeatPiConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry
        self._data: dict[str, Any] = {}
        self._last_push: datetime | None = None
        # The silences that ended since the catch-up last asked: (the last
        # push before, the push that ended it), epoch seconds. The time
        # between the two has no states of this device's sensors; the
        # catch-up writes the logged values of that time (history.py).
        self._data_gaps: list[tuple[float, float]] = []
        self._key_cap_warned = False
        self._keep_last_values = self._keep_last_values_option()

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=config_entry,
        )

    @property
    def last_push(self) -> datetime | None:
        """When the device last pushed data (None before the first push)."""
        return self._last_push

    def take_data_gaps(self) -> list[tuple[float, float]]:
        """Hand over the silences that ended since the last call."""
        gaps, self._data_gaps = self._data_gaps, []
        return gaps

    @property
    def device_online(self) -> bool | None:
        """Whether the device is pushing data.

        None until the device has been heard from at all (a fresh install
        waiting for its first push), then True while pushes keep arriving
        and False once the staleness health-check has failed.
        """
        if self._last_push is None:
            return None
        return self.last_update_success

    @property
    def keep_last_values(self) -> bool:
        """Whether reading entities keep their last value while the device is away."""
        return self._keep_last_values

    def _keep_last_values_option(self) -> bool:
        """Read the keep-last-values option from the config entry."""
        return bool(
            self.config_entry.options.get(
                CONF_KEEP_LAST_VALUES, DEFAULT_KEEP_LAST_VALUES,
            ),
        )

    @callback
    def async_options_updated(self) -> None:
        """Apply a changed keep-last-values option right away.

        Entities only render on coordinator notifications, so a device that
        is away would keep showing the old choice until its next push.
        Notify them when (and only when) the option actually changed.
        """
        keep_last_values = self._keep_last_values_option()
        if keep_last_values == self._keep_last_values:
            return
        self._keep_last_values = keep_last_values
        self.async_update_listeners()

    @callback
    def async_restore_last_push(self) -> None:
        """Pick up the last push time from before a restart or reload.

        Home Assistant keeps the Last seen sensor's state across restarts.
        Reading it before the first health-check, and before any entity is
        added, lets a device that went quiet before or during the downtime
        start out stale. Without it the device would look connected, and
        its restored readings current, until its next push.
        """
        if self._last_push is not None:
            return
        entity_id = er.async_get(self.hass).async_get_entity_id(
            Platform.SENSOR,
            DOMAIN,
            f"{self.config_entry.entry_id}_{LAST_SEEN_KEY}",
        )
        if entity_id is None:
            return
        stored = _stored_state(self.hass, entity_id)
        if stored is None:
            return
        try:
            restored = dt_util.parse_datetime(stored.state.state)
        except ValueError:
            # Shaped like a timestamp but not a real date: nothing to adopt.
            return
        if restored is None:
            return
        self._last_push = dt_util.as_utc(restored)

    async def _async_update_data(self) -> dict[str, Any]:
        """Health-check the push-based device.

        This is a push-based integration, so we don't actively poll. This
        periodic callback exists to detect when the device has stopped pushing:
        if no webhook data has arrived within the staleness window, raise
        UpdateFailed so entities become unavailable (reading entities hold
        their last value instead when the keep-last-values option is on).
        The transition to unavailable and the recovery are logged once each,
        at info level (log-when-unavailable): a device that stops pushing is
        usually a parked car whose adapter went to sleep, not an error.
        """
        if self._last_push is None:
            # No push received yet; stay available while waiting for the device.
            return self._data

        threshold = self.staleness_threshold()
        elapsed = (dt_util.utcnow() - self._last_push).total_seconds()
        if elapsed > threshold:
            if self.last_update_success:
                _LOGGER.info(
                    "%s has not pushed for %.0fs (threshold %ss); it is "
                    "asleep or away",
                    self.config_entry.title,
                    elapsed,
                    threshold,
                )
            raise UpdateFailed(
                f"No data received from the device in {elapsed:.0f}s "
                f"(threshold {threshold}s)",
            )

        return self._data

    async def _async_refresh(
        self,
        log_failures: bool = True,  # noqa: ARG002
        raise_on_auth_failed: bool = False,
        scheduled: bool = False,
        raise_on_entry_error: bool = False,
    ) -> None:
        """Refresh without the base class's error line for a quiet device.

        The only failure of this health-check is a device that stopped
        pushing, which _async_update_data logs at info level itself. An
        unexpected exception is still logged by the base class whatever
        ``log_failures`` says.
        """
        await super()._async_refresh(
            False,
            raise_on_auth_failed,
            scheduled,
            raise_on_entry_error,
        )

    def staleness_threshold(self) -> float:
        """Return the seconds without a push after which the device is stale."""
        post_interval = self.config_entry.options.get(
            CONF_POST_INTERVAL, DEFAULT_POST_INTERVAL,
        )
        return float(
            max(post_interval * DEVICE_STALE_FACTOR, MIN_DEVICE_STALE_SECONDS),
        )

    @callback
    def async_update_listeners(self) -> None:
        """Update all registered listeners, isolating failures.

        The base implementation stops at the first listener that raises, so a
        single entity failing to write its state (for example because the
        device pushed a value Home Assistant's state machine rejects) would
        silently abort updates for every entity registered after it. Contain
        each listener so one bad value can never take down the rest.
        """
        for update_callback, _ in list(self._listeners.values()):
            try:
                update_callback()
            except Exception:
                _LOGGER.exception(
                    "Error while updating a WiCAN entity listener; "
                    "continuing with remaining entities",
                )

    async def async_config_entry_first_refresh(self) -> None:
        """Perform first refresh of the coordinator.

        For WiCAN, this is a push-based integration, so we don't poll for data.
        This method initializes the coordinator with empty data and succeeds immediately.
        Entities will be created and will update when the first webhook push arrives.
        """
        _LOGGER.debug(
            "First refresh for WiCAN coordinator (push-based, no polling required)",
        )
        # A device heard from before the restart may have gone quiet since:
        # know that before the health-check below runs.
        self.async_restore_last_push()
        # Initialize with empty data - webhook pushes will populate it
        await self.async_refresh()

    def handle_webhook_data(self, data: dict[str, Any]) -> bool:
        """Handle incoming webhook data.

        This is called by the webhook handler when new data arrives.
        It updates the coordinator's data and notifies all listeners.

        Returns True when this push ended a data gap (the device had been
        silent past the staleness threshold) — the trigger for the SD-card
        history backfill.
        """
        # Defensive: ignore malformed (non-object) payloads instead of raising,
        # so a misbehaving device cannot break state updates for others.
        if not isinstance(data, dict):
            _LOGGER.warning(
                "Ignoring non-object device webhook data (%s)", type(data).__name__,
            )
            return False

        # Validate device identity before processing data
        self._validate_device_identity(data)

        # Detect a resumed-after-gap push BEFORE stamping the new push time.
        resumed_after_gap = False
        if self._last_push is not None:
            now = dt_util.utcnow()
            elapsed = (now - self._last_push).total_seconds()
            resumed_after_gap = elapsed > self.staleness_threshold()
            if resumed_after_gap:
                self._data_gaps.append(
                    (self._last_push.timestamp(), now.timestamp()),
                )
                del self._data_gaps[:-HISTORY_GAPS_MAX]

        # Update internal data store, bounded: known keys always refresh,
        # but a buggy firmware emitting ever-new top-level keys cannot grow
        # the dict without bound (dynamic PID *sensors* are already capped;
        # this bounds the raw data they are fed from).
        for key, value in data.items():
            if key in self._data or len(self._data) < MAX_COORDINATOR_KEYS:
                self._data[key] = value
            elif not self._key_cap_warned:
                self._key_cap_warned = True
                _LOGGER.warning(
                    "Ignoring new payload key %r: more than %d distinct "
                    "top-level keys received from this device",
                    key,
                    MAX_COORDINATOR_KEYS,
                )

        # Record the push time so the health-check can detect a stale device.
        self._last_push = dt_util.utcnow()
        if not self.last_update_success:
            _LOGGER.info("%s is pushing again", self.config_entry.title)

        # Notify all entities that data has been updated (also marks the
        # coordinator successful again, recovering from any stale state).
        # Contained: a failure while notifying entities must not abort the
        # webhook handler, which still has device-info persistence and
        # PID-discovery work to do after this call.
        try:
            self.async_set_updated_data(self._data)
        except Exception:
            _LOGGER.exception(
                "Unexpected error while notifying entities of new WiCAN data",
            )

        return resumed_after_gap

    def _validate_device_identity(self, data: dict[str, Any]) -> None:
        """Ensure device identity hasn't changed.

        Validates that the device_id in the webhook data matches the stored
        device_id from initial configuration. This prevents a different device
        from impersonating the configured device.

        Raises:
            ConfigEntryError: If device_id mismatch is detected.
        """
        # Extract device_id from webhook data (can be in status dict or top-level)
        status = data.get("status", {})
        if not isinstance(status, dict):
            status = {}
        incoming_device_id = status.get("device_id") or data.get("device_id")

        if not incoming_device_id:
            # No device_id provided - skip validation
            # This maintains backward compatibility with older firmware
            return

        # Compare ids as strings so a firmware that serializes the id as a
        # JSON number still matches the stored string form. A non-scalar id
        # can never match anything and falls through to the mismatch path.
        if isinstance(incoming_device_id, (int, float)) and not isinstance(
            incoming_device_id, bool,
        ):
            incoming_device_id = str(incoming_device_id)

        # Get stored device_id from config entry
        stored_device_id = self.config_entry.data.get("device_id")
        if isinstance(stored_device_id, (int, float)) and not isinstance(
            stored_device_id, bool,
        ):
            stored_device_id = str(stored_device_id)

        if not stored_device_id:
            # First time seeing device_id - this is okay
            # The webhook handler will store it in the config entry
            _LOGGER.debug(
                "No stored device_id yet, accepting incoming device_id: %s",
                incoming_device_id,
            )
            return

        # Validate device_id matches
        if incoming_device_id != stored_device_id:
            _LOGGER.error(
                "Device ID mismatch detected! Expected %s, got %s",
                stored_device_id,
                incoming_device_id,
            )
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="device_mismatch",
                translation_placeholders={
                    "expected": str(stored_device_id),
                    "actual": str(incoming_device_id),
                },
            )

        _LOGGER.debug("Device identity validated: %s", incoming_device_id)

    def normalize_sensor_value(self, key: str, raw_value: Any) -> Any:
        """Normalize raw sensor values.

        Converts string values with unit suffixes to proper numeric types.
        This centralizes value normalization logic for consistency.

        Args:
            key: Sensor key (e.g., "batt_voltage")
            raw_value: Raw value from device

        Returns:
            Normalized value suitable for Home Assistant
        """
        if raw_value is None:
            return None

        # A sensor state must be a scalar. Reject nested structures defensively
        # so a malformed device cannot push a dict/list into an entity state
        # (which Home Assistant would reject at state-write time).
        if isinstance(raw_value, (dict, list)):
            _LOGGER.debug("Ignoring non-scalar value for %s: %r", key, raw_value)
            return None

        # JSON parsing accepts NaN/Infinity/1e999; the state machine does not.
        if isinstance(raw_value, float) and not isfinite(raw_value):
            _LOGGER.debug("Ignoring non-finite value for %s: %r", key, raw_value)
            return None

        if isinstance(raw_value, str):
            return self._normalize_string_value(key, raw_value)

        return raw_value

    def _normalize_string_value(self, key: str, raw_value: str) -> Any:
        """Normalize a string sensor value (unit suffixes, numerics, length)."""
        # Battery voltage: strip "V" / " V" suffix (any case) and convert to float
        # Handles firmware variants: "12.5V", "12.5 V", "12.5v", " 12.5 V "
        if key == "batt_voltage":
            _LOGGER.debug("Raw batt_voltage from device: %r", raw_value)
            stripped = raw_value.strip()
            if stripped.upper().endswith("V"):
                numeric_part = stripped[:-1].strip()
                try:
                    return float(numeric_part)
                except ValueError:
                    _LOGGER.warning(
                        "Failed to parse battery voltage: %r (numeric part: %r)",
                        raw_value, numeric_part,
                    )
                    return raw_value

        # Generic numeric string conversion
        cleaned = raw_value.replace(".", "", 1).replace("-", "", 1)
        if cleaned.isdigit():
            try:
                return float(raw_value) if "." in raw_value else int(raw_value)
            except ValueError:
                pass

        # The state machine rejects states longer than 255 characters.
        # Truncate instead of letting the write blow up.
        if len(raw_value) > MAX_LENGTH_STATE_STATE:
            _LOGGER.warning(
                "Truncating overlong value for %s (%d characters)",
                key,
                len(raw_value),
            )
            return raw_value[:MAX_LENGTH_STATE_STATE]

        return raw_value

    def get_sensor_value(self, sensor_key: str) -> Any | None:
        """Get value for a specific sensor."""
        return self._data.get(sensor_key)
