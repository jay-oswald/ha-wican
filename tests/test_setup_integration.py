import sys
import types
import pytest
import importlib.util
import os


def make_ha_stubs():
    ha_pkg = types.ModuleType("homeassistant")

    const_mod = types.ModuleType("homeassistant.const")
    const_mod.CONF_IP_ADDRESS = "ip_address"
    const_mod.CONF_SCAN_INTERVAL = "scan_interval"

    class Platform:
        BINARY_SENSOR = "binary_sensor"
        SENSOR = "sensor"

    const_mod.Platform = Platform

    core_mod = types.ModuleType("homeassistant.core")
    class HomeAssistant:
        pass
    def callback(fn):
        return fn
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
        async def async_config_entry_first_refresh(self):
            if hasattr(self, "_async_update_data"):
                self.data = await self._async_update_data()
    class CoordinatorEntity:
        def __init__(self, coordinator):
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

    config_entries_mod = types.ModuleType("homeassistant.config_entries")
    class ConfigEntry:
        pass
    config_entries_mod.ConfigEntry = ConfigEntry

    sys.modules["homeassistant"] = ha_pkg
    sys.modules["homeassistant.const"] = const_mod
    sys.modules["homeassistant.core"] = core_mod
    sys.modules["homeassistant.exceptions"] = exceptions_mod
    sys.modules["homeassistant.helpers"] = helpers_pkg
    sys.modules["homeassistant.helpers.update_coordinator"] = helpers_update_coordinator_mod
    sys.modules["homeassistant.helpers.storage"] = helpers_storage_mod
    sys.modules["homeassistant.util"] = util_pkg
    sys.modules["homeassistant.util.dt"] = dt_mod
    sys.modules["homeassistant.config_entries"] = config_entries_mod


def load_modules_with_fakes(wican_api):
    repo_root = os.path.dirname(os.path.dirname(__file__))
    # Ensure custom_components packages exist without importing package __init__
    cc_pkg = sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    if not hasattr(cc_pkg, "__path__"):
        cc_pkg.__path__ = [os.path.join(repo_root, "custom_components")]
    wican_pkg = sys.modules.setdefault("custom_components.wican", types.ModuleType("custom_components.wican"))
    wican_pkg.__path__ = [os.path.join(repo_root, "custom_components", "wican")]

    # Provide fake wican API module
    fake_wican_mod = types.ModuleType("custom_components.wican.wican")
    class WiCan:
        def __init__(self, ip):
            self.ip = ip
        async def check_status(self):
            return await wican_api.check_status()
        async def get_pid(self):
            return await wican_api.get_pid()
    fake_wican_mod.WiCan = WiCan
    sys.modules["custom_components.wican.wican"] = fake_wican_mod

    # Load coordinator first so that __init__ pulls the same module
    name_coord = "custom_components.wican.coordinator"
    file_coord = os.path.join(repo_root, "custom_components", "wican", "coordinator.py")
    spec_c = importlib.util.spec_from_file_location(name_coord, file_coord)
    mod_c = importlib.util.module_from_spec(spec_c)
    sys.modules[name_coord] = mod_c
    assert spec_c and spec_c.loader
    spec_c.loader.exec_module(mod_c)  # type: ignore[attr-defined]

    # Load integration __init__
    name_init = "custom_components.wican.__init__"
    file_init = os.path.join(repo_root, "custom_components", "wican", "__init__.py")
    spec_i = importlib.util.spec_from_file_location(name_init, file_init)
    mod_i = importlib.util.module_from_spec(spec_i)
    sys.modules[name_init] = mod_i
    assert spec_i and spec_i.loader
    spec_i.loader.exec_module(mod_i)  # type: ignore[attr-defined]
    return mod_i, mod_c


class DummyEntry:
    def __init__(self, ip: str, entry_id: str = "e1"):
        self.entry_id = entry_id
        self.data = {"ip_address": ip}
        self.options = {}


class FakeConfigEntries:
    def __init__(self):
        self.forwarded = None
    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded = platforms


class FakeHass:
    def __init__(self):
        self.data = {}
        self.config_entries = FakeConfigEntries()


class APIGood:
    async def check_status(self):
        return {"device_id": "devS", "ecu_status": "online", "hw_version": "1", "sta_ip": "1.2.3.4", "fw_version": "1"}
    async def get_pid(self):
        return {"SOC": {"class": "none", "unit": "%", "value": 50}}


class APIOffline:
    async def check_status(self):
        return False
    async def get_pid(self):
        return False


@pytest.mark.asyncio
async def test_fresh_install_online_succeeds_and_forwards_platforms():
    make_ha_stubs()
    hass = FakeHass()
    entry = DummyEntry("1.2.3.4")
    init_mod, coord_mod = load_modules_with_fakes(APIGood())

    ok = await init_mod.async_setup_entry(hass, entry)
    assert ok is True
    assert hass.config_entries.forwarded == ["binary_sensor", "sensor"]
    # Coordinator is stored
    from custom_components.wican.const import DOMAIN
    assert DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_fresh_install_offline_raises_not_ready():
    make_ha_stubs()
    hass = FakeHass()
    entry = DummyEntry("1.2.3.4")
    init_mod, coord_mod = load_modules_with_fakes(APIOffline())
    from homeassistant.exceptions import ConfigEntryNotReady
    with pytest.raises(ConfigEntryNotReady):
        await init_mod.async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_restart_offline_with_snapshot_uses_snapshot_and_forwards():
    make_ha_stubs()
    hass = FakeHass()
    entry = DummyEntry("1.2.3.4")
    init_mod, coord_mod = load_modules_with_fakes(APIOffline())
    # Preload snapshot by patching Store
    store = sys.modules["homeassistant.helpers.storage"].Store
    # Create a coordinator instance to access the store key
    # But easier: monkeypatch the Store class to return snapshot on first load
    class StoreWithSnapshot(store):
        async def async_load(self):
            return {
                "device_id": "devSnap",
                "status": {"device_id": "devSnap", "ecu_status": "offline", "hw_version": "1", "sta_ip": "1.2.3.5", "fw_version": "1"},
                "pid": {"SOC": {"class": "none", "unit": "%", "value": 33}},
                "timestamp": "2024-01-01T00:00:00+00:00",
            }
    sys.modules["homeassistant.helpers.storage"].Store = StoreWithSnapshot
    # Ensure coordinator uses the patched Store symbol
    coord_mod.Store = StoreWithSnapshot

    ok = await init_mod.async_setup_entry(hass, entry)
    assert ok is True
    assert hass.config_entries.forwarded == ["binary_sensor", "sensor"]
    # Coordinator should be marked stale after refresh uses memory/snapshot
    from custom_components.wican.const import DOMAIN
    coord = hass.data[DOMAIN][entry.entry_id]
    assert coord.stale() is True


@pytest.mark.asyncio
async def test_device_recovers_after_offline_setup():
    make_ha_stubs()
    hass = FakeHass()
    entry = DummyEntry("1.2.3.4")
    # Start offline with snapshot
    init_mod, coord_mod = load_modules_with_fakes(APIOffline())
    store = sys.modules["homeassistant.helpers.storage"].Store
    class StoreWithSnapshot(store):
        async def async_load(self):
            return {
                "device_id": "devSnap",
                "status": {"device_id": "devSnap", "ecu_status": "offline", "hw_version": "1", "sta_ip": "1.2.3.5", "fw_version": "1"},
                "pid": {"SOC": {"class": "none", "unit": "%", "value": 33}},
                "timestamp": "2024-01-01T00:00:00+00:00",
            }
    sys.modules["homeassistant.helpers.storage"].Store = StoreWithSnapshot
    coord_mod.Store = StoreWithSnapshot

    ok = await init_mod.async_setup_entry(hass, entry)
    assert ok is True
    from custom_components.wican.const import DOMAIN
    coord = hass.data[DOMAIN][entry.entry_id]
    assert coord.stale() is True

    # Now swap API to online and refresh
    init_mod, _ = load_modules_with_fakes(APIGood())
    api_good = init_mod.WiCan  # not used directly here
    # Call coordinator.get_data with a fake api bound to coordinator
    coord.api.check_status = lambda: APIGood().check_status()
    coord.api.get_pid = lambda: APIGood().get_pid()
    data = await coord.get_data()
    assert coord.stale() is False
    assert isinstance(coord.last_successful_update, object)
