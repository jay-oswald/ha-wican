"""Different types of WiCan entities based on DataUpdateCoordinator entities."""

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import WiCanEntityDescription
from .coordinator import WiCanCoordinator


def binary_state(state: str, target_state: str):
    """Check compare binary state against target state to determine if sensor is 'on' or 'off'.

    Parameters
    ----------
    state: str
        binary sensor state
    target_state : str
        target state indicating 'on'.

    Returns
    -------
    str:
        returns homeassistant const STATE_ON or STATE_OFF based on provided input.

    """
    if state == target_state:
        return STATE_ON
    return STATE_OFF


def str_to_float(state: str):
    """Convert status voltage to type float.

    Parameters
    ----------
    state : str
        Voltage value.

    Returns
    -------
    float:
        Voltage value converted to type float.

    """
    return float(state[:-1])


class WiCanEntityBase(CoordinatorEntity):
    """WiCan entity based on DataUpdateCoordinator entity.

    Attributes
    ----------
    coordinator: WiCanCoordinator
        WiCan coordinator handling the device integration via the WiCan API.
    entity_description : WiCanEntityDescription
        Description details for the WiCan entity.

    """

    entity_description: WiCanEntityDescription
    coordinator: WiCanCoordinator
    _state = False
    _attr_has_entity_name = True

    def __init__(self, coordinator, entity_description: WiCanEntityDescription) -> None:
        """Initialize a WiCanEntity with coordinator, entity description and identifiers for HomeAssistant."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self.coordinator = coordinator

        device_id = self.coordinator.data["status"]["device_id"]

        key = self.entity_description.key
        self._attr_unique_id = "wican_" + device_id + "_" + key
        self.id = "wican_" + device_id[-3:] + "_" + key
        if self.entity_description.translation_key is None:
            self._attr_translation_key = self.entity_description.key
        self.set_state()

    def get_new_state(self):
        """Return data from coordinator. Method defined for implementation in child classes of WiCanEntityBase.

        Returns
        -------
        bool
            Always returns False in WiCanEntityBase class.

        """
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        self.set_state()
        self.async_write_ha_state()

    def set_state(self):
        """Set state for entity object. If process_state is set, convert state accordingly."""
        new_state = self.get_new_state()
        if new_state is None:
            return
        if self.entity_description.target_state is not None:
            new_state = binary_state(new_state, self.entity_description.target_state)
        if self.entity_description.process_status_voltage is True:
            new_state = str_to_float(new_state)

        self._state = new_state

    @property
    def device_info(self):
        """Provide WiCan device info from coordinator.

        Returns
        -------
        dict
            Dictionary provided by WiCanCoordinator device_info() method.

        """
        return self.coordinator.device_info()

    @property
    def available(self) -> bool:
        """Provide WiCan device availability from coordinator.

        Returns
        -------
        bool
            Device availability provided by WiCanCoordinator availability() method.

        """
        return self.coordinator.available()

    @property
    def entity_category(self):
        """Provide category of this WiCanEntity, if available.

        Returns
        -------
        EntityCategory
            Category of the entity (e.g. DIAGNOSTIC for some WiCan entities like "IP-Address").

        """
        return self.entity_description.entity_category

    @property
    def state(self):
        """Return the state of this WiCanEntity."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this WiCanEntity, if any."""
        if self.entity_description.unit_of_measurement == "none":
            return None
        return self.entity_description.unit_of_measurement

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        if self.entity_description.device_class == "none":
            return None
        return self.entity_description.device_class


class WiCanStatusEntity(WiCanEntityBase):
    """WiCan Status Entity based on WiCanEntityBase."""

    def __init__(self, coordinator, entity_description: WiCanEntityDescription) -> None:
        """Initialize the status entity same as WiCanEntityBase."""
        super().__init__(coordinator, entity_description)

    def get_new_state(self):
        """Provide entity status from coordindator based on key of this entity (e.g. "fw_version")."""
        return self.coordinator.get_status(self.entity_description.key)

    @property
    def extra_state_attributes(self):
        """Provide state attributes from WiCan device status via coordinator, if defined for entity."""

        """TODO: Check if state attributes shall be replaced by additional sensors: see "Tip" under https://developers.home-assistant.io/docs/core/entity/sensor?_highlight=extra_state_attributes#properties"""
        attributes = self.entity_description.attributes
        if attributes is None:
            return None

        return_attrs = {}
        for key in attributes:
            return_attrs[key] = self.coordinator.get_status(attributes[key])

        return return_attrs


class WiCanPidEntity(WiCanEntityBase):
    """WiCan Data Entity based on WiCanEntityBase."""

    def __init__(self, coordinator, entity_description: WiCanEntityDescription) -> None:
        """Initialize the data entity same as WiCanEntityBase."""
        super().__init__(coordinator, entity_description)
        self._attr_name = self.entity_description.name

    def get_new_state(self):
        """Provide entity value from coordindator based on key of this entity (e.g. "SOC_BMS")."""
        return self.coordinator.get_pid_value(self.entity_description.key)

    @property
    def extra_state_attributes(self):
        """Provide state attributes from WiCan device PID via coordinator, if defined for entity."""
        attributes = self.entity_description.attributes
        if attributes is None:
            return None

        return_attrs = {}
        for key in attributes:
            return_attrs[key] = self.coordinator.get_pid_value(attributes[key])

        return return_attrs

    @property
    def available(self) -> bool:
        """Provide availability of the entity."""
        if self._state is False:
            return False

        return True
