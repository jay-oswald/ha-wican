"""PID sensors of a v6 device: the config tables, the unit spellings, the decimals.

Firmware 4.x pushes ``config`` as a map of sensor name to ``{"unit",
"class"}``. A v6 firmware pushes the tables of its AutoPID config file
instead: ``pids``, ``filters`` and ``groups`` lists, where a row's
``parameters`` carry the sensor's name, unit and class. In its ``changed``
data mode a table arrives only when it changed, and the whole section after
a registration, which can be long before a PID has its first value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from custom_components.wican.const import MAX_DYNAMIC_PID_SENSORS
from custom_components.wican.pid_config import PidConfigTables
from custom_components.wican.sensor import (
    DYNAMIC_PID_SENSORS,
    _build_pid_entity_description,
)

if TYPE_CHECKING:
    from homeassistant.core import State

    from tests.device_sim import MeatPiDeviceSimulator


def _tables() -> dict[str, Any]:
    """Return the config section of a v6 push: the AutoPID tables."""
    return {
        "groups": [{"name": "driving", "enabled": True, "period_ms": 500}],
        "pids": [
            {
                "name": "05-EngineCoolantTemp",
                "type": "std",
                "cmd": "0105",
                "group": "driving",
                "parameters": [
                    {
                        "name": "EngineCoolantTemp",
                        "expression": "B3-40",
                        "unit": "degC",
                        "class": "temperature",
                        "min": -40,
                        "max": 215,
                    },
                ],
            },
            {
                "name": "33-AbsBaroPres",
                "type": "std",
                "cmd": "0133",
                "group": "driving",
                "parameters": [
                    {
                        "name": "AbsBaroPres",
                        "expression": "B3",
                        "unit": "kPa",
                        "class": "atmospheric_pressure",
                        "min": 0,
                        "max": 255,
                    },
                ],
            },
            {
                "name": "pack",
                "type": "custom",
                "cmd": "220101",
                "group": "",
                "parameters": [
                    {
                        "name": "PackVoltage",
                        "expression": "[B5:B6]/10",
                        "unit": "volts",
                        "class": "voltage",
                    },
                    {
                        "name": "CalcEngineLoad",
                        "expression": "B3*0.3921568692",
                        "unit": "%",
                        "class": "none",
                    },
                ],
            },
        ],
        "filters": [
            {
                "frame_id": 666,
                "monitor_ms": 800,
                "parameters": [
                    {
                        "name": "CabinTemp",
                        "expression": "B4/2",
                        "unit": "degC",
                        "class": "temperature",
                    },
                ],
            },
        ],
    }


_VALUES = {
    "EngineCoolantTemp": 88,
    "AbsBaroPres": 101,
    "PackVoltage": 398.4,
    "CalcEngineLoad": 25.0980392156863,
    "CabinTemp": 21.5,
}


def _state(device: MeatPiDeviceSimulator, pid_key: str) -> State:
    state = device.hass.states.get(f"sensor.wican_sim_{pid_key.lower()}")
    assert state is not None, pid_key
    return state


def _unit(device: MeatPiDeviceSimulator, pid_key: str) -> str | None:
    return _state(device, pid_key).attributes.get("unit_of_measurement")


def _class(device: MeatPiDeviceSimulator, pid_key: str) -> str | None:
    return _state(device, pid_key).attributes.get("device_class")


def _native_unit(device: MeatPiDeviceSimulator, pid_key: str) -> str | None:
    """Return the unit the sensor declares, before Home Assistant converts it.

    Home Assistant shows an atmospheric pressure in hPa in a metric system
    whatever unit the sensor declares.
    """
    assert device.entry is not None
    sensor = DYNAMIC_PID_SENSORS[device.entry.entry_id][pid_key]
    return sensor.native_unit_of_measurement


async def test_v6_config_tables_give_a_pid_sensor_its_unit_and_class(
    device: MeatPiDeviceSimulator,
) -> None:
    """A PID's and a filter's parameters name the sensor's unit and class."""
    await device.async_setup()
    await device.push_and_settle(
        {**device.status(), "autopid_data": dict(_VALUES), "config": _tables()},
    )

    assert _unit(device, "EngineCoolantTemp") == "°C"
    assert _class(device, "EngineCoolantTemp") == "temperature"
    assert _native_unit(device, "AbsBaroPres") == "kPa"
    assert _class(device, "AbsBaroPres") == "atmospheric_pressure"
    assert _unit(device, "PackVoltage") == "V"
    assert _class(device, "PackVoltage") == "voltage"
    assert _unit(device, "CalcEngineLoad") == "%"
    assert _class(device, "CalcEngineLoad") is None
    # A filter's parameter is a sensor like a PID's.
    assert _unit(device, "CabinTemp") == "°C"
    assert _class(device, "CabinTemp") == "temperature"


async def test_v6_config_tables_are_stored_as_the_per_sensor_map(
    device: MeatPiDeviceSimulator,
) -> None:
    """The entry keeps the 4.x map shape, and a reload restores the units."""
    await device.async_setup()
    await device.push_and_settle(
        {**device.status(), "autopid_data": dict(_VALUES), "config": _tables()},
    )

    assert device.entry is not None
    assert device.entry.data["config"] == {
        "EngineCoolantTemp": {"unit": "degC", "class": "temperature"},
        "AbsBaroPres": {"unit": "kPa", "class": "atmospheric_pressure"},
        "PackVoltage": {"unit": "volts", "class": "voltage"},
        "CalcEngineLoad": {"unit": "%", "class": "none"},
        "CabinTemp": {"unit": "degC", "class": "temperature"},
    }

    await device.async_reload()

    assert _unit(device, "EngineCoolantTemp") == "°C"
    assert _class(device, "EngineCoolantTemp") == "temperature"
    assert _unit(device, "PackVoltage") == "V"


async def test_pid_with_its_first_value_after_the_config_gets_its_unit(
    device: MeatPiDeviceSimulator,
) -> None:
    """The tables come with the registration, the value when the car runs."""
    await device.async_setup()
    # After a registration: the whole config, the car is off (no values).
    await device.push_and_settle({**device.status(), "config": _tables()})
    # Later, in the changed mode: values only.
    await device.push_and_settle(
        {**device.status(), "autopid_data": {"EngineCoolantTemp": 71}},
    )

    assert _state(device, "EngineCoolantTemp").state == "71"
    assert _unit(device, "EngineCoolantTemp") == "°C"
    assert _class(device, "EngineCoolantTemp") == "temperature"
    assert device.entry is not None
    assert device.entry.data["config"] == {
        "EngineCoolantTemp": {"unit": "degC", "class": "temperature"},
    }


async def test_one_changed_table_does_not_forget_the_others(
    device: MeatPiDeviceSimulator,
) -> None:
    """In the changed mode a config section can carry one table alone."""
    await device.async_setup()
    await device.push_and_settle({**device.status(), "config": _tables()})
    # The groups changed on the device: only that table is pushed.
    await device.push_and_settle(
        {
            **device.status(),
            "config": {"groups": [{"name": "slow", "enabled": True}]},
            "autopid_data": {"PackVoltage": 401.2},
        },
    )
    # A new filter: only the filters table is pushed.
    await device.push_and_settle(
        {
            **device.status(),
            "config": {
                "filters": [
                    {
                        "frame_id": 777,
                        "parameters": [
                            {"name": "Soc", "unit": "%", "class": "battery"},
                        ],
                    },
                ],
            },
            "autopid_data": {"Soc": 80, "AbsBaroPres": 100},
        },
    )

    assert _unit(device, "PackVoltage") == "V"
    assert _unit(device, "Soc") == "%"
    assert _class(device, "Soc") == "battery"
    assert _native_unit(device, "AbsBaroPres") == "kPa"


async def test_changed_unit_of_a_known_pid_is_stored_without_a_value(
    device: MeatPiDeviceSimulator,
) -> None:
    """A table pushed alone refreshes what a reload restores."""
    await device.async_setup()
    await device.push_and_settle(
        {**device.status(), "autopid_data": dict(_VALUES), "config": _tables()},
    )
    tables = _tables()
    tables["pids"][1]["parameters"][0]["unit"] = "Pa"
    await device.push_and_settle(
        {**device.status(), "config": {"pids": tables["pids"]}},
    )

    assert device.entry is not None
    assert device.entry.data["config"]["AbsBaroPres"] == {
        "unit": "Pa",
        "class": "atmospheric_pressure",
    }
    # It takes effect with the next load of the entry, as before.
    assert _native_unit(device, "AbsBaroPres") == "kPa"
    await device.async_reload()
    assert _native_unit(device, "AbsBaroPres") == "Pa"


async def test_malformed_config_tables_are_survived(
    device: MeatPiDeviceSimulator,
) -> None:
    """Rows and parameters of the wrong shape are skipped, the rest is read."""
    await device.async_setup()
    await device.push_and_settle(
        {
            **device.status(),
            "autopid_data": {"Good": 1, "NoConfig": 2},
            "config": {
                "groups": "not a list",
                "pids": [
                    "not a row",
                    {"name": "no parameters"},
                    {"name": "bad parameters", "parameters": {"name": "x"}},
                    {
                        "name": "mixed",
                        "parameters": [
                            None,
                            {"unit": "V"},
                            {"name": 7, "unit": "V"},
                            {"name": "Good", "unit": "km", "class": "distance"},
                        ],
                    },
                ],
                "filters": [{"frame_id": 1, "parameters": "nope"}],
            },
        },
    )

    assert _unit(device, "Good") == "km"
    assert _class(device, "Good") == "distance"
    assert _unit(device, "NoConfig") is None
    assert device.entry is not None
    assert device.entry.data["config"] == {
        "Good": {"unit": "km", "class": "distance"},
    }


async def test_legacy_config_map_still_gives_the_unit(
    device: MeatPiDeviceSimulator,
) -> None:
    """The 4.x map shape is read as before, with the same unit spellings."""
    await device.async_setup()
    await device.push_and_settle(
        {
            **device.status(),
            **device.pids(
                {"Coolant": 90, "Speed": 55},
                {
                    "Coolant": {"unit": "degC", "class": "temperature"},
                    "Speed": {"unit": "km/h", "class": "speed"},
                },
            ),
        },
    )

    assert _unit(device, "Coolant") == "°C"
    assert _class(device, "Coolant") == "temperature"
    assert _unit(device, "Speed") == "km/h"
    assert device.entry is not None
    assert device.entry.data["config"] == {
        "Coolant": {"unit": "degC", "class": "temperature"},
        "Speed": {"unit": "km/h", "class": "speed"},
    }


@pytest.mark.parametrize(
    ("firmware_unit", "firmware_class", "unit", "device_class"),
    [
        ("degC", "temperature", "°C", "temperature"),
        ("degF", "temperature", "°F", "temperature"),
        ("deg", "none", "°", None),
        ("grams/sec", "none", "g/s", None),
        ("seconds", "duration", "s", "duration"),
        ("minutes", "duration", "min", "duration"),
        ("hours", "none", "h", None),
        ("volts", "voltage", "V", "voltage"),
        ("Encoded", "none", None, None),
        # Spellings Home Assistant shares stay as they are.
        ("kPa", "pressure", "kPa", "pressure"),
        ("mA", "current", "mA", "current"),
        ("ratio", "none", "ratio", None),
        # A unit in another case is not rewritten (the unit of a sensor
        # that exists must not change under its statistics); the class
        # Home Assistant would warn about goes.
        ("RPM", "none", "RPM", None),
        ("KPa", "pressure", "KPa", None),
    ],
)
def test_firmware_unit_spelling_becomes_home_assistants(
    firmware_unit: str,
    firmware_class: str,
    unit: str | None,
    device_class: str | None,
) -> None:
    """The firmware's standard PID table spells some units its own way."""
    description = _build_pid_entity_description(
        "SomeRow", {"unit": firmware_unit, "class": firmware_class},
    )

    assert description.native_unit_of_measurement == unit
    assert description.device_class == device_class


@pytest.mark.parametrize(
    ("firmware_unit", "firmware_class", "unit"),
    [
        # Pairs of the firmware's standard PID table that Home Assistant
        # does not take: the class goes, the unit stays.
        ("ratio", "gas", "ratio"),
        ("%", "volume_storage", "%"),
        ("grams/sec", "volume_flow_rate", "g/s"),
        ("none", "pressure", None),
        ("none", "temperature", None),
    ],
)
async def test_class_home_assistant_rejects_with_the_unit_is_dropped(
    device: MeatPiDeviceSimulator,
    caplog: pytest.LogCaptureFixture,
    firmware_unit: str,
    firmware_class: str,
    unit: str | None,
) -> None:
    """Home Assistant must not be given a class and unit pair it warns about."""
    await device.async_setup()
    await device.push_and_settle(
        {
            **device.status(),
            **device.pids(
                {"SomeRow": 3},
                {"SomeRow": {"unit": firmware_unit, "class": firmware_class}},
            ),
        },
    )

    assert _unit(device, "SomeRow") == unit
    assert _class(device, "SomeRow") is None
    assert "not a valid unit for the device class" not in caplog.text


@pytest.mark.parametrize(
    ("firmware_unit", "firmware_class", "device_class"),
    [
        # Home Assistant takes these classes only as a total.
        ("kWh", "energy", None),
        ("L", "volume", None),
        ("L", "water", None),
        # These it takes as a measurement.
        ("km", "distance", "distance"),
        ("kW", "power", "power"),
        ("%", "battery", "battery"),
    ],
)
async def test_class_that_is_only_a_total_is_dropped(
    device: MeatPiDeviceSimulator,
    caplog: pytest.LogCaptureFixture,
    firmware_unit: str,
    firmware_class: str,
    device_class: str | None,
) -> None:
    """A PID sensor is a measurement: no class Home Assistant refuses for one."""
    await device.async_setup()
    await device.push_and_settle(
        {
            **device.status(),
            **device.pids(
                {"SomeRow": 3.5},
                {"SomeRow": {"unit": firmware_unit, "class": firmware_class}},
            ),
        },
    )

    state = _state(device, "SomeRow")
    assert state.state == "3.5"
    assert state.attributes.get("unit_of_measurement") == firmware_unit
    assert state.attributes.get("device_class") == device_class
    assert state.attributes.get("state_class") == "measurement"
    assert "impossible considering device class" not in caplog.text
    assert "not a valid unit for the device class" not in caplog.text


def test_what_a_device_can_make_the_integration_remember_is_capped() -> None:
    """Neither a table nor the 4.x map grows past the number of sensors."""
    too_many = MAX_DYNAMIC_PID_SENSORS + 5
    tables = PidConfigTables()

    assert tables.update("not a config section") is False
    assert tables.update({"groups": [{"name": "driving"}]}) is False
    assert tables.update(
        {
            "pids": [
                {"parameters": [{"name": f"P{n}", "unit": "V"}]}
                for n in range(too_many)
            ],
        },
    )
    assert len(tables.sensors()) == MAX_DYNAMIC_PID_SENSORS

    by_name = PidConfigTables()
    assert by_name.update({f"P{n}": {"unit": "V"} for n in range(too_many)})
    assert len(by_name.sensors()) == MAX_DYNAMIC_PID_SENSORS
    # A name that is known is still refreshed.
    assert by_name.update({"P0": {"unit": "mV"}})
    assert by_name.sensors()["P0"] == {"unit": "mV"}


async def test_pid_value_is_rounded_to_three_decimals(
    device: MeatPiDeviceSimulator,
) -> None:
    """A computed value keeps three decimals; whole numbers and the fix stay."""
    await device.async_setup()
    await device.push_and_settle(
        {
            **device.status(),
            "autopid_data": {
                "CalcEngineLoad": 25.0980392156863,
                "ShortTrim": -3.90625,
                "FuelLevel": "47.123456",
                "EngineRPM": 820,
                "Odometer": 12345.0,
                "gps_latitude": -25.344412345,
                "gps_longitude": 131.036912345,
                "gps_altitude": 88.80004,
            },
        },
    )

    assert _state(device, "CalcEngineLoad").state == "25.098"
    assert _state(device, "ShortTrim").state == "-3.906"
    assert _state(device, "FuelLevel").state == "47.123"
    assert _state(device, "EngineRPM").state == "820"
    assert _state(device, "Odometer").state == "12345.0"
    assert _state(device, "gps_altitude").state == "88.8"
    # The coordinates keep every digit the device sent.
    assert _state(device, "gps_latitude").state == "-25.344412345"
    assert _state(device, "gps_longitude").state == "131.036912345"

    assert device.entry is not None
    rpm = DYNAMIC_PID_SENSORS[device.entry.entry_id]["EngineRPM"].native_value
    assert rpm == 820
    assert isinstance(rpm, int)


async def test_number_sent_as_text_is_rounded_too(
    device: MeatPiDeviceSimulator,
) -> None:
    """A value with a unit that arrives as text is read as a number."""
    await device.async_setup()
    await device.push_and_settle(
        {
            **device.status(),
            **device.pids(
                {"PackVoltage": " 398.45678 "},
                {"PackVoltage": {"unit": "volts", "class": "voltage"}},
            ),
        },
    )

    assert _state(device, "PackVoltage").state == "398.457"
