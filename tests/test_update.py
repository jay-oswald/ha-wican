"""Test the WiCAN update platform."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError, ClientResponseError
from homeassistant.components.update import (
    ATTR_VERSION,
    DOMAIN as UPDATE_DOMAIN,
    SERVICE_INSTALL,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.wican.const import DOMAIN, GITHUB_RELEASES_UPDATE_INTERVAL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def mock_params_update():
    """Mock async_update_params_from_github to prevent blocking I/O in tests."""
    with patch(
        "custom_components.wican.param_loader.async_update_params_from_github",
        new_callable=AsyncMock,
        return_value=False,
    ):
        yield


@pytest.fixture
def mock_github_release():
    """Return mock GitHub release data for standard WiCAN-OBD (matches actual v4.13 release)."""
    return {
        "tag_name": "v4.13",
        "name": "WiCAN v4.13",
        "html_url": "https://github.com/meatpiHQ/wican-fw/releases/tag/v4.13",
        "body": "Supported Devices: WiCAN-OBD and WiCAN-USB",
        "prerelease": False,
        "assets": [
            {
                "name": "wican-fw_obd_v413.bin",
                "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.13/wican-fw_obd_v413.bin",
                "size": 1646592,  # 1.57 MB from screenshot
            },
            {
                "name": "wican-fw_obd_v413.zip",
                "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.13/wican-fw_obd_v413.zip",
                "size": 956416,  # 934 KB from screenshot
            },
            {
                "name": "wican-fw_usb_v413u.bin",
                "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.13/wican-fw_usb_v413u.bin",
                "size": 1646592,  # 1.57 MB from screenshot
            },
            {
                "name": "wican-fw_usb_v413u.zip",
                "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.13/wican-fw_usb_v413u.zip",
                "size": 968704,  # 947 KB from screenshot
            },
        ],
    }


@pytest.fixture
def mock_github_release_pro():
    """Return mock GitHub release data for WiCAN-PRO (matches actual GitHub release)."""
    return {
        "tag_name": "v4.45p",
        "name": "WiCAN-PRO v4.45",
        "html_url": "https://github.com/meatpiHQ/wican-fw/releases/tag/v4.45p",
        "body": "Bug Fix:\n- Fixed WiFi primary/fallback issue\n- Removed full configuration from HTTP post",
        "prerelease": False,
        "assets": [
            {
                "name": "wican-fw_obd_pro_v445p.bin",
                "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.45p/wican-fw_obd_pro_v445p.bin",
                "size": 3342336,  # 3.19 MB from screenshot
            },
            {
                "name": "wican-fw_obd_pro_v445p.zip",
                "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.45p/wican-fw_obd_pro_v445p.zip",
                "size": 1793024,  # 1.71 MB from screenshot
            },
        ],
    }


@pytest.fixture
def mock_config_entry_pro() -> MockConfigEntry:
    """Return a mock config entry for WiCAN-PRO."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN PRO Device",
        data={
            "mdns": "http://wican_pro.local:80",
            "webhook_id": "test_webhook_id_pro",
            "fw_version": "4.44p",
            "hw_version": "WiCAN-PRO",
            "device_id": "test_device_pro",
            "host": "wican_pro.local",
            "ip": "192.168.1.101",
        },
        options={"post_interval": 1000},
        unique_id="wican_pro-192.168.1.101:80",
    )


@pytest.fixture
def mock_config_entry_usb() -> MockConfigEntry:
    """Return a mock config entry for WiCAN-USB."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN USB Device",
        data={
            "mdns": "http://wican_usb.local:80",
            "webhook_id": "test_webhook_id_usb",
            "fw_version": "4.12u",
            "hw_version": "WiCAN-USB",
            "device_id": "test_device_usb",
            "host": "wican_usb.local",
            "ip": "192.168.1.102",
        },
        options={"post_interval": 1000},
        unique_id="wican_usb-192.168.1.102:80",
    )


async def test_update_entity_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test update entity is created."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_session,
    ):
        # Mock GitHub API response
        mock_response = AsyncMock()
        mock_response.get.return_value.__aenter__.return_value.status = 200
        mock_response.get.return_value.__aenter__.return_value.json.return_value = [
            mock_github_release,
        ]
        mock_response.get.return_value.__aenter__.return_value.raise_for_status = (
            MagicMock()
        )
        mock_session.return_value.get.return_value.__aenter__.return_value = (
            mock_response.get.return_value.__aenter__.return_value
        )

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Check update entity exists
    state = hass.states.get("update.wican_device_firmware")
    assert state is not None
    assert state.attributes.get("installed_version") == "2.00"


async def test_installed_version_from_coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test installed version comes from coordinator data."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_session,
    ):
        # Mock GitHub API
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = [mock_github_release]
        mock_response.raise_for_status = MagicMock()
        mock_session.return_value.get.return_value.__aenter__.return_value = (
            mock_response
        )

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Simulate webhook data with new firmware version
        coordinator = mock_config_entry.runtime_data.coordinator
        coordinator.handle_webhook_data(
            {
                "status": {
                    "fw_version": "4.50",
                    "device_id": "test_device_123",
                },
            },
        )
        await hass.async_block_till_done()

        # Check installed version updated
        state = hass.states.get("update.wican_device_firmware")
        assert state is not None
        assert state.attributes.get("installed_version") == "4.50"


async def test_latest_version_from_github(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test latest version comes from GitHub coordinator."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_session,
    ):
        # Mock GitHub API - use correct pattern for 'await session.get()' not 'async with'
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[mock_github_release])
        mock_response.raise_for_status = MagicMock()
        mock_session.return_value.get = AsyncMock(return_value=mock_response)

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Check latest version from GitHub
        state = hass.states.get("update.wican_device_firmware")
        assert state is not None
        assert state.attributes.get("latest_version") == "4.13"


async def test_firmware_filename_standard(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test firmware filename for standard WiCAN."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
    ):
        # Mock GitHub API
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Mock firmware download
        mock_firmware_data = b"fake_firmware_data"
        mock_download_response = AsyncMock()
        mock_download_response.read.return_value = mock_firmware_data
        mock_download_response.raise_for_status = MagicMock()

        # Mock firmware upload
        mock_upload_response = AsyncMock()
        mock_upload_response.raise_for_status = MagicMock()

        mock_update_session.return_value.get = AsyncMock(return_value=mock_download_response)
        mock_update_session.return_value.post = AsyncMock(return_value=mock_upload_response)

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: "update.wican_device_firmware"},
            blocking=True,
        )

        # Verify correct asset was downloaded (standard WiCAN-OBD)
        download_calls = mock_update_session.return_value.get.call_args_list
        assert len(download_calls) > 0
        download_url = str(download_calls[0][0][0])
        # Should use the OBD asset URL from GitHub release (not PRO or USB)
        assert "wican-fw_obd_v413.bin" in download_url
        assert "pro" not in download_url.lower()
        assert "usb" not in download_url.lower()


async def test_firmware_filename_pro(
    hass: HomeAssistant,
    mock_config_entry_pro: MockConfigEntry,
    mock_github_release_pro: dict,
) -> None:
    """Test firmware asset selection for WiCAN-PRO."""
    mock_config_entry_pro.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
    ):
        # Mock GitHub API with PRO release
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release_pro])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Mock firmware download
        mock_firmware_data = b"fake_firmware_data_pro"
        mock_download_response = AsyncMock()
        mock_download_response.read.return_value = mock_firmware_data
        mock_download_response.raise_for_status = MagicMock()

        # Mock firmware upload
        mock_upload_response = AsyncMock()
        mock_upload_response.raise_for_status = MagicMock()

        mock_update_session.return_value.get = AsyncMock(return_value=mock_download_response)
        mock_update_session.return_value.post = AsyncMock(return_value=mock_upload_response)

        assert await hass.config_entries.async_setup(mock_config_entry_pro.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: "update.wican_pro_device_firmware"},
            blocking=True,
        )

        # Verify correct PRO asset was downloaded
        download_calls = mock_update_session.return_value.get.call_args_list
        assert len(download_calls) > 0
        download_url = str(download_calls[0][0][0])
        # Should use the PRO asset URL from GitHub release
        assert "wican-fw_obd_pro_v445p.bin" in download_url
        assert "pro" in download_url.lower()


async def test_firmware_filename_usb(
    hass: HomeAssistant,
    mock_config_entry_usb: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test firmware asset selection for WiCAN-USB."""
    mock_config_entry_usb.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
    ):
        # Mock GitHub API with standard release (has both OBD and USB assets)
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Mock firmware download
        mock_firmware_data = b"fake_firmware_data_usb"
        mock_download_response = AsyncMock()
        mock_download_response.read.return_value = mock_firmware_data
        mock_download_response.raise_for_status = MagicMock()

        # Mock firmware upload
        mock_upload_response = AsyncMock()
        mock_upload_response.raise_for_status = MagicMock()

        mock_update_session.return_value.get = AsyncMock(return_value=mock_download_response)
        mock_update_session.return_value.post = AsyncMock(return_value=mock_upload_response)

        assert await hass.config_entries.async_setup(mock_config_entry_usb.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: "update.wican_usb_device_firmware"},
            blocking=True,
        )

        # Verify correct USB asset was downloaded
        download_calls = mock_update_session.return_value.get.call_args_list
        assert len(download_calls) > 0
        download_url = str(download_calls[0][0][0])
        # Should use the USB asset URL (not OBD)
        assert "wican-fw_usb_v413u.bin" in download_url
        assert "usb" in download_url.lower()


async def test_firmware_download_404_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test firmware download with 404 error."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
    ):
        # Mock GitHub API
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Mock firmware download with 404 error
        mock_download_response = AsyncMock()
        mock_download_response.raise_for_status = MagicMock(side_effect=ClientResponseError(
            request_info=MagicMock(), history=(), status=404,
        ))

        mock_update_session.return_value.get = AsyncMock(return_value=mock_download_response)

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update - should fail with version not found
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                UPDATE_DOMAIN,
                SERVICE_INSTALL,
                {ATTR_ENTITY_ID: "update.wican_device_firmware"},
                blocking=True,
            )


async def test_firmware_upload_connection_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test firmware upload with connection error."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
    ):
        # Mock GitHub API
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Mock firmware download success
        mock_firmware_data = b"fake_firmware_data"
        mock_download_response = AsyncMock()
        mock_download_response.read.return_value = mock_firmware_data
        mock_download_response.raise_for_status = MagicMock()

        # Mock firmware upload failure
        mock_upload_response = AsyncMock()
        mock_upload_response.raise_for_status = MagicMock(
            side_effect=ClientError("Connection failed"),
        )

        mock_update_session.return_value.get = AsyncMock(return_value=mock_download_response)
        mock_update_session.return_value.post = AsyncMock(return_value=mock_upload_response)

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update - should fail with upload error
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                UPDATE_DOMAIN,
                SERVICE_INSTALL,
                {ATTR_ENTITY_ID: "update.wican_device_firmware"},
                blocking=True,
            )


async def test_firmware_update_with_specific_version(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test firmware update with specific version."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
    ):
        # Mock GitHub API
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Provide an additional release for v4.40 for SPECIFIC_VERSION path
        mock_github_release_v440 = {
            "tag_name": "v4.40",
            "name": "WiCAN v4.40",
            "html_url": "https://github.com/meatpiHQ/wican-fw/releases/tag/v4.40",
            "body": "Supported Devices: WiCAN-OBD and WiCAN-USB",
            "prerelease": False,
            "assets": [
                {
                    "name": "wican-fw_obd_v440.bin",
                    "browser_download_url": "https://github.com/meatpiHQ/wican-fw/releases/download/v4.40/wican-fw_obd_v440.bin",
                    "size": 1600000,
                },
            ],
        }

        # Mock firmware download
        mock_firmware_data = b"fake_firmware_v4.40"
        mock_download_response = AsyncMock()
        mock_download_response.read = AsyncMock(return_value=mock_firmware_data)
        mock_download_response.raise_for_status = MagicMock()

        # Mock firmware upload
        mock_upload_response = AsyncMock()
        mock_upload_response.raise_for_status = MagicMock()

        # First GET is GitHub releases API (for version lookup), second GET is firmware binary
        mock_api_response = AsyncMock()
        mock_api_response.status = 200
        mock_api_response.raise_for_status = MagicMock()
        mock_api_response.json = AsyncMock(
            return_value=[mock_github_release, mock_github_release_v440],
        )

        mock_update_session.return_value.get = AsyncMock(
            side_effect=[mock_api_response, mock_download_response],
        )
        mock_update_session.return_value.post = AsyncMock(return_value=mock_upload_response)

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update with specific version
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {
                ATTR_ENTITY_ID: "update.wican_device_firmware",
                ATTR_VERSION: "4.40",
            },
            blocking=True,
        )

        # Verify correct version was requested
        download_calls = mock_update_session.return_value.get.call_args_list
        assert len(download_calls) >= 2
        download_url = str(download_calls[1][0][0])
        assert "wican-fw_obd_v440.bin" in download_url


async def test_update_entity_unavailable_without_version(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test update entity is unavailable without installed version."""
    # Create config entry without fw_version
    config_entry_no_version = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "webhook_id": "test_webhook_id",
            "device_id": "test_device_123",
            "host": "wican_test.local",
        },
        options={"post_interval": 1000},
        unique_id="wican_test-192.168.1.100:80",
    )
    config_entry_no_version.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_session,
    ):
        # Mock GitHub API
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = [mock_github_release]
        mock_response.raise_for_status = MagicMock()
        mock_session.return_value.get.return_value.__aenter__.return_value = (
            mock_response
        )

        assert await hass.config_entries.async_setup(config_entry_no_version.entry_id)
        await hass.async_block_till_done()

        # Check update entity is unavailable
        state = hass.states.get("update.wican_device_firmware")
        assert state is not None
        assert state.state == "unavailable"


async def test_progress_reporting_during_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """Test progress is reported during firmware update."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        patch(
            "custom_components.wican.github_releases.async_get_clientsession",
        ) as mock_gh_session,
        patch(
            "custom_components.wican.update.async_get_clientsession",
        ) as mock_update_session,
        patch("asyncio.sleep", return_value=None),  # Speed up test
    ):
        # Mock GitHub API
        mock_gh_response = AsyncMock()
        mock_gh_response.status = 200
        mock_gh_response.json = AsyncMock(return_value=[mock_github_release])
        mock_gh_response.raise_for_status = MagicMock()
        mock_gh_session.return_value.get = AsyncMock(return_value=mock_gh_response)

        # Mock firmware download
        mock_firmware_data = b"fake_firmware_data"
        mock_download_response = AsyncMock()
        mock_download_response.read.return_value = mock_firmware_data
        mock_download_response.raise_for_status = MagicMock()

        # Mock firmware upload
        mock_upload_response = AsyncMock()
        mock_upload_response.raise_for_status = MagicMock()

        mock_update_session.return_value.get = AsyncMock(return_value=mock_download_response)
        mock_update_session.return_value.post = AsyncMock(return_value=mock_upload_response)

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        # Trigger firmware update
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: "update.wican_device_firmware"},
            blocking=True,
        )

        # Verify update completed (progress back to False)
        state = hass.states.get("update.wican_device_firmware")
        assert state is not None
        assert state.attributes.get("in_progress") is False


async def test_ota_prefers_v6_route_with_legacy_fallback(
    hass: HomeAssistant,
    init_integration,
) -> None:
    """Contract v2: OTA goes to /api/ota/upload; legacy is the bridge.

    The legacy /upload/ota.bin route is deliberately kept as the
    migration path — it is how pre-v5 firmware gets updated to a
    supported version from HA in the first place.
    """
    from unittest.mock import MagicMock

    import aiohttp as aiohttp_mod

    from custom_components.wican.update import MeatPiUpdateEntity

    entity = MeatPiUpdateEntity(init_integration)
    entity.hass = hass

    ok = AsyncMock()
    ok.raise_for_status = MagicMock()
    ok.read = AsyncMock(return_value=b"")
    ok.text = AsyncMock(return_value="OK")

    posted: list[str] = []

    session = MagicMock()
    session.post = AsyncMock(side_effect=lambda url, **kw: (posted.append(str(url)), ok)[1])
    with patch(
        "custom_components.wican.update.async_get_clientsession",
        return_value=session,
    ):
        await entity._upload_firmware_to_device(b"fw", "wican-fw_obd_v500.bin")

    assert len(posted) == 1
    assert posted[0].endswith("/api/ota/upload")

    # Pre-v5 firmware 404s the V6 route: the legacy route takes over.
    posted.clear()
    bad = AsyncMock()
    bad.raise_for_status = MagicMock(
        side_effect=aiohttp_mod.ClientResponseError(
            request_info=MagicMock(), history=(), status=404,
        ),
    )

    def _route(url, **kw):
        posted.append(str(url))
        return bad if "/api/ota/" in str(url) else ok

    session.post = AsyncMock(side_effect=_route)
    with patch(
        "custom_components.wican.update.async_get_clientsession",
        return_value=session,
    ):
        await entity._upload_firmware_to_device(b"fw", "wican-fw_obd_v500.bin")

    assert posted[0].endswith("/api/ota/upload")
    assert posted[1].endswith("/upload/ota.bin")


async def test_latest_version_recovers_after_failed_first_fetch(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_github_release: dict,
) -> None:
    """A failed GitHub fetch at setup is retried on the hourly poll.

    The update entity must listen to the releases coordinator: a
    DataUpdateCoordinator only schedules its next refresh while it has
    listeners, so without one a single failed fetch at startup leaves
    the latest version unknown until Home Assistant restarts.
    """
    mock_config_entry.add_to_hass(hass)

    gateway_timeout = MagicMock()
    gateway_timeout.raise_for_status = MagicMock(
        side_effect=ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=504,
        ),
    )
    good = AsyncMock()
    good.json = AsyncMock(return_value=[mock_github_release])
    good.raise_for_status = MagicMock()
    mock_get = AsyncMock(side_effect=[gateway_timeout, good])

    with patch(
        "custom_components.wican.github_releases.async_get_clientsession",
    ) as mock_session:
        mock_session.return_value.get = mock_get

        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get("update.wican_device_firmware")
        assert state is not None
        assert state.attributes.get("latest_version") is None

        async_fire_time_changed(
            hass,
            dt_util.utcnow() + timedelta(seconds=GITHUB_RELEASES_UPDATE_INTERVAL + 1),
        )
        await hass.async_block_till_done()

    assert mock_get.await_count == 2
    state = hass.states.get("update.wican_device_firmware")
    assert state is not None
    assert state.attributes.get("latest_version") == "4.13"
