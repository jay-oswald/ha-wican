import sys
import types
import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
import importlib.util
import os


def load_wican_module():
    # Stub aiohttp to avoid real dependency
    aiohttp_mod = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class ClientSession:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, *args, **kwargs):
            class _Resp:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

                async def json(self, content_type=None):
                    return {}

                @property
                def status(self):
                    return 200

            return _Resp()

    aiohttp_mod.ClientError = ClientError
    aiohttp_mod.ClientTimeout = ClientTimeout
    aiohttp_mod.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp_mod

    # Load module from file
    repo_root = os.path.dirname(os.path.dirname(__file__))
    name = "custom_components.wican.wican"
    file_path = os.path.join(repo_root, "custom_components", "wican", "wican.py")
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


@pytest.mark.asyncio
async def test_check_status_timeout_returns_false(monkeypatch):
    wican_mod = load_wican_module()
    WiCan = getattr(wican_mod, "WiCan")
    api = WiCan("1.2.3.4")

    async def raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(api, "call", raise_timeout, raising=True)

    ok = await api.check_status()
    assert ok is False


@pytest.mark.asyncio
async def test_get_pid_timeout_returns_false(monkeypatch):
    wican_mod = load_wican_module()
    WiCan = getattr(wican_mod, "WiCan")
    api = WiCan("1.2.3.4")

    async def raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(api, "call", raise_timeout, raising=True)

    ok = await api.get_pid()
    assert ok is False


def load_coordinator_module():
    # Minimal HA stubs required by coordinator
    ha_pkg = types.ModuleType("homeassistant")
    const_mod = types.ModuleType("homeassistant.const")
    const_mod.CONF_SCAN_INTERVAL = "scan_interval"
    const_mod.CONF_IP_ADDRESS = "ip_address"
    class Platform:
        BINARY_SENSOR = "binary_sensor"
        SENSOR = "sensor"
    const_mod.Platform = Platform

    core_mod = types.ModuleType("homeassistant.core")
    class HomeAssistant:
        pass
    def callback(func):
        return func
    core_mod.HomeAssistant = HomeAssistant
    core_mod.callback = callback

    exceptions_mod = types.ModuleType("homeassistant.exceptions")
    class ConfigEntryNotReady(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__(*args)
    exceptions_mod.ConfigEntryNotReady = ConfigEntryNotReady

    helpers_pkg = types.ModuleType("homeassistant.helpers")
    helpers_update_coordinator_mod = types.ModuleType("homeassistant.helpers.update_coordinator")
    class DataUpdateCoordinator:
        def __init__(self, *a, **kw):
            self.data = {}
    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
        async def async_added_to_hass(self):
            pass
        def async_write_ha_state(self):
            pass
    helpers_update_coordinator_mod.DataUpdateCoordinator = DataUpdateCoordinator
    helpers_update_coordinator_mod.CoordinatorEntity = CoordinatorEntity

    helpers_storage_mod = types.ModuleType("homeassistant.helpers.storage")
    class Store:
        def __init__(self, hass, version: int, key: str):
            self.hass = hass
            self.version = version
            self.key = key
            self.data = None
        async def async_load(self):
            return self.data
        async def async_save(self, data):
            self.data = data
    helpers_storage_mod.Store = Store

    util_pkg = types.ModuleType("homeassistant.util")
    dt_mod = types.ModuleType("homeassistant.util.dt")
    from datetime import datetime, timezone
    def utcnow():
        return datetime.now(timezone.utc)
    def parse_datetime(s: str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    dt_mod.utcnow = utcnow
    dt_mod.parse_datetime = parse_datetime

    sys.modules["homeassistant"] = ha_pkg
    sys.modules["homeassistant.const"] = const_mod
    sys.modules["homeassistant.core"] = core_mod
    sys.modules["homeassistant.exceptions"] = exceptions_mod
    sys.modules["homeassistant.helpers"] = helpers_pkg
    sys.modules["homeassistant.helpers.update_coordinator"] = helpers_update_coordinator_mod
    sys.modules["homeassistant.helpers.storage"] = helpers_storage_mod
    sys.modules["homeassistant.util"] = util_pkg
    sys.modules["homeassistant.util.dt"] = dt_mod

    # Create package shells to avoid importing __init__.py
    repo_root = os.path.dirname(os.path.dirname(__file__))
    cc_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    if not hasattr(cc_pkg, "__path__"):
        cc_pkg.__path__ = [os.path.join(repo_root, "custom_components")]
    wican_pkg = sys.modules.setdefault("custom_components.wican", types.ModuleType("custom_components.wican"))
    wican_pkg.__path__ = [os.path.join(repo_root, "custom_components", "wican")]

    # Load coordinator
    name = "custom_components.wican.coordinator"
    file_path = os.path.join(repo_root, "custom_components", "wican", "coordinator.py")
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


class DummyEntry:
    def __init__(self, entry_id: str = "e1"):
        self.entry_id = entry_id
        self.data = {}
        self.options = {}


@pytest.mark.asyncio
async def test_coordinator_stale_on_timeout(monkeypatch):
    # Load modules
    wican_mod = load_wican_module()
    WiCan = getattr(wican_mod, "WiCan")
    coord_mod = load_coordinator_module()
    WiCanCoordinator = getattr(coord_mod, "WiCanCoordinator")

    # Build API and coordinator
    api = WiCan("1.2.3.4")
    hass = object()
    coordinator = WiCanCoordinator(hass, DummyEntry(), api)

    # First, populate memory with success without hitting network
    status_ok = {"device_id": "devT", "ecu_status": "online", "hw_version": "1", "sta_ip": "1.2.3.4", "fw_version": "1"}
    pid_ok = {"A": {"class": "none", "unit": "none", "value": 1}}

    async def fake_status():
        return status_ok

    async def fake_pid():
        return pid_ok

    monkeypatch.setattr(api, "check_status", fake_status, raising=True)
    monkeypatch.setattr(api, "get_pid", fake_pid, raising=True)
    coordinator.data = await coordinator.get_data()

    # Now cause timeouts by raising from call (used by real check_status)
    async def raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    # Restore real check_status and patch call to raise
    monkeypatch.setattr(api, "call", raise_timeout, raising=True)
    # Bind the original method to the instance
    bound_check_status = WiCan.check_status.__get__(api, WiCan)
    monkeypatch.setattr(api, "check_status", bound_check_status, raising=True)

    data2 = await coordinator.get_data()
    assert data2 == coordinator.data
    assert coordinator.stale() is True
