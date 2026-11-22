"""The device log's units against the units Home Assistant keeps.

The device logs a value in its own unit (a coolant temperature in °C). Home
Assistant keeps a sensor's states and its statistics in the unit it shows,
which follows the unit system and the user's choice (°F on a US system).
Until 2.0.0-beta2 the catch-up wrote the logged number as it came: an hour
recorded as 79 °C went into a °F series as "79 °F" (12 of 30 sensors of a
test car on a US system, the barometric pressure on a metric one).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _as_logged(value: float) -> float:
    return value


def unit_converter(
    logged_unit: str | None,
    kept_unit: str | None,
) -> Callable[[float], float] | None:
    """Return the function that turns a logged value into the kept unit.

    The value passes as it is when the units are the same, or when one of
    them is not known (a sensor without a unit: nothing to convert by).
    None when both are known and Home Assistant has no conversion between
    them: such a value must not be written at all.
    """
    if not logged_unit or not kept_unit or logged_unit == kept_unit:
        return _as_logged
    converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(kept_unit)
    if converter is None or logged_unit not in converter.VALID_UNITS:
        return None

    def _convert(value: float) -> float:
        return float(converter.convert(value, logged_unit, kept_unit))

    return _convert
