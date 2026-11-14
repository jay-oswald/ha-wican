"""The GPS track: the save_gpx service against the simulator's GPX route."""

from __future__ import annotations

import os
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.wican import merge_gpx_documents
from custom_components.wican.const import DOMAIN, SERVICE_SAVE_GPX
from tests.device_sim import MeatPiDeviceSimulator

GPX_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gpx version="1.1" creator="WiCAN PRO" xmlns="http://www.topografix.com/GPX/1/1">\n'
    "<metadata><name>WiCAN track</name><time>2026-11-06T03:00:00.000Z</time></metadata>\n"
    "<trk><name>WiCAN track</name>\n"
)


def _doc(points: list[tuple[float, float]], cursor: str, more: bool) -> str:
    body = "<trkseg>\n" + "".join(
        f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><time>2026-11-06T03:00:{i:02d}.000Z</time></trkpt>\n'
        for i, (lat, lon) in enumerate(points)
    ) + "</trkseg>\n"
    return (
        GPX_HEAD + body + "</trk>\n</gpx>\n"
        f'<!-- cursor="{cursor}" more="{"true" if more else "false"}" points="{len(points)}" -->\n'
    )


@pytest.fixture
def mock_capability_probe() -> None:
    """Override conftest's autouse probe stub: run the REAL API client."""
    return


async def _device(hass: HomeAssistant, hass_client: Any, aioclient_mock: Any) -> MeatPiDeviceSimulator:
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup()
    await sim.push_and_settle({**sim.status(), **sim.pids({"SOC": 71})})
    return sim


def _ha_device_id(hass: HomeAssistant, entry_id: str) -> str:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
    assert devices, "the entry has a device"
    return devices[0].id


async def test_save_gpx_writes_the_track_under_www(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """Two device pages become one file under www/wican with every point."""
    sim = await _device(hass, hass_client, aioclient_mock)
    sim.api.gpx_documents = [
        _doc([(-25.3, 131.0), (-25.31, 131.01)], "1793900000:200", True),
        _doc([(-25.32, 131.02)], "1793900000:300", False),
    ]

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SAVE_GPX,
        {"device_id": _ha_device_id(hass, sim.entry.entry_id), "hours": 6},
        blocking=True,
        return_response=True,
    )

    assert response["points"] == 3
    assert response["url"].startswith("/local/wican/")
    path = response["path"]
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert text.count("<trkpt ") == 3
    assert text.count("<trkseg>") == 2
    assert text.rstrip().endswith("</gpx>")
    assert "<!--" not in text
    # the second page started at the first page's cursor; the first at the window
    assert sim.api.gpx_calls[1]["since"] == "1793900000:200"
    assert sim.api.gpx_calls[0]["since"].isdigit()
    assert sim.api.gpx_calls[0]["limit"] == "4000"


async def test_save_gpx_without_points_or_route_raises(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> None:
    """No points in the window, or a firmware without the route: an error, no file."""
    sim = await _device(hass, hass_client, aioclient_mock)
    device_id = _ha_device_id(hass, sim.entry.entry_id)
    folder = hass.config.path("www", "wican")
    before = set(os.listdir(folder)) if os.path.isdir(folder) else set()

    sim.api.gpx_documents = [_doc([], "1793900000:0", False)]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SAVE_GPX, {"device_id": device_id}, blocking=True, return_response=True,
        )

    sim.api.gpx_documents = None
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SAVE_GPX, {"device_id": device_id}, blocking=True, return_response=True,
        )
    after = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    assert after == before  # nothing written


def test_merge_gpx_documents() -> None:
    """One document from several, the device's trailers dropped."""
    merged = merge_gpx_documents(
        [_doc([(1.0, 2.0)], "a:1", True), _doc([(3.0, 4.0)], "a:2", False)],
    )
    assert merged.count("<trkseg>") == 2
    assert merged.count("<trkpt ") == 2
    assert merged.count("</gpx>") == 1
    assert "cursor=" not in merged
    assert merge_gpx_documents([]) == ""
