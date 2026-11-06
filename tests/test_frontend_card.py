"""The GPS track card is served and registered as a frontend resource."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.const import DATA_FRONTEND_REGISTERED, GPX_CARD_FILE

from tests.test_history import _entry_with_api


async def test_gpx_card_is_served_and_registered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_client,
) -> None:
    """Setting up an entry serves the card file and adds it as an extra module URL."""
    assert await async_setup_component(hass, "frontend", {})
    await _entry_with_api(hass, mock_config_entry)

    assert hass.data.get(DATA_FRONTEND_REGISTERED) is True
    card = Path(__file__).parent.parent / "custom_components" / "wican" / "www" / GPX_CARD_FILE
    assert card.is_file()
    text = card.read_text(encoding="utf-8")
    assert 'const TAG = "wican-gpx-card"' in text
    assert "customElements.define(TAG, WicanGpxCard)" in text
    urls = hass.data[DATA_EXTRA_MODULE_URL].urls
    assert any(u.startswith("/wican-static/" + GPX_CARD_FILE) for u in urls)


async def test_gpx_card_is_served_as_javascript(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_client,
) -> None:
    """The file comes with a JavaScript content type (a browser refuses a
    module served as text) and nothing else under the prefix is served."""
    assert await async_setup_component(hass, "frontend", {})
    await _entry_with_api(hass, mock_config_entry)
    client = await hass_client()
    reply = await client.get("/wican-static/" + GPX_CARD_FILE)
    assert reply.status == 200
    assert reply.headers["Content-Type"].startswith("text/javascript")
    assert "wican-gpx-card" in await reply.text()
    other = await client.get("/wican-static/__init__.py")
    assert other.status == 404


async def test_gpx_card_without_frontend_is_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A Home Assistant without the frontend sets the entry up all the same."""
    await _entry_with_api(hass, mock_config_entry)
    assert not hass.data.get(DATA_FRONTEND_REGISTERED)


async def test_gpx_card_registers_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    hass_client,
) -> None:
    """A second entry does not add the resource twice."""
    assert await async_setup_component(hass, "frontend", {})
    await _entry_with_api(hass, mock_config_entry)
    second = MockConfigEntry(
        domain="wican",
        data=dict(mock_config_entry.data, host="10.0.0.9"),
        unique_id="second-device",
    )
    await _entry_with_api(hass, second)
    urls = [u for u in hass.data[DATA_EXTRA_MODULE_URL].urls if "wican-gpx-card" in u]
    assert len(urls) == 1
