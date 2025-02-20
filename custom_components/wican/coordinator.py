import logging
from datetime import timedelta
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class WiCanCoordinator(DataUpdateCoordinator):
    ecu_online = False

    def __init__(self, hass, api):
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
        data = {}
        data["status"] = await self.api.check_status()
        if data["status"] == False:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            )

        self.ecu_online = True
        # self.ecu_online = data['status']['ecu_status'] == 'online'

        if not self.ecu_online:
            return data

        data["pid"] = await self.api.get_pid()

        _LOGGER.info(data)

        return data

    def device_info(self):
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
        return self.data["status"] != False

    def get_status(self, key) -> str | bool:
        if not self.data["status"]:
            return False

        return self.data["status"][key]

    def get_pid_value(self, key) -> str | bool:
        if not self.data["status"]:
            return False

        return self.data["pid"][key]["value"]
