"""Update platform for WiCAN integration."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    FIRMWARE_DOWNLOAD_TIMEOUT,
    FIRMWARE_UPDATE_REBOOT_DELAY,
    FIRMWARE_UPLOAD_TIMEOUT,
    GITHUB_API_RELEASES_URL,
    GITHUB_API_TIMEOUT,
    GITHUB_OWNER,
    GITHUB_REPO,
    OTA_FORM_FIELD,
    OTA_LEGACY_ENDPOINT,
    OTA_V6_ENDPOINT,
)
from .entity import MeatPiEntity
from .exceptions import (
    FirmwareDownloadError,
    FirmwareUploadError,
    FirmwareVersionNotFoundError,
)
from .github_releases import hardware_stream, release_matches_stream

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MeatPiConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1  # Only one update at a time


def _tag_version(release: dict[str, Any] | None) -> str:
    """Version part of a release's tag, null-safe and prefix-insensitive.

    GitHub can hand back ``"tag_name": null``, and a maintainer may tag
    ``V4.45`` — neither must break the update entity's properties.
    """
    if not release:
        return ""
    return str(release.get("tag_name") or "").lstrip("vV")


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: MeatPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WiCAN update entity."""
    async_add_entities([MeatPiUpdateEntity(entry)])


class MeatPiUpdateEntity(MeatPiEntity, UpdateEntity):
    """Representation of WiCAN firmware update entity."""

    __slots__ = ("_github_coordinator", "_update_in_progress")

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.SPECIFIC_VERSION
        | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, config_entry: MeatPiConfigEntry) -> None:
        """Initialize the update entity."""
        # Create entity description for parent class
        entity_description = UpdateEntityDescription(
            key="firmware",
            translation_key="firmware",
            device_class=UpdateDeviceClass.FIRMWARE,
        )
        super().__init__(config_entry, entity_description)
        # The update dialog's software name: use the product, not the brand
        # of one product line (an ESPNetlink is not "WiCAN Firmware").
        self._attr_title = (
            f"{config_entry.runtime_data.device_profile.model} Firmware"
        )
        self._github_coordinator = config_entry.runtime_data.github_coordinator
        self._update_in_progress = False

    async def async_added_to_hass(self) -> None:
        """Listen to the releases coordinator as well as the device's.

        A coordinator schedules its periodic refresh only while it has
        listeners. Without this one the releases are never fetched again
        after setup, and a fetch that failed at setup leaves the latest
        version unknown until Home Assistant restarts.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self._github_coordinator.async_add_listener(
                self._handle_coordinator_update,
            ),
        )

    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle WiCAN webhook event.

        Update entity doesn't need webhook events - version info comes from coordinator.
        This is required by MeatPiEntity base class.
        """
        # No action needed - version info updated via coordinator

    @property
    def installed_version(self) -> str | None:
        """Return the installed firmware version."""
        # Get from coordinator data (from webhook status)
        data = self.coordinator.data or {}
        status = data.get("status", {})
        fw_version = status.get("fw_version") if isinstance(status, dict) else None
        version = self._normalize_version(fw_version)
        if not version:
            # Fallback to config entry data (sanitized at webhook time)
            version = self._normalize_version(self.config_entry.data.get("fw_version"))
        return version

    @property
    def latest_version(self) -> str | None:
        """Return the latest firmware version from GitHub."""
        version = _tag_version(self._github_coordinator.data)
        return self._normalize_version(version) or None

    def _normalize_version(self, version: str | None) -> str | None:
        """Normalize version string by removing device-specific suffixes.

        Device firmware versions are reported without suffixes (e.g., "4.46"),
        but GitHub releases include suffixes (e.g., "4.45p" for PRO, "4.13u" for USB).
        Strip these suffixes to enable proper version comparison.

        Examples:
            "4.45p" -> "4.45"  (PRO)
            "4.13u" -> "4.13"  (USB)
            "4.46"  -> "4.46"  (already normalized)
        """
        # The device-reported version flows in unvalidated from the webhook
        # payload; tolerate a firmware that serializes it as a number and
        # treat anything else as unknown.
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            version = str(version)
        if version is not None and not isinstance(version, str):
            return None
        if not version:
            return version
        # Remove common suffixes: 'p' (PRO), 'u' (USB) — either case, so a
        # maintainer tagging v4.45P cannot leave an eternal update offered.
        return version.rstrip("puPU")

    @property
    def release_url(self) -> str | None:
        """Return the URL for release notes."""
        if not self._github_coordinator.data:
            return None
        return self._github_coordinator.data.get("html_url")

    @property
    def release_summary(self) -> str | None:
        """Return the release notes summary."""
        if not self._github_coordinator.data:
            return None
        body = str(self._github_coordinator.data.get("body", ""))
        # Truncate to first 500 chars
        return body[:500] + "..." if len(body) > 500 else body

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Install firmware update."""
        if self._update_in_progress:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_in_progress",
            )

        try:
            self._update_in_progress = True
            self._attr_in_progress = True
            self.async_write_ha_state()

            # Resolve version (use latest if not specified). Users naturally
            # type the tag forms ("v4.45", "4.45p"); normalize them the same
            # way release tags are normalized so the lookup can match.
            if isinstance(version, str):
                version = self._normalize_version(version.lstrip("vV"))
            target_version = version or self.latest_version
            if not target_version:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="no_firmware_version",
                )

            _LOGGER.info("Starting firmware update to version %s", target_version)

            # Step 1: Download firmware from GitHub (50% of progress)
            self._attr_update_percentage = 50
            self.async_write_ha_state()
            firmware_data, firmware_filename = await self._download_firmware(target_version)

            # Step 2: Upload firmware to device (50% to 100%)
            self._attr_update_percentage = 75
            self.async_write_ha_state()
            await self._upload_firmware_to_device(firmware_data, firmware_filename)

            _LOGGER.info("Firmware update initiated successfully")

            # Update complete - device will reboot
            self._attr_update_percentage = 100
            self.async_write_ha_state()

            # Wait for device to start updating, then refresh
            await asyncio.sleep(FIRMWARE_UPDATE_REBOOT_DELAY)
            await self.coordinator.async_request_refresh()

        except FirmwareVersionNotFoundError as err:
            _LOGGER.exception("Firmware update failed")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="version_not_found",
                translation_placeholders={"version": str(target_version or "")},
            ) from err
        except FirmwareDownloadError as err:
            _LOGGER.exception("Firmware update failed")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_download_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except FirmwareUploadError as err:
            _LOGGER.exception("Firmware update failed")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_upload_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            self._update_in_progress = False
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()

    async def _fetch_github_release(self, version: str) -> dict[str, Any]:
        """Fetch a specific release from GitHub API.

        Args:
            version: Version to fetch (normalized, without 'v' prefix or suffixes)

        Returns:
            Release data dict from GitHub API

        """
        url = GITHUB_API_RELEASES_URL.format(
            owner=GITHUB_OWNER,
            repo=GITHUB_REPO,
        )

        session = async_get_clientsession(self.hass)

        try:
            async with asyncio.timeout(GITHUB_API_TIMEOUT):
                response = await session.get(
                    url,
                    headers={"Accept": "application/vnd.github.v3+json"},
                )
                response.raise_for_status()
                releases = await response.json()

            # Determine the device's firmware stream for filtering
            device_stream = hardware_stream(self.config_entry.data.get("hw_version"))

            # Search for matching release
            # Version can have suffixes in GitHub (4.45p, 4.13u) but we get normalized version
            if not isinstance(releases, list):
                releases = []
            for release in releases:
                if not isinstance(release, dict) or release.get("prerelease", False):
                    continue

                tag_name = _tag_version(release)
                normalized_tag = self._normalize_version(tag_name)

                # Check if this release matches the requested version
                if normalized_tag != version:
                    continue

                # Check if this release can serve the device's stream
                if release_matches_stream(release, device_stream):
                    _LOGGER.info(
                        "Found GitHub release %s for version %s",
                        tag_name,
                        version,
                    )
                    return cast("dict[str, Any]", release)

            raise FirmwareVersionNotFoundError(
                f"Version {version} not found in GitHub releases for this device type",
            )

        except TimeoutError as err:
            raise FirmwareDownloadError(
                "Timeout fetching GitHub releases",
            ) from err
        except aiohttp.ClientError as err:
            raise FirmwareDownloadError(
                f"Failed to fetch GitHub releases: {err}",
            ) from err

    async def _download_firmware(self, version: str) -> tuple[bytes, str]:  # noqa: C901, PLR0912, PLR0915
        """Download firmware binary from GitHub release assets.

        Args:
            version: Version to download (without 'v' prefix, normalized without suffixes)

        Returns:
            Tuple of (firmware_data, filename)

        """
        # Fetch specific release from GitHub if version differs from latest
        normalized_latest = self._normalize_version(
            _tag_version(self._github_coordinator.data),
        )

        # If requesting a different version than the cached latest, fetch it from GitHub
        if version != normalized_latest:
            release_data = await self._fetch_github_release(version)
        else:
            # Use cached coordinator data for latest version
            release_data = self._github_coordinator.data

        if not release_data:
            raise FirmwareDownloadError(
                f"Could not find GitHub release for version {version}",
            )

        # Find firmware asset in release
        assets = release_data.get("assets", [])
        if not assets:
            raise FirmwareVersionNotFoundError(
                f"No assets found in release {version}",
            )

        # Determine device type from hardware version
        hw_version = str(self.config_entry.data.get("hw_version") or "").lower()
        is_pro = "pro" in hw_version
        is_usb = "usb" in hw_version

        # Find the .bin file that matches device type
        # Firmware naming patterns:
        # - PRO: wican-fw_obd_pro_vXXXp.bin
        # - USB: wican-fw_usb_vXXXu.bin
        # - OBD: wican-fw_obd_vXXX.bin
        firmware_asset = None
        for asset in assets:
            name = asset.get("name", "").lower()
            if not name.endswith(".bin"):
                continue

            # Match asset to device type
            has_pro = "pro" in name
            has_usb = "usb" in name and "pro" not in name
            has_obd = "obd" in name and "usb" not in name and "pro" not in name

            if (is_pro and has_pro) or (is_usb and has_usb):
                firmware_asset = asset
                break
            if not is_pro and not is_usb and has_obd:
                firmware_asset = asset
                break

        if not firmware_asset:
            device_type = "PRO" if is_pro else ("USB" if is_usb else "OBD")
            raise FirmwareVersionNotFoundError(
                f"No {device_type} firmware found in release {version}",
            )

        download_url = firmware_asset.get("browser_download_url")
        if not download_url:
            raise FirmwareDownloadError(
                "No download URL found for firmware asset",
            )

        firmware_filename = firmware_asset.get("name", "firmware.bin")

        _LOGGER.debug(
            "Downloading firmware from %s (asset: %s)",
            download_url,
            firmware_filename,
        )

        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(FIRMWARE_DOWNLOAD_TIMEOUT):
                response = await session.get(download_url)
                response.raise_for_status()
                firmware_data = await response.read()
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                raise FirmwareVersionNotFoundError(
                    f"Firmware asset not found at {download_url}",
                ) from err
            raise FirmwareDownloadError(
                f"Failed to download firmware: HTTP {err.status}",
            ) from err
        except TimeoutError as err:
            raise FirmwareDownloadError(
                "Timeout downloading firmware from GitHub",
            ) from err
        except aiohttp.ClientError as err:
            raise FirmwareDownloadError(
                f"Network error downloading firmware: {err}",
            ) from err
        else:
            _LOGGER.info(
                "Downloaded firmware %s (%d bytes)",
                firmware_filename,
                len(firmware_data),
            )
            return firmware_data, firmware_filename

    async def _upload_firmware_to_device(self, firmware_data: bytes, firmware_filename: str) -> None:
        """Upload firmware to device OTA endpoint.

        Args:
            firmware_data: Binary firmware data
            firmware_filename: Original filename from GitHub release (e.g., wican-fw_obd_pro_v445p.bin)

        """
        # Get device connection info
        device_host = (
            self.config_entry.runtime_data.device_host
            or self.config_entry.data.get("host")
            or self.config_entry.data.get("mdns")
        )

        if not device_host:
            # Fallback to IP
            device_ip = self.config_entry.runtime_data.device_ip
            if device_ip:
                device_host = f"http://{device_ip}"

        if not device_host:
            raise FirmwareUploadError(
                translation_domain=DOMAIN,
                translation_key="device_unreachable",
            )

        # Ensure http:// prefix
        if not device_host.startswith(("http://", "https://")):
            device_host = f"http://{device_host}"

        # Get the session without timeout/connector limits that might interfere
        session = async_get_clientsession(self.hass, verify_ssl=False)

        # Contract v2 route first: raw octet-stream to /api/ota/upload.
        v6_url = f"{device_host.rstrip('/')}{OTA_V6_ENDPOINT}"
        _LOGGER.debug(
            "Uploading firmware %s (%d bytes) to %s",
            firmware_filename, len(firmware_data), v6_url,
        )
        try:
            async with asyncio.timeout(FIRMWARE_UPLOAD_TIMEOUT):
                response = await session.post(
                    v6_url,
                    data=firmware_data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                await response.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            # Migration bridge: pre-v5 firmware only serves the legacy
            # upload route — the very path users take to REACH v5/v6.
            _LOGGER.info(
                "V6 OTA route unavailable (%s); using the legacy upload "
                "route (pre-v5 firmware migration bridge)",
                err,
            )
        else:
            _LOGGER.info("Firmware uploaded successfully to device (V6 route)")
            return

        url = f"{device_host.rstrip('/')}{OTA_LEGACY_ENDPOINT}"
        _LOGGER.debug("Uploading firmware %s (%d bytes) to %s", firmware_filename, len(firmware_data), url)

        try:
            # Attempt 1: regular multipart upload (may use chunked transfer encoding).
            # Now that device firmware supports multipart+chunked, prefer this simpler path.
            try:
                async with asyncio.timeout(FIRMWARE_UPLOAD_TIMEOUT):
                    form_data = aiohttp.FormData()
                    form_data.add_field(
                        OTA_FORM_FIELD,
                        firmware_data,
                        filename=firmware_filename,
                        content_type="application/octet-stream",
                    )

                    response = await session.post(
                        url,
                        data=form_data,
                        headers={
                            "Accept": "*/*",
                            "Connection": "keep-alive",
                        },
                    )
                    response.raise_for_status()
                    await response.text()
            except (TimeoutError, aiohttp.ClientError) as err:
                # Attempt 2: fallback to fixed Content-Length multipart body.
                # Useful for devices/firmwares that still struggle with chunked uploads.
                _LOGGER.warning(
                    "Initial firmware upload attempt failed (%s). Retrying with fixed Content-Length multipart body.",
                    err,
                )

                boundary = f"----wican-ha-{secrets.token_hex(16)}"
                header = (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{OTA_FORM_FIELD}"; filename="{firmware_filename}"\r\n'
                    "Content-Type: application/octet-stream\r\n"
                    "\r\n"
                ).encode()
                footer = f"\r\n--{boundary}--\r\n".encode()
                body = header + firmware_data + footer

                _LOGGER.debug(
                    "Firmware multipart body size: %d bytes (boundary=%s)",
                    len(body),
                    boundary,
                )

                async with asyncio.timeout(FIRMWARE_UPLOAD_TIMEOUT):
                    response = await session.post(
                        url,
                        data=body,
                        headers={
                            "Accept": "*/*",
                            "Connection": "keep-alive",
                            "Content-Type": f"multipart/form-data; boundary={boundary}",
                            "Content-Length": str(len(body)),
                        },
                    )
                    response.raise_for_status()
                    await response.text()

            _LOGGER.info("Firmware uploaded successfully to device")

        except TimeoutError as err:
            raise FirmwareUploadError(
                "Timeout uploading firmware to device",
            ) from err
        except aiohttp.ClientError as err:
            raise FirmwareUploadError(
                f"Failed to upload firmware to device: {err}",
            ) from err



    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Available if we have version info from device
        return self.installed_version is not None
