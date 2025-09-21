import sys
import types
from datetime import datetime, timezone
from typing import Any

import pytest
import importlib.util
import os


# Create minimal stubs for Home Assistant modules to avoid heavy dependencies.
ha_pkg = types.ModuleType("homeassistant")

const_mod = types.ModuleType("homeassistant.const")
const_mod.CONF_SCAN_INTERVAL = "scan_interval"
const_mod.CONF_IP_ADDRESS = "ip_address"


class Platform:
    BINARY_SENSOR = "binary_sensor"
    SENSOR = "sensor"


const_mod.Platform = Platform

core_mod = types.ModuleType("homeassistant.core")


class HomeAssistant:  # simple stub
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
helpers_update_coordinator_mod = types.ModuleType(
    "homeassistant.helpers.update_coordinator"
)


class DataUpdateCoordinator:  # minimal stub for inheritance only
    def __init__(self, hass, logger, name: 'str | None' = None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data: dict[str, Any] = {}


helpers_update_coordinator_mod.DataUpdateCoordinator = DataUpdateCoordinator

helpers_storage_mod = types.ModuleType("homeassistant.helpers.storage")


class Store:  # will be monkeypatched in tests
    def __init__(self, hass, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self):
        return None

    async def async_save(self, data):
        return None


helpers_storage_mod.Store = Store

util_pkg = types.ModuleType("homeassistant.util")
dt_mod = types.ModuleType("homeassistant.util.dt")


def utcnow():
    return datetime.now(timezone.utc)


def parse_datetime(value: str):
    try:
        # Accept both ISO formats with Z and with offset
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


dt_mod.utcnow = utcnow
dt_mod.parse_datetime = parse_datetime

# Wire up the package structure in sys.modules
sys.modules["homeassistant"] = ha_pkg
sys.modules["homeassistant.const"] = const_mod
sys.modules["homeassistant.core"] = core_mod
sys.modules["homeassistant.exceptions"] = exceptions_mod
config_entries_mod = types.ModuleType("homeassistant.config_entries")


class ConfigEntry:
    pass


config_entries_mod.ConfigEntry = ConfigEntry
sys.modules["homeassistant.config_entries"] = config_entries_mod
sys.modules["homeassistant.helpers"] = helpers_pkg
sys.modules["homeassistant.helpers.update_coordinator"] = (
    helpers_update_coordinator_mod
)
sys.modules["homeassistant.helpers.storage"] = helpers_storage_mod
sys.modules["homeassistant.util"] = util_pkg
sys.modules["homeassistant.util.dt"] = dt_mod

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


def load_coordinator_module():
    """Dynamically load the coordinator module without importing package __init__."""
    name = "custom_components.wican.coordinator"
    if name in sys.modules:
        return sys.modules[name]

    # Ensure package stubs exist with proper __path__ to real files
    repo_root = os.path.dirname(os.path.dirname(__file__))
    cc_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    if not hasattr(cc_pkg, "__path__"):
        cc_pkg.__path__ = [os.path.join(repo_root, "custom_components")]
    wican_pkg = sys.modules.setdefault("custom_components.wican", types.ModuleType("custom_components.wican"))
    wican_pkg.__path__ = [os.path.join(repo_root, "custom_components", "wican")]

    file_path = os.path.join(repo_root, "custom_components", "wican", "coordinator.py")
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


class FakeStore:
    """In-memory fake Store for testing snapshot persistence."""

    def __init__(self, hass: HomeAssistant, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.data: Any = None
        self.save_calls = 0
        self.load_calls = 0
        self.load_return: Any = None
        self.load_exception: Any = None

    async def async_load(self):
        self.load_calls += 1
        if self.load_exception:
            raise self.load_exception
        return self.load_return

    async def async_save(self, data):
        self.save_calls += 1
        self.data = data


class DummyEntry:
    def __init__(self, entry_id: str = "test_entry") -> None:
        self.entry_id = entry_id
        self.data = {}
        self.options = {}


class FakeAPI:
    def __init__(self, status, pid, ip: str = "1.2.3.4") -> None:
        self._status = status
        self._pid = pid
        self.ip = ip

    async def check_status(self):
        return self._status

    async def get_pid(self):
        return self._pid


@pytest.fixture
def hass():
    return HomeAssistant()


@pytest.mark.asyncio
async def test_persist_snapshot_on_successful_refresh(hass: HomeAssistant, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    status = {
        "device_id": "dev123",
        "ecu_status": "online",
        "hw_version": "1.0",
        "sta_ip": "1.2.3.4",
        "fw_version": "0.0.1",
    }
    pid = {"SOC_BMS": {"class": "battery", "unit": "%", "value": 42}}

    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    coordinator = WiCanCoordinator(hass, DummyEntry(), FakeAPI(status, pid))

    data = await coordinator.get_data()

    # Persisted once with expected content
    store: FakeStore = coordinator._store  # type: ignore[attr-defined]
    assert store.save_calls == 1
    assert isinstance(store.data, dict)
    assert store.data["device_id"] == "dev123"
    assert store.data["status"] == status
    assert store.data["pid"] == pid
    # Timestamp is ISO8601
    assert isinstance(store.data["timestamp"], str)
    assert dt_util.parse_datetime(store.data["timestamp"]) is not None
    # Returned data mirrors live
    assert data["status"] == status
    assert data["pid"] == pid


@pytest.mark.asyncio
async def test_offline_uses_snapshot(hass: HomeAssistant, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    snapshot = {
        "device_id": "dev999",
        "status": {"device_id": "dev999", "ecu_status": "offline"},
        "pid": {"X": {"class": "none", "unit": "none", "value": 0}},
        "timestamp": dt_util.utcnow().isoformat(),
    }

    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    coordinator = WiCanCoordinator(hass, DummyEntry(), FakeAPI(False, False))
    # Preload the store with a snapshot to be returned
    store: FakeStore = coordinator._store  # type: ignore[attr-defined]
    store.load_return = snapshot

    data = await coordinator.get_data()

    assert data["status"] == snapshot["status"]
    assert data["pid"] == snapshot["pid"]


@pytest.mark.asyncio
async def test_corrupted_snapshot_logs_and_fallback(hass: HomeAssistant, monkeypatch, caplog):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    coordinator = WiCanCoordinator(hass, DummyEntry(), FakeAPI(False, False))
    store: FakeStore = coordinator._store  # type: ignore[attr-defined]
    store.load_exception = ValueError("corrupt")

    from homeassistant.exceptions import ConfigEntryNotReady

    with caplog.at_level("WARNING"):
        with pytest.raises(ConfigEntryNotReady):
            await coordinator.get_data()
    assert any("Failed to load WiCAN snapshot" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_debounced_writes(hass: HomeAssistant, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    status = {"device_id": "debounce", "ecu_status": "online"}
    pid = {"Y": {"class": "none", "unit": "none", "value": 1}}

    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    coordinator = WiCanCoordinator(hass, DummyEntry(), FakeAPI(status, pid))
    # Speed up the test by keeping default debounce interval and calling twice quickly
    await coordinator.get_data()
    await coordinator.get_data()

    store: FakeStore = coordinator._store  # type: ignore[attr-defined]
    assert store.save_calls == 1
