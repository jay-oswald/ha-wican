"""Tests for the param_loader module."""

from __future__ import annotations

import pytest

from custom_components.wican.param_loader import (
    get_param_unit,
    get_param_device_class,
    get_param_icon,
    get_param_description,
    get_param_display_name,
    is_binary_sensor,
    get_all_params,
     is_valid_device_class,
    is_valid_class_unit_combo,
    DEFAULT_PARAM_ICON,
)


class TestGetParamUnit:
    """Tests for get_param_unit function."""

    def test_known_param_uppercase(self) -> None:
        """Test known parameter with uppercase name."""
        assert get_param_unit("SOC") == "%"
        assert get_param_unit("HV_V") == "V"
        assert get_param_unit("SPEED") == "km/h"

    def test_known_param_lowercase(self) -> None:
        """Test known parameter with lowercase name (case-insensitive)."""
        assert get_param_unit("soc") == "%"
        assert get_param_unit("hv_v") == "V"
        assert get_param_unit("speed") == "km/h"

    def test_known_param_mixed_case(self) -> None:
        """Test known parameter with mixed case."""
        assert get_param_unit("SoC") == "%"
        assert get_param_unit("Hv_V") == "V"

    def test_unknown_param_returns_none(self) -> None:
        """Test unknown parameter returns None."""
        assert get_param_unit("unknown_pid") is None
        assert get_param_unit("NONEXISTENT") is None

    def test_param_with_none_unit_returns_none(self) -> None:
        """Test parameter with empty/none unit returns None."""
        # GEAR has unit="" in params.json
        assert get_param_unit("GEAR") is None


class TestGetParamDeviceClass:
    """Tests for get_param_device_class function."""

    def test_known_param_with_class(self) -> None:
        """Test known parameter with valid device class."""
        assert get_param_device_class("SOC") == "battery"
        assert get_param_device_class("HV_V") == "voltage"
        assert get_param_device_class("COOLANT_TMP") == "temperature"
        assert get_param_device_class("SPEED") == "speed"

    def test_param_with_none_class_returns_none(self) -> None:
        """Test parameter with class='none' returns None."""
        assert get_param_device_class("ENGINE_RPM") == "frequency"
        # THROTTLE has class="none"
        assert get_param_device_class("THROTTLE") is None

    def test_unknown_param_returns_none(self) -> None:
        """Test unknown parameter returns None."""
        assert get_param_device_class("unknown_pid") is None


class TestGetParamIcon:
    """Tests for get_param_icon function."""

    def test_icon_from_device_class(self) -> None:
        """Test icon resolution from device class."""
        assert get_param_icon("any_param", "temperature") == "mdi:thermometer"
        assert get_param_icon("any_param", "voltage") == "mdi:flash"
        assert get_param_icon("any_param", "battery") == "mdi:battery"

    def test_icon_from_param_name(self) -> None:
        """Test icon resolution from parameter name."""
        assert get_param_icon("soc", None) == "mdi:battery"
        assert get_param_icon("hv_v", None) == "mdi:battery-high"
        assert get_param_icon("engine_rpm", None) == "mdi:engine"
        assert get_param_icon("fuel", None) == "mdi:fuel"

    def test_icon_device_class_takes_priority(self) -> None:
        """Test that device class icon takes priority over param name."""
        # Even though "soc" has its own icon, temperature device class should win
        assert get_param_icon("soc", "temperature") == "mdi:thermometer"

    def test_unknown_param_returns_default(self) -> None:
        """Test unknown parameter returns default icon."""
        assert get_param_icon("unknown_xyz", None) == DEFAULT_PARAM_ICON

    def test_unknown_device_class_falls_back_to_param_name(self) -> None:
        """Test unknown device class falls back to param name icon."""
        assert get_param_icon("soc", "unknown_class") == "mdi:battery"


class TestGetParamDescription:
    """Tests for get_param_description function."""

    def test_known_param_description(self) -> None:
        """Test getting description for known parameter."""
        assert get_param_description("SOC") == "State Of Charge"
        assert get_param_description("HV_V") == "High Voltage Battery Voltage"
        assert get_param_description("COOLANT_TMP") == "Coolant Temperature"

    def test_unknown_param_returns_none(self) -> None:
        """Test unknown parameter returns None."""
        assert get_param_description("unknown_pid") is None


class TestIsBinarySensor:
    """Tests for is_binary_sensor function."""

    def test_binary_sensor_params(self) -> None:
        """Test parameters defined as binary sensors."""
        assert is_binary_sensor("CHARGING") is True
        assert is_binary_sensor("CHARGER_CONNECTED") is True
        assert is_binary_sensor("READY") is True
        assert is_binary_sensor("PARK_BRAKE") is True

    def test_non_binary_sensor_params(self) -> None:
        """Test parameters that are not binary sensors."""
        assert is_binary_sensor("SOC") is False
        assert is_binary_sensor("HV_V") is False
        assert is_binary_sensor("SPEED") is False

    def test_unknown_param_returns_false(self) -> None:
        """Test unknown parameter returns False."""
        assert is_binary_sensor("unknown_pid") is False


class TestGetAllParams:
    """Tests for get_all_params function."""

    def test_returns_dict(self) -> None:
        """Test that get_all_params returns a dictionary."""
        params = get_all_params()
        assert isinstance(params, dict)

    def test_contains_known_params(self) -> None:
        """Test that returned dict contains known parameters."""
        params = get_all_params()
        assert "SOC" in params
        assert "HV_V" in params
        assert "SPEED" in params
        assert "COOLANT_TMP" in params

    def test_returns_copy(self) -> None:
        """Test that get_all_params returns a copy, not the original."""
        params1 = get_all_params()
        params2 = get_all_params()
        # Modifying one should not affect the other
        params1["TEST_KEY"] = {"description": "test"}
        assert "TEST_KEY" not in params2


class TestEvParameters:
    """Test EV-specific parameters from params.json."""

    def test_ev_battery_params(self) -> None:
        """Test EV battery parameters are loaded correctly."""
        assert get_param_unit("SOC") == "%"
        assert get_param_unit("SOH") == "%"
        assert get_param_unit("HV_V") == "V"
        assert get_param_unit("HV_A") == "A"
        assert get_param_unit("HV_W") == "W"
        assert get_param_unit("HV_CAPACITY") == "Ah"
        assert get_param_unit("HV_CAPACITY_KWH") == "kWh"

    def test_ev_charging_params(self) -> None:
        """Test EV charging parameters are loaded correctly."""
        assert get_param_unit("CHARGER_DC_PWR") == "kW"
        assert get_param_unit("KWH_CHARGED") == "kwh"
        assert get_param_unit("AC_C_C") == "A"
        assert get_param_unit("AC_C_V") == "V"

    def test_ev_range_params(self) -> None:
        """Test EV range parameters are loaded correctly."""
        assert get_param_unit("RANGE") == "km"
        assert get_param_unit("DIST_SINCE_FULL_CHARGE") == "km"

    def test_ev_temperature_params(self) -> None:
        """Test EV temperature parameters are loaded correctly."""
        for i in range(1, 6):
            assert get_param_unit(f"HV_T_{i}") == "°C"
        assert get_param_unit("HV_T_A") == "°C"
        assert get_param_unit("HV_T_MAX") == "°C"
        assert get_param_unit("HV_T_MIN") == "°C"


class TestIceParameters:
    """Test ICE (Internal Combustion Engine) parameters from params.json."""

    def test_engine_params(self) -> None:
        """Test engine parameters are loaded correctly."""
        assert get_param_unit("ENGINE_RPM") == "RPM"
        assert get_param_unit("SPEED") == "km/h"
        assert get_param_unit("COOLANT_TMP") == "°C"
        assert get_param_unit("THROTTLE") == "%"

    def test_fuel_params(self) -> None:
        """Test fuel parameters are loaded correctly."""
        assert get_param_unit("FUEL") == "%"
        assert get_param_unit("FUEL_PRESSURE") == "kPa"
        assert get_param_unit("FUEL_RATE") == "g/s"

    def test_tyre_params(self) -> None:
        """Test tyre parameters are loaded correctly."""
        for pos in ["FL", "FR", "RL", "RR"]:
            assert get_param_unit(f"TYRE_P_{pos}") == "psi"
            assert get_param_unit(f"TYRE_T_{pos}") == "°C"


class TestIsValidDeviceClass:
    """Tests for is_valid_device_class function."""

    def test_valid_device_classes(self) -> None:
        """Test valid HA device classes return True."""
        valid_classes = [
            "temperature", "voltage", "current", "power", "energy",
            "battery", "speed", "distance", "pressure", "humidity",
            "frequency", "duration",
        ]
        for dc in valid_classes:
            assert is_valid_device_class(dc) is True, f"{dc} should be valid"

    def test_invalid_device_classes(self) -> None:
        """Test invalid/firmware-specific device classes return False."""
        invalid_classes = [
            "invalid_class",
            "unknown",
            "none",            # "none" string should be invalid
            "encoded",         # Firmware uses this for some PIDs
            "not_a_class",
        ]
        for dc in invalid_classes:
            assert is_valid_device_class(dc) is False, f"{dc} should be invalid"

    def test_none_returns_false(self) -> None:
        """Test None returns False."""
        assert is_valid_device_class(None) is False

    def test_empty_string_returns_false(self) -> None:
        """Test empty string returns False."""
        assert is_valid_device_class("") is False


class TestIsValidClassUnitCombo:
    """Tests for is_valid_class_unit_combo function."""

    def test_valid_speed_units(self) -> None:
        """Test valid speed + unit combinations."""
        valid_combos = [
            ("speed", "km/h"),
            ("speed", "mph"),
            ("speed", "m/s"),
        ]
        for dc, unit in valid_combos:
            assert is_valid_class_unit_combo(dc, unit) is True, f"{dc}+{unit} should be valid"

    def test_invalid_speed_rpm_combo(self) -> None:
        """Test speed + rpm is invalid (this causes HA statistics issues)."""
        assert is_valid_class_unit_combo("speed", "rpm") is False
        assert is_valid_class_unit_combo("speed", "RPM") is False

    def test_valid_temperature_units(self) -> None:
        """Test valid temperature + unit combinations."""
        valid_combos = [
            ("temperature", "°C"),
            ("temperature", "°F"),
            ("temperature", "K"),
            ("temperature", "degC"),
        ]
        for dc, unit in valid_combos:
            assert is_valid_class_unit_combo(dc, unit) is True, f"{dc}+{unit} should be valid"

    def test_valid_pressure_units(self) -> None:
        """Test valid pressure + unit combinations."""
        valid_combos = [
            ("pressure", "kPa"),
            ("pressure", "bar"),
            ("pressure", "psi"),
            ("pressure", "hPa"),
        ]
        for dc, unit in valid_combos:
            assert is_valid_class_unit_combo(dc, unit) is True, f"{dc}+{unit} should be valid"

    def test_none_values_return_true(self) -> None:
        """Test None values return True (no validation possible)."""
        assert is_valid_class_unit_combo(None, "km/h") is True
        assert is_valid_class_unit_combo("speed", None) is True
        assert is_valid_class_unit_combo(None, None) is True

    def test_unknown_class_returns_true(self) -> None:
        """Test unknown device class returns True (no rules to validate)."""
        assert is_valid_class_unit_combo("unknown_class", "unknown_unit") is True


class TestPidAliasNormalization:
    """Tests for PID alias normalization (OBD-II naming variants)."""

    def test_obd_hex_prefix_enginerpm(self) -> None:
        """Test 0C-EngineRPM variants normalize correctly."""
        # All these should map to ENGINE_RPM and get its unit
        variants = [
            "0c-enginerpm",
            "0c_enginerpm",
            "0C-EngineRPM",
            "enginerpm",
            "engine_rpm",
            "EngineRPM",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "RPM", f"{variant} should have unit RPM, got {unit}"

    def test_obd_hex_prefix_vehiclespeed(self) -> None:
        """Test 0D-VehicleSpeed variants normalize correctly."""
        variants = [
            "0d-vehiclespeed",
            "0d_vehiclespeed",
            "0D-VehicleSpeed",
            "vehiclespeed",
            "vehicle_speed",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "km/h", f"{variant} should have unit km/h, got {unit}"

    def test_obd_coolant_temp_variants(self) -> None:
        """Test coolant temp variants normalize correctly."""
        variants = [
            "05-enginecoolanttemp",
            "05_enginecoolanttemp",
            "coolant_temp",
            "coolanttemp",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "°C", f"{variant} should have unit °C, got {unit}"

    def test_obd_fuel_level_variants(self) -> None:
        """Test fuel level variants normalize correctly."""
        variants = [
            "2f-fuellevel",
            "2f_fuellevel",
            "fuel_level",
            "fuellevel",
        ]
        for variant in variants:
            unit = get_param_unit(variant)
            assert unit == "%", f"{variant} should have unit %, got {unit}"


class TestGitHubParamsUpdate:
    """Tests for GitHub params.json update functionality."""

    @pytest.mark.asyncio
    async def test_fetch_params_from_github_success(self) -> None:
        """Test successful fetch from GitHub."""
        from unittest.mock import AsyncMock, MagicMock
        from custom_components.wican.param_loader import async_fetch_params_from_github

        # Mock response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=b'{"TEST_PARAM": {"description": "Test", "settings": {"unit": "V"}}}')
        mock_response.release = MagicMock()

        # Mock session
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_response)

        params, content_hash = await async_fetch_params_from_github(mock_session)

        assert params is not None
        assert "TEST_PARAM" in params
        assert params["TEST_PARAM"]["settings"]["unit"] == "V"
        assert content_hash is not None

    @pytest.mark.asyncio
    async def test_fetch_params_from_github_http_error(self) -> None:
        """Test handling of HTTP error from GitHub."""
        from unittest.mock import AsyncMock, MagicMock
        from custom_components.wican.param_loader import async_fetch_params_from_github

        # Mock 404 response
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.release = MagicMock()

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_response)

        params, content_hash = await async_fetch_params_from_github(mock_session)

        assert params is None
        assert content_hash is None

    @pytest.mark.asyncio
    async def test_fetch_params_from_github_invalid_json(self) -> None:
        """Test handling of invalid JSON from GitHub."""
        from unittest.mock import AsyncMock, MagicMock
        from custom_components.wican.param_loader import async_fetch_params_from_github

        # Mock response with invalid JSON
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=b'invalid json {{{')
        mock_response.release = MagicMock()

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_response)

        params, content_hash = await async_fetch_params_from_github(mock_session)

        assert params is None
        assert content_hash is None

    @pytest.mark.asyncio
    async def test_fetch_params_from_github_timeout(self) -> None:
        """Test handling of timeout from GitHub."""
        import asyncio
        from unittest.mock import MagicMock
        from custom_components.wican.param_loader import async_fetch_params_from_github

        # Mock session that raises timeout
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())

        params, content_hash = await async_fetch_params_from_github(mock_session)

        assert params is None
        assert content_hash is None

    def test_compute_hash_consistency(self) -> None:
        """Test that hash computation is consistent."""
        from custom_components.wican.param_loader import _compute_hash

        data = b'{"test": "data"}'
        hash1 = _compute_hash(data)
        hash2 = _compute_hash(data)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex length

    def test_compute_hash_different_data(self) -> None:
        """Test that different data produces different hashes."""
        from custom_components.wican.param_loader import _compute_hash

        hash1 = _compute_hash(b'{"test": "data1"}')
        hash2 = _compute_hash(b'{"test": "data2"}')

        assert hash1 != hash2

    def test_reload_params(self) -> None:
        """Test reload_params reloads from disk."""
        from custom_components.wican.param_loader import reload_params, get_all_params

        # Just verify it doesn't crash and returns valid data
        reload_params()
        params = get_all_params()
        assert isinstance(params, dict)
        assert "SOC" in params  # Known param should still exist


class TestGetParamDisplayName:
    """Tests for get_param_display_name function."""

    def test_uses_the_description(self) -> None:
        """params.json already carries a readable name for every param."""
        assert get_param_display_name("SOC") == "State Of Charge"
        assert get_param_display_name("HV_V") == "High Voltage Battery Voltage"

    def test_falls_back_to_the_key(self) -> None:
        """A PID with no description - unknown, or a hex-prefixed standard
        PID alias that params.json has never heard of - keeps the raw key
        the device gave it. Resolving those aliases to a real name needs the
        fallback table from fix-standard-pid-fallbacks, not present here.
        """
        assert get_param_display_name("SOME_CUSTOM_PID") == "SOME_CUSTOM_PID"
        assert get_param_display_name("42-ControlModuleVolt") == "42-ControlModuleVolt"
