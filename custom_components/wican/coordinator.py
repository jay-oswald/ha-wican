"""Coordinator for WiCan Integration.

Purpose: Coordinate data update for WiCAN devices and persist a minimal
snapshot to support offline startup with cached state.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Optional, TypedDict

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import CONF_DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class Snapshot(TypedDict):
    """Minimal persisted snapshot schema for offline startup.

    Keys
    ----
    device_id: str
        Device identifier used in entity unique_id.
    status: dict
        Last known device status payload from `/check_status`.
    pid: dict
        Last known AutoPID data payload (combined metadata + values).
    timestamp: str
        UTC ISO8601 timestamp when the snapshot was written.
    """

    device_id: str
    status: dict
    pid: dict
    timestamp: str


class WiCanCoordinator(DataUpdateCoordinator):
    """WiCAN Coordinator class based on HomeAssistant DataUpdateCoordinator.

    Attributes
    ----------
    api: Any
        WiCan device api to be used.
    data: dict
        Inherited from DataUpdateCoordinator.
        dict is created and filled from WiCan API with first call of method "_async_update_data".

    """

    ecu_online = False

    def __init__(self, hass: HomeAssistant, config_entry, api) -> None:
        """Initialize a WiCanCoordinator and set the WiCan device API."""
        SCAN_INTERVAL = timedelta(
            seconds=config_entry.options.get(
                CONF_SCAN_INTERVAL,
                config_entry.data.get(CONF_SCAN_INTERVAL, CONF_DEFAULT_SCAN_INTERVAL),
            )
        )
        super().__init__(
            hass, _LOGGER, name="WiCAN Coordinator", update_interval=SCAN_INTERVAL
        )
        self.api = api
        # Storage for last-known snapshot
        self._store: Store = Store(
            hass, 1, f"{DOMAIN}_{config_entry.entry_id}_snapshot"
        )
        self._last_persist_utc: Optional[str] = None
        # Debounce writes to avoid excessive I/O
        self._persist_min_interval_sec: int = 30
        # Offline tolerance tracking
        self._stale: bool = False
        self.last_successful_update: Optional[dt_util.datetime] = None

    async def _async_update_data(self):
        return await self.get_data()

    async def get_data(self):
        """Check, if WiCan API is available and return data dictionary containing car configuration and data (PIDs) using the WiCan API.

        Returns
        -------
        data: dict
            Dictionary containing WiCan device status, car configuration and data (PIDs).
            If device API is not reachable, return an empty dict.

        """
        data: dict[str, Any] = {}
        status = await self.api.check_status()

        if not status:
            # Device offline/unreachable: prefer in-memory data, then snapshot, else first-run failure
            if self.data and isinstance(self.data.get("status"), dict):
                _LOGGER.warning("WiCAN device offline; serving stale in-memory data")
                self._stale = True
                return self.data

            snapshot = await self._load_snapshot()
            if snapshot is not None:
                _LOGGER.warning("WiCAN device offline; using cached snapshot")
                self._stale = True
                data["status"] = snapshot.get("status")
                data["pid"] = snapshot.get("pid")
                # Best-effort ECU marker from snapshot
                self.ecu_online = (
                    isinstance(data["status"], dict)
                    and data["status"].get("ecu_status") == "online"
                )
                return data

            # First run: no memory, no snapshot
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"ip_address": self.api.ip},
            )

        data["status"] = status
        self.ecu_online = True
        # self.ecu_online = data['status']['ecu_status'] == 'online'

        if not self.ecu_online:
            await self._persist_snapshot(
                {
                    "device_id": status.get("device_id", "unknown"),
                    "status": status,
                    "pid": {},
                    "timestamp": dt_util.utcnow().isoformat(),
                }
            )
            return data

        pid = await self.api.get_pid()
        data["pid"] = pid if pid else {}

        # Persist minimal snapshot after a successful poll
        try:
            await self._persist_snapshot(
                {
                    "device_id": status.get("device_id", "unknown"),
                    "status": status,
                    "pid": data["pid"],
                    "timestamp": dt_util.utcnow().isoformat(),
                }
            )
        except Exception:  # pragma: no cover - avoid breaking updates on storage errors
            _LOGGER.warning("Failed to persist WiCAN snapshot", exc_info=True)

        # Success path: clear stale flag and record last successful update time
        self._stale = False
        self.last_successful_update = dt_util.utcnow()

        _LOGGER.debug("Updated WiCAN data: %s", list(data.keys()))

        return data

    async def _load_snapshot(self) -> Optional[Snapshot]:
        """Load last-known snapshot from Home Assistant storage.

        Returns None if loading fails or the data is invalid.
        """
        try:
            snapshot = await self._store.async_load()
        except Exception as err:  # file corruption or read error
            _LOGGER.warning("Failed to load WiCAN snapshot: %s", err)
            return None

        if not isinstance(snapshot, dict):
            if snapshot is not None:
                _LOGGER.warning("Ignoring malformed WiCAN snapshot: not a dict")
            return None

        # Basic shape validation
        required = {"device_id", "status", "pid", "timestamp"}
        if not required.issubset(set(snapshot.keys())):
            _LOGGER.warning("Ignoring malformed WiCAN snapshot: missing keys")
            return None
        return snapshot  # type: ignore[return-value]

    async def _persist_snapshot(self, snapshot: Snapshot) -> None:
        """Persist snapshot with simple time-based debouncing.

        To prevent excessive disk writes, this method will not write more than
        once every ``_persist_min_interval_sec`` seconds.
        """
        now_iso = dt_util.utcnow().isoformat()
        if self._last_persist_utc is not None:
            try:
                # Compare using parsed datetimes; fall back to write on parse errors
                last = dt_util.parse_datetime(self._last_persist_utc)
                now = dt_util.parse_datetime(now_iso)
                if last and now:
                    delta = (now - last).total_seconds()
                    if delta < self._persist_min_interval_sec:
                        return
            except Exception:
                # On parsing failure, continue to save
                pass

        # Merge with previous snapshot to preserve last good PID values when new values are missing
        try:
            existing = await self._store.async_load()
        except Exception:
            existing = None

        if isinstance(snapshot.get("pid"), dict) and isinstance(existing, dict):
            prev_pid = existing.get("pid", {}) if isinstance(existing.get("pid"), dict) else {}
            for key, pid_entry in snapshot["pid"].items():
                if not isinstance(pid_entry, dict):
                    continue
                # If new value is None/False, keep previous good value if present
                new_val = pid_entry.get("value") if "value" in pid_entry else None
                prev_entry = prev_pid.get(key, {}) if isinstance(prev_pid, dict) else {}
                prev_val = prev_entry.get("value") if isinstance(prev_entry, dict) else None
                if (new_val is None or new_val is False) and (prev_val is not None and prev_val is not False):
                    pid_entry["value"] = prev_val

        await self._store.async_save(snapshot)
        self._last_persist_utc = now_iso

    async def async_preload_snapshot(self) -> bool:
        """Preload snapshot into coordinator data if available.

        Returns True if a snapshot was loaded, False otherwise.
        """
        snapshot = await self._load_snapshot()
        if not snapshot:
            return False

        # Do not set stale here; staleness is determined on refresh paths
        self.data = {
            "status": snapshot.get("status"),
            "pid": snapshot.get("pid", {}),
        }
        return True

    def device_info(self):
        """Return basic device information shown in HomeAssistant "Device Info" section of the WiCan device.

        Returns
        -------
        dict
            Dictionary containing details about the device (e.g. Device URL, Software Version).

        """
        return {
            "identifiers": {(DOMAIN, self.data["status"]["device_id"])},
            "name": "WiCAN",
            "manufacturer": "MeatPi",
            "model": self.data["status"]["hw_version"],
            "configuration_url": "http://" + self.data["status"]["sta_ip"],
            "sw_version": self.data["status"]["fw_version"],
            "hw_version": self.data["status"]["hw_version"],
        }

    def available(self) -> bool:
        """Check, if WiCan device is available, based on the data received from earlier API calls.

        Returns
        -------
        bool
            Device availability.

        """
        return bool(self.data and self.data.get("status"))

    def stale(self) -> bool:
        """Return True if current data is stale (served from cache or memory when offline)."""
        return self._stale

    def get_status(self, key) -> str | bool:
        """Check, if device status is available from previous API call and get status-value for a given key.

        Parameters
        ----------
        key: Any
            Status key to be checked (e.g. "fw_version").

        Returns
        -------
        str | bool:
            str containing status-value, if device status is available.
            False, if no device status available.

        """
        if not self.data["status"]:
            return False

        return self.data["status"][key]

    def get_pid_value(self, key) -> str | bool | None:
        """Check, if device status is available from previous API call and get value for a given PID-key.

        Parameters
        ----------
        key: Any
            PID-key (e.g. "SOC_BMS") to be checked for available data.

        Returns
        -------
        str | bool
            False, if no device status available.
            str containing value of PID, if device status is available.

        """
        if not self.data["status"]:
            return False

        if self.data["pid"].get(key) is None:
            return None

        value = self.data["pid"][key].get("value")
        if value is False:
            return None
        return value
