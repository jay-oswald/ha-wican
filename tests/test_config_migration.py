"""Tests for config-entry migration.

1.1 → 1.2: the device-type schema. 1.2 → 1.3: an entry made before the
record of the webhook settings its device holds is marked as holding an
earlier release's settings.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import (
    CONF_DEVICE_TYPE,
    CONF_HELD_BY_EARLIER_RELEASE,
    CONF_HELD_WEBHOOK_REV,
    DOMAIN,
)
from custom_components.wican.devices import (
    DEVICE_TYPE_WICAN,
    DEVICE_TYPE_WICAN_PRO,
)


def _legacy_entry(**data_overrides: object) -> MockConfigEntry:
    """Return a version 1.1 entry as written by the previous release."""
    data = {
        "mdns": "http://wican_test.local:80",
        CONF_WEBHOOK_ID: "test_webhook_id",
        "fw_version": "4.10",
        "hw_version": "WiCAN-OBD",
        "device_id": "test_device_123",
    }
    data.update(data_overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=data,
        version=1,
        minor_version=1,
        unique_id="test_device_123",
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_migrates_1_1_to_1_2_with_device_type(
    hass: HomeAssistant,
) -> None:
    """A 1.1 entry gains a device_type inferred from its hardware version."""
    entry = _legacy_entry(hw_version="WiCAN-PRO")

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 1
    assert entry.minor_version == 3
    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WICAN_PRO


async def test_migrates_entry_without_hw_version(hass: HomeAssistant) -> None:
    """A 1.1 entry with no hardware version defaults to the WiCAN profile."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        version=1,
        minor_version=1,
        unique_id="no_hw_device",
    )

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 3
    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WICAN


async def test_migration_preserves_existing_device_type(
    hass: HomeAssistant,
) -> None:
    """An entry that somehow already carries device_type keeps it."""
    entry = _legacy_entry(
        hw_version="WiCAN-OBD", **{CONF_DEVICE_TYPE: DEVICE_TYPE_WICAN_PRO},
    )

    await _setup(hass, entry)

    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WICAN_PRO


async def test_future_major_version_not_migrated(hass: HomeAssistant) -> None:
    """An entry from a future major version refuses to load (downgrade)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={CONF_WEBHOOK_ID: "test_webhook_id"},
        version=2,
        minor_version=1,
        unique_id="future_device",
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_current_entries_not_touched(hass: HomeAssistant) -> None:
    """A 1.2 entry passes through migration unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            CONF_WEBHOOK_ID: "test_webhook_id",
            "mdns": "http://wican_test.local:80",
            CONF_DEVICE_TYPE: DEVICE_TYPE_WICAN,
        },
        version=1,
        minor_version=2,
        unique_id="current_device",
    )

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 3
    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_WICAN


async def test_migrates_1_2_to_1_3_marks_earlier_release_settings(
    hass: HomeAssistant,
) -> None:
    """A 1.2 entry kept no record of what its device holds: it is marked so."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data=dict(_legacy_entry(device_type=DEVICE_TYPE_WICAN_PRO).data),
        version=1,
        minor_version=2,
        unique_id="test_device_123",
    )

    await _setup(hass, entry)

    assert entry.minor_version == 3
    assert entry.data[CONF_HELD_BY_EARLIER_RELEASE] is True


async def test_migrates_1_2_to_1_3_keeps_a_record(hass: HomeAssistant) -> None:
    """A 1.2 entry that already records what its device holds is not marked."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            **_legacy_entry(device_type=DEVICE_TYPE_WICAN_PRO).data,
            CONF_HELD_WEBHOOK_REV: "0123abcd",
        },
        version=1,
        minor_version=2,
        unique_id="test_device_123",
    )

    await _setup(hass, entry)

    assert entry.minor_version == 3
    assert CONF_HELD_BY_EARLIER_RELEASE not in entry.data
    assert entry.data[CONF_HELD_WEBHOOK_REV] == "0123abcd"
