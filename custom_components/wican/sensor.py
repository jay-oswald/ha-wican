import logging
from datetime import timedelta
import async_timeout
from homeassistant.components.number import (
    NumberEntity,
    NumberDeviceClass
)

from homeassistant.components.binary_sensor import (
    BinarySensorEntity
)

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.core import callback
from homeassistant.const import (
    EntityCategory,
)
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    wican = hass.data[DOMAIN][config_entry.entry_id]

    coordinator = WiCanCoordinator(hass, wican)

    await coordinator.async_config_entry_first_refresh()

    entities = [];

    if(coordinator.data['status'] == False):
        return;EntityCategory.DIAGNOSTIC

    entities.append(WiCanStatusVoltage(coordinator))
    entities.append(WiCanText(coordinator, {
        "key": "sta_ip",
        "name": "IP Address",
        "category": EntityCategory.DIAGNOSTIC
    }))
    entities.append(WiCanTextStatus(coordinator, {
        "key": "protocol",
        "name": "Mode".
        "category": EntityCategory.DIAGNOSTIC
    }))
    entities.append(WiCanBinarySensor(coordinator, 'ble_status', "Bluetooth Status", coordinator.data['status']['ble_status'] == 'enable'))
    # entities.append(WiCanTextStatus(coordinator, 'mqtt_en', "MQTT Status", coordinator.data['status']['ble_status'] == 'enable'))


    async_add_entities(entities)

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

class WiCanSensorBase(CoordinatorEntity):
    data = {}
    coordinator = None
    process_state = None
    _attr_has_entity_name = True
    _attr_name = None
    def __init__(self, coordinator, data, process_state = None):
        super().__init__(coordinator)
        self.data = data
        self.coordinator = coordinator
        self.process_state = process_state

        key = self.get_data('key')
        self._attr_unique_id = "wican_" + self.coordinator.data['status']['mac'] + "_" + key
        self.id = 'wican_' + key
        self._attr_name = self.get_data('name')
        self.set_state()

    def get_data(self, key):
        if key in self.data:
            return self.data[key]
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.set_state()
        self.async_write_ha_state()
    
    def set_state(self):
        _LOGGER.warning(self.process_state)
        _LOGGER.warning(self.coordinator.data['status'][self.get_data('key')])
        
        if self.process_state is not None:
            self._state = self.process_state(self)
            return
        self._state = self.coordinator.data['status'][self.get_data('key')]


    @property
    def device_class(self):
        return self.get_data("device_class")

    @property
    def device_info(self):
        return device_info(self.coordinator)

    # TODO
    @property
    def available(self) -> bool:
        return True

    @property
    def entity_category(self):
        return self.get_data("category")

    @property
    def state(self):
        return self._state

class WiCanText(WiCanSensorBase):
    def __init(self, coordinator, data):
        super().__init__(coordinator, data)

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

class WiCanBinary(WiCanSensorBase):
    def __init(self, coordinator, data):
        super().__init__(coordinator, data)


class WiCanBinarySensor(CoordinatorEntity, BinarySensorEntity):
    sensor = ''
    name = ''
    state = None
    def __init__(self, coordinator, sensor, name, state):
        super().__init__(coordinator)
        self.sensor = sensor
        self.name = name
        self.setValues()
        self.state = state

    def setValues(self):
        self._attr_unique_id = "wican_" + self.coordinator.data['status']['mac'] + "_" + self.sensor
        # self._attr_name = self.name
        self._attr_is_on = self.state
        self.id = 'wican_' + self.sensor

    
    @property
    def device_info(self):
        return device_info(self.coordinator)

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True
