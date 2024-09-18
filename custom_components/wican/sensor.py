import logging
from datetime import timedelta
import async_timeout
from homeassistant.components.number import (
    NumberEntity,
    NumberDeviceClass
)
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.core import callback
from homeassistant.const import (
    EntityCategory,
)

from .const import DOMAIN
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    wican = hass.data[DOMAIN][config_entry.entry_id]

    coordinator = WiCanCoordinator(hass, wican)

    await coordinator.async_config_entry_first_refresh()

    entities = [];

    if(coordinator.data['status'] == False):
        return;

    entities.append(WiCanStatusVoltage(coordinator))
    entities.append(WiCanTextStatus(coordinator, 'sta_ip', "IP Address"))
    entities.append(WiCanTextStatus(coordinator, 'ble_status', "Bluetooth Status"))

    async_add_entities(entities)

class WiCanCoordinator(DataUpdateCoordinator):

    def __init__(self, hass, api):
        super().__init__(
            hass,
            _LOGGER,
            name="WiCan Coordinator",
            update_interval = timedelta(seconds=30)
        )

        self.api = api

    async def _async_update_data(self):
        try:
            return await self.get_data()
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")
        
    async def get_data(self):
        data = {};
        data['status'] = await self.api.check_status()

        return data;
    
def device_info(coordinator):
    return {
        "identifiers": {(DOMAIN, coordinator.data['status']['mac'])},
        "name": "WiCan",
        "manufacturer": "MeatPi",
        "model": coordinator.data['status']['hw_version'],
        "configuration_url": "http://" + coordinator.data['status']['sta_ip'],
        "sw_version": coordinator.data['status']['fw_version'],
        "hw_version": coordinator.data['status']['hw_version'],
    }


class WiCanStatusVoltage(CoordinatorEntity, NumberEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.setValues()

    def setValues(self):
        self._attr_unique_id = "wican_" + self.coordinator.data['status']['mac'] + "_status_voltage"
        self._attr_name = "Status Voltage"
        #Removing the "V" from the API data
        self._state = float( self.coordinator.data['status']['batt_voltage'][:-1] )
        self.id = 'wican_status_voltage'

    @callback
    def _handle_coordinator_update(self) -> None:
        self.setValues()
        self.async_write_ha_state()

    @property
    def device_class(self):
        return NumberDeviceClass.VOLTAGE
    
    @property
    def device_info(self):
        return device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return True
    
    @property
    def state(self):
        return self._state
    
    @property
    def mode(self):
        return 'box'
    
    @property
    def native_min_value(self):
        return 0
    
    @property
    def native_max_value(self):
        return 16
    
    @property
    def native_unit_of_measurement(self):
        return "V"
    
    
    @property
    def native_step(self):
        return 0.1

class WiCanTextStatus(CoordinatorEntity):
    sensor = ''
    name = ''
    def __init__(self, coordinator, sensor, name):
        super().__init__(coordinator)
        self.sensor = sensor
        self.name = name
        self.setValues()

    def setValues(self):
        _LOGGER.warning('wican_' + self.sensor)
        self._attr_unique_id = "wican_" + self.coordinator.data['status']['mac'] + "_" + self.sensor
        # self._attr_name = self.name
        self._state = self.coordinator.data['status'][self.sensor]
        self.id = 'wican_' + self.sensor

    @callback
    def _handle_coordinator_update(self) -> None:
        self.setValues()
        self.async_write_ha_state()
    
    @property
    def device_info(self):
        return device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return True

    @property
    def category(self):
        return "diagnostic"
    
    @property
    def state(self):
        return self._state
