import logging
from typing import Any
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, CONF_SCAN_INTERVAL
import voluptuous as vol

from homeassistant.data_entry_flow import FlowResult
from .const import DOMAIN, CONF_DEFAULT_SCAN_INTERVAL
from .wican import WiCan

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IP_ADDRESS): str,
        vol.Required(CONF_SCAN_INTERVAL, default=CONF_DEFAULT_SCAN_INTERVAL): int,
    }
)
OPTIONS_SCHEMA = vol.Schema({vol.Required(CONF_SCAN_INTERVAL): int})
_LOGGER = logging.getLogger(__name__)


class WiCanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            ip = user_input[CONF_IP_ADDRESS]
            try:
                wican = WiCan(ip)
                info = await wican.test()

                if info:
                    user_input[CONF_SCAN_INTERVAL] = max(
                        5, user_input[CONF_SCAN_INTERVAL]
                    )
                    return self.async_create_entry(title="WiCAN", data=user_input)
                else:
                    errors[CONF_IP_ADDRESS] = (
                        "Failed validation, double check the IP, as well as check if you have protocol set to auto_pid"
                    )
            except ConnectionError:
                _LOGGER.exception("Connection Error")
                errors[CONF_IP_ADDRESS] = (
                    "WiCAN Connection error, are you sure the IP is correct?"
                )
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors[CONF_IP_ADDRESS] = "WiCAN not validated, unknown error"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ):
        """Create the options flow."""
        return WiCanOptionsFlowHandler()


class WiCanOptionsFlowHandler(config_entries.OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = max(5, user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
