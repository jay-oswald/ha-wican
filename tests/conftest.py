"""Common test fixtures for WiCAN integration tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican import _async_register_webhook_on_device
from custom_components.wican.api import LEGACY_CAPABILITIES
from custom_components.wican.const import DOMAIN, CONF_POST_INTERVAL


# Automatically enable the custom component for all tests
pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    yield


@pytest.fixture(autouse=True)
def mock_catalog_fetch() -> Generator[MagicMock]:
    """Keep the device-catalog remote fetch off the network in every test.

    The bundled catalog still loads and applies normally; only the GitHub
    fetch is stubbed to "unreachable". Catalog fetch tests re-patch or
    call the real function against aioclient_mock.
    """
    with patch(
        "custom_components.wican.catalog._async_fetch_remote",
        return_value=None,
    ) as fetch:
        yield fetch


@pytest.fixture(autouse=True)
def reset_device_catalog() -> Generator[None]:
    """Reset the module-level catalog registry between tests."""
    yield
    from custom_components.wican.devices import apply_catalog

    apply_catalog({})


@pytest.fixture(autouse=True)
def mock_capability_probe() -> Generator[MagicMock]:
    """Keep the device capability probe off the network in every test.

    Patches the API client class as referenced by the integration setup
    module, defaulting to legacy capabilities (probe finds no HTTP API) so
    the hundreds of pre-existing tests behave exactly as before control
    support. V6-control tests reconfigure ``async_probe.return_value``. The
    real client class in custom_components.wican.api stays unpatched for
    its own unit tests.
    """
    with patch("custom_components.wican.MeatPiApiClient") as client_cls:
        client_cls.return_value = AsyncMock()
        client_cls.return_value.async_probe.return_value = LEGACY_CAPABILITIES
        yield client_cls


@pytest.fixture(autouse=True)
def mock_webhook_registration(
    request: pytest.FixtureRequest,
) -> Generator[AsyncMock | None]:
    """Keep webhook registration on the device off the network in every test.

    Every push that changes the entry's data re-registers the webhook on the
    device. Unmocked, that connects to the test entry's address, is refused
    and retries with a 1 s then 2 s backoff, so each push costs 3 s. Tests
    of the registration itself opt out with
    ``@pytest.mark.real_webhook_registration``.
    """
    if request.node.get_closest_marker("real_webhook_registration"):
        yield None
        return
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ) as register:
        yield register


@pytest.fixture(autouse=True)
def no_firmware_reboot_delay() -> Generator[None]:
    """Skip the wait for the device to reboot after a firmware update."""
    with patch("custom_components.wican.update.FIRMWARE_UPDATE_REBOOT_DELAY", 0):
        yield


@pytest.fixture
def one_registration_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the real webhook registration a single attempt.

    A failed registration sleeps through a backoff between its attempts;
    a test that lets the real registration fail has no reason to wait.
    """
    monkeypatch.setattr(_async_register_webhook_on_device, "__defaults__", (1,))


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "fw_version": "2.00",
            "hw_version": "v3.1",
            "device_id": "test_device_123",
            "git_version": "abc123",
            "host": "wican_test.local",
            "ip": "192.168.1.100",
        },
        options={
            CONF_POST_INTERVAL: 1000,
        },
        unique_id="wican_test-192.168.1.100:80",
        # As the config flow makes an entry now.
        version=1,
        minor_version=3,
    )


@pytest.fixture
def mock_webhook_data() -> dict:
    """Return mock webhook data."""
    return {
        "status": {
            "wifi_mode": "Station",
            "batt_voltage": "12.5V",
            "sleep_mode": "off",
            "can_protocol": "Auto",
            "ecu_pids_num": 5,
            "ble_status": "Disabled",
            "ap_ssid": "",
            "ap_password": "",
            "ble_device_name": "WiCAN_BLE",
            "vpn_status": "Not Connected",
            "ecu_status": "Online",
            "loop_status": "Stopped",
            "device_id": "test_device_123",
            "uptime": "01:00:00",
        },
        "pids": {
            "pid_0x0c": {
                "name": "Engine RPM",
                "value": 1500,
                "unit": "rpm",
            },
            "pid_0x05": {
                "name": "Coolant Temp",
                "value": 90,
                "unit": "°C",
            },
        },
    }


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up the WiCAN integration for testing."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    return mock_config_entry


@pytest.fixture
async def device(hass: HomeAssistant, hass_client):
    """Return an unconfigured simulated WiCAN device.

    Imported lazily to avoid a circular import (device_sim imports
    MockConfigEntry re-exported by this module).
    """
    from tests.device_sim import MeatPiDeviceSimulator

    return MeatPiDeviceSimulator(hass, hass_client)


@pytest.fixture
def mock_aiohttp_session() -> Generator[MagicMock]:
    """Mock aiohttp session."""
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession"
    ) as mock_session:
        session = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"status": "ok"})
        session.post = AsyncMock(return_value=mock_response)
        mock_session.return_value = session
        yield mock_session
