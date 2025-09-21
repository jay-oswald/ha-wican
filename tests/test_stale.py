import sys
import types
from datetime import datetime, timezone
from typing import Any

import pytest
import importlib.util
import os


# Minimal HA stubs (shared with previous tests but defined locally for isolation)
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


def load_coordinator_module():
    name = "custom_components.wican.coordinator"
    if name in sys.modules:
        return sys.modules[name]

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
    def __init__(self, hass: HomeAssistant, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.data: Any = None
        self.load_return: Any = None
        self.load_exception: Any = None
        self.save_calls = 0

    async def async_load(self):
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
async def test_memory_data_offline_sets_stale(hass, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    # First success
    status = {"device_id": "dev123", "ecu_status": "online"}
    pid = {"X": {"class": "none", "unit": "none", "value": 1}}
    api = FakeAPI(status, pid)
    coordinator = WiCanCoordinator(hass, DummyEntry(), api)
    coordinator.data = await coordinator.get_data()
    assert coordinator.stale() is False
    assert coordinator.last_successful_update is not None

    # Now device goes offline
    api._status = False
    data2 = await coordinator.get_data()
    assert data2 == coordinator.data  # should serve memory
    assert coordinator.stale() is True


@pytest.mark.asyncio
async def test_snapshot_used_when_no_memory_offline(hass, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    snapshot = {
        "device_id": "dev999",
        "status": {"device_id": "dev999", "ecu_status": "offline"},
        "pid": {"Y": {"class": "none", "unit": "none", "value": 0}},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    api = FakeAPI(False, False)
    coordinator = WiCanCoordinator(hass, DummyEntry(), api)
    # Inject snapshot before first refresh
    store: FakeStore = coordinator._store  # type: ignore[attr-defined]
    store.load_return = snapshot

    data = await coordinator.get_data()
    assert data["status"] == snapshot["status"]
    assert coordinator.stale() is True
    assert coordinator.last_successful_update is None


@pytest.mark.asyncio
async def test_first_run_offline_no_snapshot_raises(hass, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    api = FakeAPI(False, False)
    coordinator = WiCanCoordinator(hass, DummyEntry(), api)

    from homeassistant.exceptions import ConfigEntryNotReady
    with pytest.raises(ConfigEntryNotReady):
        await coordinator.get_data()


@pytest.mark.asyncio
async def test_recovery_clears_stale_and_updates_timestamp(hass, monkeypatch):
    coordinator_mod = load_coordinator_module()
    monkeypatch.setattr(coordinator_mod, "Store", FakeStore, raising=True)
    WiCanCoordinator = getattr(coordinator_mod, "WiCanCoordinator")

    # Start with snapshot path
    snapshot = {
        "device_id": "dev999",
        "status": {"device_id": "dev999", "ecu_status": "offline"},
        "pid": {"Z": {"class": "none", "unit": "none", "value": 5}},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    api = FakeAPI(False, False)
    coordinator = WiCanCoordinator(hass, DummyEntry(), api)
    store: FakeStore = coordinator._store  # type: ignore[attr-defined]
    store.load_return = snapshot
    await coordinator.get_data()
    assert coordinator.stale() is True
    assert coordinator.last_successful_update is None

    # Device becomes reachable
    api._status = {"device_id": "dev999", "ecu_status": "online", "hw_version": "1", "sta_ip": "1.2.3.4", "fw_version": "1"}
    api._pid = {"Z": {"class": "none", "unit": "none", "value": 42}}
    await coordinator.get_data()
    assert coordinator.stale() is False
    assert coordinator.last_successful_update is not None
