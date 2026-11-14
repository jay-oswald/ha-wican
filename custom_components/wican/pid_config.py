"""The unit and class of each PID sensor, read from the config section of a push.

Firmware 4.x pushes ``config`` as a map of sensor name to ``{"unit",
"class"}``. Firmware 6 pushes the tables of its AutoPID config file instead:
``pids`` and ``filters`` are lists of rows whose ``parameters`` name the
sensors with their unit and class, ``groups`` says how the rows are polled.
Read as the 4.x map, the tables gave no sensor of a v6 device a unit or a
class.

In its ``changed`` data mode a v6 device sends a table only when it changed,
and the whole section after a registration, which can be long before a PID
has its first value. What was seen is therefore kept for as long as the entry
is loaded (:class:`PidConfigTables`); only what backs an entity is stored.
"""

from __future__ import annotations

from typing import Any, Final

from .const import (
    MAX_DYNAMIC_PID_SENSORS,
    MAX_PID_CONFIG_FIELD_LENGTH,
    MAX_PID_KEY_LENGTH,
)

# The tables of a v6 config section whose rows carry sensors.
_SENSOR_TABLES: Final[tuple[str, ...]] = ("pids", "filters")

# Units the firmware's standard PID table spells its own way, by their
# lower-case form, and the spelling Home Assistant knows. Home Assistant
# takes a device class only with one of its own units ("degC" with the
# temperature class is a warning in its log and no unit conversion).
# ``None``: the value is not a quantity ("Encoded" is a bit field).
# Only words that are no unit to Home Assistant at all are here: a unit
# in another case ("RPM", "KPa") stays as the device spells it, so the
# unit of a sensor that exists does not change under its statistics.
_UNIT_SPELLINGS: Final[dict[str, str | None]] = {
    "degc": "°C",
    "degf": "°F",
    "deg": "°",
    "grams/sec": "g/s",
    "seconds": "s",
    "minutes": "min",
    "hours": "h",
    "volts": "V",
    "encoded": None,
}


def home_assistant_unit(unit: str | None) -> str | None:
    """Return a unit in Home Assistant's spelling; others stay as they are."""
    if unit is None:
        return None
    return _UNIT_SPELLINGS.get(unit.strip().lower(), unit)


def trimmed_pid_config(config: Any) -> dict[str, str]:
    """Reduce a device-reported per-PID config to the fields we persist.

    Only ``unit`` and ``class`` are ever read back, and the config entry is
    written to disk, so cap what a misbehaving device can store there.
    """
    if not isinstance(config, dict):
        return {}
    trimmed: dict[str, str] = {}
    for config_field in ("unit", "class"):
        value = config.get(config_field)
        if isinstance(value, str) and value and len(value) <= MAX_PID_CONFIG_FIELD_LENGTH:
            trimmed[config_field] = value
    return trimmed


def _usable_name(name: Any) -> bool:
    return isinstance(name, str) and bool(name.strip()) and len(name) <= MAX_PID_KEY_LENGTH


def _table_sensors(rows: list[Any]) -> dict[str, dict[str, str]]:
    """Return the sensors of one v6 table: parameter name to unit and class.

    Rows and parameters of another shape are skipped; the count is capped
    like the sensors themselves.
    """
    sensors: dict[str, dict[str, str]] = {}
    for row in rows:
        parameters = row.get("parameters") if isinstance(row, dict) else None
        if not isinstance(parameters, list):
            continue
        for parameter in parameters:
            if len(sensors) >= MAX_DYNAMIC_PID_SENSORS:
                return sensors
            if isinstance(parameter, dict) and _usable_name(parameter.get("name")):
                sensors[parameter["name"]] = trimmed_pid_config(parameter)
    return sensors


class PidConfigTables:
    """What a device has said about its PID sensors since the entry loaded."""

    __slots__ = ("_by_name", "_tables")

    def __init__(self) -> None:
        # The 4.x map, entry by entry as pushed.
        self._by_name: dict[str, dict[str, str]] = {}
        # The v6 tables, each replaced whole when it is pushed again.
        self._tables: dict[str, dict[str, dict[str, str]]] = {}

    def update(self, config: Any) -> bool:
        """Take the config section of a push; True when it carried any."""
        if not isinstance(config, dict):
            return False
        took = False
        for key, value in config.items():
            if key in _SENSOR_TABLES and isinstance(value, list):
                self._tables[key] = _table_sensors(value)
                took = True
            elif isinstance(value, dict) and _usable_name(key) and (
                key in self._by_name or len(self._by_name) < MAX_DYNAMIC_PID_SENSORS
            ):
                self._by_name[key] = trimmed_pid_config(value)
                took = True
        return took

    def sensors(self) -> dict[str, dict[str, str]]:
        """Return sensor name to ``{"unit", "class"}``; a v6 table wins."""
        merged = dict(self._by_name)
        for table in _SENSOR_TABLES:
            merged.update(self._tables.get(table, {}))
        return merged
