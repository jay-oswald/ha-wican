import sys
import types
from datetime import datetime, timezone
from typing import Any

import pytest
import importlib.util
import os


# Minimal HA stubs
ha_pkg = types.ModuleType("homeassistant")

const_mod = types.ModuleType("homeassistant.const")
const_mod.CONF_SCAN_INTERVAL = "scan_interval"

core_mod = types.ModuleType("homeassistant.core")


def callback(func):
    return func


core_mod.callback = callback
core_mod.HomeAssistant = object

helpers_pkg = types.ModuleType("homeassistant.helpers")
helpers_update_coordinator_mod = types.ModuleType(
    "homeassistant.helpers.update_coordinator"
)


class DataUpdateCoordinator:
    def __init__(self, hass=None, logger=None, name=None, update_interval=None) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = {}


class CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        pass

    def async_write_ha_state(self) -> None:
        # no-op for tests
        pass


helpers_update_coordinator_mod.DataUpdateCoordinator = DataUpdateCoordinator
helpers_update_coordinator_mod.CoordinatorEntity = CoordinatorEntity

helpers_restore_state_mod = types.ModuleType("homeassistant.helpers.restore_state")


class RestoreEntity:
    async def async_get_last_state(self):
        # Tests can set _fake_last_state on the entity instance
        return getattr(self, "_fake_last_state", None)


helpers_restore_state_mod.RestoreEntity = RestoreEntity

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

# Wire stubs
sys.modules["homeassistant"] = ha_pkg
sys.modules["homeassistant.const"] = const_mod
sys.modules["homeassistant.core"] = core_mod
sys.modules["homeassistant.helpers"] = helpers_pkg
sys.modules["homeassistant.helpers.update_coordinator"] = (
    helpers_update_coordinator_mod
)
helpers_storage_mod = types.ModuleType("homeassistant.helpers.storage")


class Store:
    def __init__(self, hass, version: int, key: str) -> None:
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self):
        return None

    async def async_save(self, data):
        return None


helpers_storage_mod.Store = Store
sys.modules["homeassistant.helpers.storage"] = helpers_storage_mod
exceptions_mod = types.ModuleType("homeassistant.exceptions")


class ConfigEntryNotReady(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
exceptions_mod.ConfigEntryNotReady = ConfigEntryNotReady
sys.modules["homeassistant.exceptions"] = exceptions_mod
sys.modules["homeassistant.helpers.restore_state"] = helpers_restore_state_mod
sys.modules["homeassistant.util"] = util_pkg
sys.modules["homeassistant.util.dt"] = dt_mod


def load_modules():
    repo_root = os.path.dirname(os.path.dirname(__file__))
    # Ensure package namespace exists
    cc_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    if not hasattr(cc_pkg, "__path__"):
        cc_pkg.__path__ = [os.path.join(repo_root, "custom_components")]
    wican_pkg = sys.modules.setdefault("custom_components.wican", types.ModuleType("custom_components.wican"))
    wican_pkg.__path__ = [os.path.join(repo_root, "custom_components", "wican")]

    # Load coordinator first (entity imports it for type only)
    name_coord = "custom_components.wican.coordinator"
    file_coord = os.path.join(repo_root, "custom_components", "wican", "coordinator.py")
    spec = importlib.util.spec_from_file_location(name_coord, file_coord)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name_coord] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]

    # Ensure the update_coordinator stub exposes CoordinatorEntity
    ucm = sys.modules.get("homeassistant.helpers.update_coordinator")
    if ucm is not None and not hasattr(ucm, "CoordinatorEntity"):
        setattr(ucm, "CoordinatorEntity", CoordinatorEntity)

    # Now load entity
    name_ent = "custom_components.wican.entity"
    file_ent = os.path.join(repo_root, "custom_components", "wican", "entity.py")
    spec2 = importlib.util.spec_from_file_location(name_ent, file_ent)
    mod2 = importlib.util.module_from_spec(spec2)
    sys.modules[name_ent] = mod2
    assert spec2 and spec2.loader
    spec2.loader.exec_module(mod2)  # type: ignore[attr-defined]
    return mod, mod2


class DummyCoordinator:
    def __init__(self) -> None:
        self._stale = True
        self.data = {"status": {"device_id": "dev111", "hw_version": "1", "sta_ip": "1.2.3.4", "fw_version": "1"}}
        self._available = True
        self.last_successful_update = None

    def stale(self) -> bool:
        return self._stale

    def available(self) -> bool:
        return self._available

    def get_status(self, key):
        return self.data["status"].get(key)

    def get_pid_value(self, key):
        return False

    def device_info(self):
        return {
            "identifiers": {("wican", self.data["status"]["device_id"])},
            "name": "WiCAN",
            "manufacturer": "MeatPi",
            "model": self.data["status"]["hw_version"],
            "configuration_url": "http://" + self.data["status"]["sta_ip"],
            "sw_version": self.data["status"]["fw_version"],
            "hw_version": self.data["status"]["hw_version"],
        }


class LastState:
    def __init__(self, state: str) -> None:
        self.state = state


@pytest.mark.asyncio
async def test_restore_entity_state_when_stale_and_no_live_data():
    _, entity_mod = load_modules()
    WiCanPidEntity = getattr(entity_mod, "WiCanPidEntity")

    coord = DummyCoordinator()
    ent = WiCanPidEntity(coord, {"key": "SOC_BMS", "name": "SOC_BMS", "class": "none", "unit": "%"})
    # No live data -> state is False; mark stale and provide last state
    ent._fake_last_state = LastState("42")
    await ent.async_added_to_hass()

    assert ent.state == "42"
    attrs = ent.extra_state_attributes
    assert attrs["wican_data_stale"] is True
    assert attrs["last_successful_update"] is None
    # Available should be True while stale
    assert ent.available is True


@pytest.mark.asyncio
async def test_entity_updates_on_fresh_data_and_metadata():
    coord_mod, entity_mod = load_modules()
    WiCanPidEntity = getattr(entity_mod, "WiCanPidEntity")

    coord = DummyCoordinator()
    ent = WiCanPidEntity(coord, {"key": "SOC_BMS", "name": "SOC_BMS", "class": "none", "unit": "%"})
    ent._fake_last_state = LastState("41")
    await ent.async_added_to_hass()
    assert ent.state == "41"

    # Fresh update arrives
    coord._stale = False
    coord.last_successful_update = datetime.now(timezone.utc)
    coord.get_pid_value = lambda k: 55
    ent._handle_coordinator_update()
    assert ent.state == 55
    attrs = ent.extra_state_attributes
    assert attrs["wican_data_stale"] is False
    assert isinstance(attrs["last_successful_update"], str)
    assert ent.available is True
