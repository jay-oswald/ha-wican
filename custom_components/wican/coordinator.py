import logging
from datetime import timedelta
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, CONF_DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class WiCanCoordinator(DataUpdateCoordinator):
    ecu_online = False

    def __init__(self, hass, config_entry, api):
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

    async def _async_update_data(self):
        return await self.get_data()

    async def get_data(self):
        data = {}
        data["status"] = await self.api.check_status()
        if not data["status"]:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"ip_address": self.api.ip},
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

        if self.data["pid"].get(key) is None:
            return False

        return self.data["pid"][key]["value"]
