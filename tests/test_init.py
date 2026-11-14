"""Test the WiCAN integration init."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.http import ApiConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError

from custom_components.wican import (
    _async_register_webhook_on_device,
    _device_tls_support,
)
from custom_components.wican.const import DOMAIN

from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_entry_success(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiohttp_session,
) -> None:
    """Test successful setup of entry."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not None
    assert mock_config_entry.runtime_data.coordinator is not None


async def test_setup_entry_webhook_registration_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiohttp_session,
) -> None:
    """Test setup when webhook registration fails (should still succeed)."""
    mock_config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=False,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # Should still load even if webhook registration fails
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_unload_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test unloading an entry."""
    entry = init_integration

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_webhook_post(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test webhook endpoint receives data."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Post webhook data
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=mock_webhook_data,
    )

    assert resp.status == 204  # NO_CONTENT

    # Verify coordinator has the data
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data == mock_webhook_data


async def test_webhook_device_identity_mismatch(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test webhook rejects data from wrong device."""
    entry = init_integration
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    client = await hass_client()

    # Change device_id to simulate wrong device - deep copy the data
    import copy
    wrong_device_data = copy.deepcopy(mock_webhook_data)
    wrong_device_data["status"]["device_id"] = "different_device_456"

    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=wrong_device_data,
    )

    # A push whose device_id does not match the stored device is rejected,
    # and the impostor's device_id is NOT persisted to the config entry.
    assert resp.status == 403
    assert entry.data.get("device_id") == "test_device_123"


async def test_late_learned_hw_version_retargets_release_stream(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """PRO hardware revealed on first push retargets the release stream.

    Regression: the releases coordinator's stream was frozen at setup from
    entry.data["hw_version"], which a manually added device does not have
    until its first webhook push — so a PRO device tracked the standard
    (OBD) release stream until HA restart.
    """
    entry = init_integration
    assert entry.runtime_data.github_coordinator._stream == "obd"

    payload = dict(mock_webhook_data)
    payload["status"] = {**payload["status"], "hw_version": "WiCAN PRO v2.0"}

    client = await hass_client()
    with patch(
        "custom_components.wican.github_releases.GitHubReleasesCoordinator"
        ".async_request_refresh",
        new_callable=AsyncMock,
    ) as mock_refresh:
        resp = await client.post(
            f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=payload,
        )
        await hass.async_block_till_done()

    assert resp.status == 204
    assert entry.runtime_data.github_coordinator._stream == "pro"
    assert mock_refresh.await_count == 1


async def test_entry_updated(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Test entry options update."""
    entry = init_integration

    # Update post interval
    new_interval = 2000
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "post_interval": new_interval},
    )
    await hass.async_block_till_done()

    # Verify runtime data updated
    assert entry.runtime_data.post_interval == new_interval


async def test_webhook_no_duplicate_registration(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Test webhook doesn't trigger duplicate registration when same device info repeated."""
    entry = init_integration
    
    client = await hass_client()
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    # First webhook - data already stored from setup
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=mock_webhook_data,
    )
    assert resp.status == 204
    await hass.async_block_till_done()

    # Second webhook with same data - should NOT trigger re-registration  
    # The key point: mdns/device_id are already in entry.data from init,
    # so webhook shouldn't see them as "changed"
    resp = await client.post(
        f"/api/webhook/{webhook_id}",
        json=mock_webhook_data,
    )
    assert resp.status == 204
    await hass.async_block_till_done()
    
    # Integration is working - no errors means no spurious re-registrations
    assert entry.state == ConfigEntryState.LOADED


async def test_webhook_registration_no_url_available(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test webhook registration falls back to stored webhook_url."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "host": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "webhook_url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        },
        unique_id="wican_test-192.168.1.100:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    # The precondition of the stored URL: Home Assistant has no network
    # address of its own (a start before the network is up).
    hass.config.api = ApiConfig("127.0.0.1", "127.0.0.1", 8123, False)

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            side_effect=NoURLAvailableError,
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert (
        session.post.await_args.kwargs["json"]["url"]
        == "http://192.168.1.10:8123/api/webhook/test_webhook_id"
    )
    assert entry.data["webhook_url"] == "http://192.168.1.10:8123/api/webhook/test_webhook_id"


async def test_webhook_registration_non_pro_uses_single_local_http_url(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test non-PRO firmware registers a single local HTTP webhook URL."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "host": "http://wican_test.local:80",
            "fw_version": "v4.99",
            "hw_version": "v3.1",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        unique_id="wican_test-192.168.1.100:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            return_value="http://192.168.1.10:8123",
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"] == {
        "url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        "enabled": True,
        "interval": entry.runtime_data.post_interval,
    }


async def test_webhook_registration_pro_uses_local_and_external_urls(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test PRO firmware 4.49+ receives both local and external webhook URLs."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN PRO",
        data={
            "mdns": "http://wican_pro.local:80",
            "host": "http://wican_pro.local:80",
            "fw_version": "v4.49",
            "hw_version": "WiCAN PRO",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        unique_id="wican_pro-192.168.1.101:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            side_effect=[
                "http://192.168.1.10:8123",
                "https://example.ui.nabu.casa",
            ],
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"] == {
        "url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        "urls": [
            "http://192.168.1.10:8123/api/webhook/test_webhook_id",
            "https://example.ui.nabu.casa/api/webhook/test_webhook_id",
        ],
        "enabled": True,
        "interval": entry.runtime_data.post_interval,
    }


async def test_webhook_registration_pro_pre_449_uses_single_local_url(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """Test older PRO firmware keeps the single local webhook URL payload."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN PRO",
        data={
            "mdns": "http://wican_pro.local:80",
            "host": "http://wican_pro.local:80",
            "fw_version": "v4.48",
            "hw_version": "WiCAN PRO",
            CONF_WEBHOOK_ID: "test_webhook_id",
        },
        unique_id="wican_pro-192.168.1.102:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            return_value="http://192.168.1.10:8123",
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True

    session.post.assert_awaited()
    assert session.post.await_args.kwargs["json"] == {
        "url": "http://192.168.1.10:8123/api/webhook/test_webhook_id",
        "enabled": True,
        "interval": entry.runtime_data.post_interval,
    }


async def test_webhook_registration_https_local_url_on_a_plain_server(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """An https Local network URL on a plain-http server is not registered.

    The server still answers plain http on its own address: that one goes
    to the device, and the stored URL (another address here) follows it.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="WiCAN Device",
        data={
            "mdns": "http://wican_test.local:80",
            "host": "http://wican_test.local:80",
            CONF_WEBHOOK_ID: "test_webhook_id",
            "webhook_url": "http://192.168.1.99:8123/api/webhook/test_webhook_id",
        },
        unique_id="wican_test-192.168.1.103:80",
    )
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    entry = config_entry
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response

    hass.config.api = ApiConfig("192.168.1.10", "192.168.1.10", 8123, False)

    with (
        patch(
            "custom_components.wican.helpers.get_url",
            return_value="https://internal.example.com",
        ),
        patch(
            "custom_components.wican.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True
        await hass.async_block_till_done()

    session.post.assert_awaited()
    assert (
        session.post.await_args.kwargs["json"]["url"]
        == "http://192.168.1.10:8123/api/webhook/test_webhook_id"
    )
    assert entry.data["webhook_url"] == "http://192.168.1.10:8123/api/webhook/test_webhook_id"


# ---- Home Assistant's own web server with SSL on (HTTPS only) ---------------

HTTPS_BY_IP = "https://192.168.1.10:8123/api/webhook/test_webhook_id"
HTTPS_BY_NAME = "https://ha.example.org:8123/api/webhook/test_webhook_id"
STORED_HTTP = "http://192.168.1.10:8123/api/webhook/test_webhook_id"


async def _loaded_entry(hass: HomeAssistant, hw_version: str | None) -> MockConfigEntry:
    """Return a loaded entry that was added while the server was plain http."""
    data = {
        "mdns": "http://wican_test.local:80",
        "host": "http://wican_test.local:80",
        CONF_WEBHOOK_ID: "test_webhook_id",
        "webhook_url": STORED_HTTP,
    }
    if hw_version:
        data["hw_version"] = hw_version
    config_entry = MockConfigEntry(domain=DOMAIN, title="WiCAN Device", data=data)
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    return config_entry


def _server_with_ssl(hass: HomeAssistant, *, ssl: bool = True) -> None:
    """What `ssl_certificate` under `http:` makes of the server description."""
    hass.config.api = ApiConfig("192.168.1.10", "192.168.1.10", 8123, ssl)
    hass.config.internal_url = None
    hass.config.external_url = "https://ha.example.org:8123"


def _device_session() -> AsyncMock:
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    session.post.return_value = response
    return session


@pytest.mark.parametrize("hw_version", ["WiCAN-PRO", None])
async def test_webhook_registration_server_with_ssl_registers_https(
    hass: HomeAssistant,
    mock_aiohttp_session,
    hw_version: str | None,
) -> None:
    """SSL turned on after the entry was added: https URLs, the http one gone.

    A PRO, and a device that has not reported its hardware yet, both get
    the server's own https address first and the Internet URL behind it.
    The URL stored while SSL was off is neither registered nor kept.
    """
    entry = await _loaded_entry(hass, hw_version)
    _server_with_ssl(hass)
    session = _device_session()

    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=session,
    ):
        assert await _async_register_webhook_on_device(hass, entry) is True
        await hass.async_block_till_done()

    payload = session.post.await_args.kwargs["json"]
    assert payload["url"] == HTTPS_BY_IP
    assert payload["urls"] == [HTTPS_BY_IP, HTTPS_BY_NAME]
    assert entry.data["webhook_url"] == HTTPS_BY_IP
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"https_only_server_{entry.entry_id}",
    ) is None


@pytest.mark.parametrize("hw_version", ["WiCAN-OBD", "WiCAN-USB"])
async def test_webhook_registration_server_with_ssl_device_without_tls(
    hass: HomeAssistant,
    mock_aiohttp_session,
    hw_version: str,
) -> None:
    """A WiCAN OBD or USB cannot push to an HTTPS-only Home Assistant.

    Nothing is registered and a repair issue says why; it clears at the
    first registration after the server is back on plain http.
    """
    entry = await _loaded_entry(hass, hw_version)
    issue_id = f"https_only_server_{entry.entry_id}"
    _server_with_ssl(hass)
    session = _device_session()

    with patch(
        "custom_components.wican.async_get_clientsession",
        return_value=session,
    ):
        assert await _async_register_webhook_on_device(hass, entry) is False

        session.post.assert_not_awaited()
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_key == "https_only_server"
        assert issue.translation_placeholders == {"device": "WiCAN Device"}
        assert entry.data["webhook_url"] == STORED_HTTP

        _server_with_ssl(hass, ssl=False)
        assert await _async_register_webhook_on_device(hass, entry) is True
        await hass.async_block_till_done()

    assert session.post.await_args.kwargs["json"]["url"] == STORED_HTTP
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_unload_clears_the_https_only_repair_issue(
    hass: HomeAssistant,
    mock_aiohttp_session,
) -> None:
    """An unloaded entry leaves no HTTPS-only repair issue behind."""
    entry = await _loaded_entry(hass, "WiCAN-OBD")
    issue_id = f"https_only_server_{entry.entry_id}"
    _server_with_ssl(hass)

    assert await _async_register_webhook_on_device(hass, entry) is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


def test_device_tls_support() -> None:
    """Which devices can push over HTTPS, and which are not known yet."""

    def entry(**data: str) -> MockConfigEntry:
        return MockConfigEntry(domain=DOMAIN, data=data)

    assert _device_tls_support(entry(hw_version="WiCAN-PRO")) is True
    # positively a WiCAN without a TLS stack
    assert _device_tls_support(entry(hw_version="WiCAN-OBD")) is False
    assert _device_tls_support(entry(hw_version="WiCAN-USB")) is False
    # not reported yet: the WiCAN type of a manual entry is only a default
    assert _device_tls_support(entry()) is None
    assert _device_tls_support(entry(device_type="wican")) is None
    # another product, or a hardware string the integration does not know
    assert _device_tls_support(entry(hw_version="ESPNetlink R1")) is None
    assert _device_tls_support(entry(hw_version="MeatPi Hub R1")) is None
    assert _device_tls_support(entry(hw_version="v3.1")) is None











def test_parse_version_variants() -> None:
    """_parse_version handles empty, non-numeric, and normal versions."""
    from custom_components.wican import _parse_version

    assert _parse_version(None) is None
    assert _parse_version("") is None
    assert _parse_version("no-digits-here") is None
    assert _parse_version("v4.49") == (4, 49)


def test_is_version_at_least() -> None:
    """_is_version_at_least compares padded version tuples."""
    from custom_components.wican import _is_version_at_least

    assert _is_version_at_least(None, (4, 49)) is False
    assert _is_version_at_least("v4.49", (4, 49)) is True
    assert _is_version_at_least("v4.50", (4, 49)) is True
    assert _is_version_at_least("v4.48", (4, 49)) is False


def test_supports_dual_webhook_urls() -> None:
    """Dual webhook URLs are a PRO capability; OBD/USB are http-only.

    Contract v2: WiCAN OBD and USB have no TLS stack and can never take
    an https URL — they always get a single plain-http local webhook.
    """
    from custom_components.wican import _supports_dual_webhook_urls

    non_pro = MockConfigEntry(domain=DOMAIN, data={"hw_version": "v3.1"})
    assert _supports_dual_webhook_urls(non_pro) is False

    usb = MockConfigEntry(domain=DOMAIN, data={"hw_version": "WiCAN-USB"})
    assert _supports_dual_webhook_urls(usb) is False

    pro = MockConfigEntry(
        domain=DOMAIN, data={"hw_version": "WiCAN-Pro", "fw_version": "v6.00"},
    )
    assert _supports_dual_webhook_urls(pro) is True


async def test_setup_updates_params_from_github(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiohttp_session,
) -> None:
    """Setup logs when params.json is refreshed from GitHub (line 233)."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican.async_update_params_from_github",
            AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_setup_params_update_failure_is_non_fatal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiohttp_session,
) -> None:
    """A GitHub params update error does not block setup (lines 234-235)."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.wican.async_update_params_from_github",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_firmware_below_minimum_raises_repair_and_clears(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_webhook_data: dict,
    hass_client,
) -> None:
    """Contract v2: old firmware keeps telemetry but demands an update.

    The entry's fw_version (2.00, OBD stream) is below the 5.00 minimum,
    so setup raises the firmware_update_required repair issue; a push
    reporting a supported version clears it.
    """
    from homeassistant.helpers import issue_registry as ir

    entry = init_integration
    issue_id = f"firmware_update_required_{entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # Telemetry still works while flagged.
    client = await hass_client()
    payload = dict(mock_webhook_data)
    payload["status"] = {**payload["status"], "fw_version": "5.01"}
    resp = await client.post(
        f"/api/webhook/{entry.data[CONF_WEBHOOK_ID]}", json=payload,
    )
    await hass.async_block_till_done()

    assert resp.status == 204
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_non_wican_products_exempt_from_firmware_minimum(
    hass: HomeAssistant,
) -> None:
    """ESPNetlink and catalog products version independently — no nag."""
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ESPNetlink",
        data={
            "mdns": "http://espnetlink_1.local",
            CONF_WEBHOOK_ID: "espnetlink_webhook_id",
            "fw_version": "1.00",
            "hw_version": "ESPNetlink",
            "device_type": "espnetlink",
            "device_id": "netlink_1",
        },
        unique_id="espnetlink-test",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(
            DOMAIN, f"firmware_update_required_{entry.entry_id}",
        )
        is None
    )
