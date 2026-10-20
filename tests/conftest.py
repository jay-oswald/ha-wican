"""Common test fixtures for WiCAN integration tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import DOMAIN, CONF_POST_INTERVAL


# Automatically enable the custom component for all tests
pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    yield


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
def mock_aiohttp_session() -> Generator[MagicMock]:
    """Mock aiohttp session.

    Patched where each module looks the name up: the integration imports
    ``async_get_clientsession`` into its own namespace, so patching it on
    ``homeassistant.helpers.aiohttp_client`` alone left the real session in
    place and the tests reached the network.
    """
    session = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"status": "ok"})
    session.post = AsyncMock(return_value=mock_response)
    with (
        patch("custom_components.wican.async_get_clientsession", return_value=session) as mock_session,
        patch("custom_components.wican.github_releases.async_get_clientsession", return_value=session),
    ):
        yield mock_session


#: The device_id the stand-in WiCAN reports.
TEST_DEVICE_ID = "a0b1c2d3e4f5"


@pytest.fixture
def device_answers(aioclient_mock):
    """Make a WiCAN answer its status endpoint, /check_status, at the given base URLs."""

    def answer(*bases: str, device_id: str = TEST_DEVICE_ID) -> None:
        for base in bases:
            aioclient_mock.get(f"{base.rstrip('/')}/check_status", json={"device_id": device_id})

    return answer
