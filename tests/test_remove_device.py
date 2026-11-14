"""Test removing stale WiCAN devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican import async_remove_config_entry_device
from custom_components.wican.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, identifier: str,
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, identifier)},
    )


async def test_current_adapter_device_cannot_be_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The device of the adapter the entry is bound to stays."""
    mock_config_entry.add_to_hass(hass)
    device = _device(hass, mock_config_entry, "test_device_123")

    assert not await async_remove_config_entry_device(
        hass, mock_config_entry, device,
    )


async def test_replaced_adapter_device_can_be_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A device left behind by a replaced adapter can be removed."""
    mock_config_entry.add_to_hass(hass)
    device = _device(hass, mock_config_entry, "old_device_456")

    assert await async_remove_config_entry_device(
        hass, mock_config_entry, device,
    )


async def test_pre_device_id_placeholder_can_be_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The entry-id device made before the first webhook can be removed."""
    mock_config_entry.add_to_hass(hass)
    device = _device(hass, mock_config_entry, mock_config_entry.entry_id)

    assert await async_remove_config_entry_device(
        hass, mock_config_entry, device,
    )


async def test_entry_id_device_protected_until_device_id_known(
    hass: HomeAssistant,
) -> None:
    """Before the adapter reports a device_id, the entry-id device is current."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    device = _device(hass, entry, entry.entry_id)

    assert not await async_remove_config_entry_device(hass, entry, device)
