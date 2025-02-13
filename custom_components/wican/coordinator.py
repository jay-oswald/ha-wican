"""Coordinator for WiCAN Integration.

Purpose: Coordinate data update for WiCAN devices.
"""

import logging
from datetime import timedelta
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class WiCanCoordinator(DataUpdateCoordinator):
    """WiCAN Coordinator class.

    Attributes
    ----------
    hass: Any
        HomeAssistant object.
    api: Any
        WiCAN api to be used.

    """

    ecu_online = False

    def __init__(self, hass, api):
        """Initialize a WiCanCoordinator via HomeAssistant DataUpdateCoordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="WiCAN Coordinator",
            update_interval=timedelta(seconds=30),
        )

        self.api = api

    async def _async_update_data(self):
        return await self.get_data()

    async def get_data(self):
        """Check, if WiCan API is available and return data dictionary containing status and PIDs.

        Returns
        -------
        data: dict
            Dictionary containing API status and PIDs.

        """
        data = {}
        data["status"] = await self.api.check_status()
        if data["status"] == False:
            return data

        self.ecu_online = True
        # self.ecu_online = data['status']['ecu_status'] == 'online'

        if not self.ecu_online:
            return data

        data["pid"] = await self.api.get_pid()

        _LOGGER.info(data)

        return data

    def device_info(self):
        """Return device information for HomeAssistant.

        Returns
        -------
        dict
            Dictionary containing details about the device (e.g. configuration URL, software version).

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
        """Check, if WiCan device is available.

        Returns
        -------
        bool
            Device availability.

        """
        return self.data["status"] != False

    def get_status(self, key) -> str | bool:
        """Check, if device status is available and get status-value for a given key.

        Parameters
        ----------
        key: Any
            Status key to be checked.

        Returns
        -------
        str | bool:
            False, if no device status available.
            str containing status-value, if device status is available.

        """
        if not self.data["status"]:
            return False

        return self.data["status"][key]

    def get_pid_value(self, key) -> str | bool:
        """Check, if device status is available and get pid-value for a given key.

        Parameters
        ----------
        key: Any
            PID-key to be checked.

        Returns
        -------
        str | bool
            False, if no device status available.
            str containing pid-value, if device status is available.

        """
        if not self.data["status"]:
            return False

        return self.data["pid"][key]["value"]
