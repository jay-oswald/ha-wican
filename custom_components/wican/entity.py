from homeassistant.core import callback
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from .coordinator import WiCanCoordinator


class WiCanEntityBase(CoordinatorEntity):
    data = {}
    coordinator: WiCanCoordinator
    _state = False
    process_state = None
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator, data, process_state=None):
        super().__init__(coordinator)
        self.data = data
        self.coordinator = coordinator
        self.process_state = process_state

        device_id = self.coordinator.data["status"]["device_id"]

        key = self.get_data("key")
        self._attr_unique_id = "wican_" + device_id + "_" + key
        self.id = "wican_" + device_id[-3:] + "_" + key
        self._attr_name = self.get_data("name")
        self.set_state()

    def get_data(self, key):
        if key in self.data:
            return self.data[key]
        return None

    def get_new_state(self):
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        self.set_state()
        self.async_write_ha_state()

    def set_state(self):
        new_state = self.get_new_state()
        if not new_state:
            return

        if self.process_state is not None:
            new_state = self.process_state(new_state)

        self._state = new_state

    @property
    def device_info(self):
        return self.coordinator.device_info()

    @property
    def available(self) -> bool:
        return self.coordinator.available()

    @property
    def entity_category(self):
        return self.get_data("category")

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        if self.get_data("unit") == "none":
            return None
        else:
            return self.get_data("unit")

    @property
    def device_class(self):
        if self.get_data("class") == "none":
            return None
        else:
            return self.get_data("class")


class WiCanStatusEntity(WiCanEntityBase):
    def __init__(self, coordinator, data, process_state=None):
        super().__init__(coordinator, data, process_state)

    def get_new_state(self):
        return self.coordinator.get_status(self.get_data("key"))

    @property
    def extra_state_attributes(self):
        attributes = self.get_data("attributes")
        if attributes is None:
            return None

        return_attrs = {}
        for key in attributes:
            return_attrs[key] = self.coordinator.get_status(attributes[key])

        return return_attrs


class WiCanPidEntity(WiCanEntityBase):
    def __init__(self, coordinator, data, process_state=None):
        super().__init__(coordinator, data, process_state)

    def get_new_state(self):
        return self.coordinator.get_pid_value(self.get_data("key"))

    @property
    def extra_state_attributes(self):
        attributes = self.get_data("attributes")
        if attributes is None:
            return None

        return_attrs = {}
        for key in attributes:
            return_attrs[key] = self.coordinator.get_pid_value(attributes[key])

        return return_attrs

    @property
    def available(self) -> bool:
        if self._state is False:
            return False

        return True
