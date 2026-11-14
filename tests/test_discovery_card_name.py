"""The discovery card names the device it found.

A discovered device waits on the integrations page as a card. Without a
title of its own the card showed the integration's name alone: the device's
name only appeared after opening it, and two discovered devices looked the
same.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.wican.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "wican" / "translations" / "en.json"
)


def _discovery(hostname: str, mac: bytes) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address="192.168.1.100",
        ip_addresses=["192.168.1.100"],
        hostname=hostname,
        name="WiCAN-WebServer._wican._tcp.local.",
        port=80,
        type="_wican._tcp.local.",
        properties={"mac": mac, "device_id": b"test_device_123"},
    )


async def _discover(hass: HomeAssistant, discovery: ZeroconfServiceInfo) -> Any:
    with patch(
        "homeassistant.components.onboarding.async_is_onboarded",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=discovery,
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    return result


def _card_names(hass: HomeAssistant) -> dict[str, Any]:
    """Flow id to the placeholders the frontend titles each card with."""
    return {
        flow["flow_id"]: flow["context"].get("title_placeholders")
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    }


async def test_discovery_card_carries_the_devices_name(hass: HomeAssistant) -> None:
    """The waiting card is titled with the name the dialog shows."""
    result = await _discover(hass, _discovery("wican_test.local.", b"AA:BB:CC:DD:EE:FF"))

    assert _card_names(hass) == {result["flow_id"]: {"name": "wican_test.local"}}
    assert result["description_placeholders"]["name"] == "wican_test.local"


async def test_two_discovered_devices_can_be_told_apart(hass: HomeAssistant) -> None:
    """Each card has its own device's name."""
    first = await _discover(hass, _discovery("wican_aaa.local.", b"AA:BB:CC:00:00:0A"))
    second = await _discover(hass, _discovery("wican_bbb.local.", b"AA:BB:CC:00:00:0B"))

    assert _card_names(hass) == {
        first["flow_id"]: {"name": "wican_aaa.local"},
        second["flow_id"]: {"name": "wican_bbb.local"},
    }


async def test_device_without_a_host_name_is_titled_by_its_service(
    hass: HomeAssistant,
) -> None:
    """Zeroconf gave no host name: the service's name stands in."""
    result = await _discover(hass, _discovery("", b"AA:BB:CC:DD:EE:FF"))

    assert _card_names(hass) == {
        result["flow_id"]: {"name": "WiCAN-WebServer._wican._tcp.local."},
    }


def test_flow_title_is_the_name_placeholder() -> None:
    """The frontend builds the card's title from this string."""
    strings = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))

    assert strings["config"]["flow_title"] == "{name}"
