"""The away time of a sensor in Home Assistant's History.

The catch-up imports what the device recorded into each sensor's long-term
statistics (history.py), which is all Home Assistant's official API takes.
The History page and a sensor's own graph draw STATES, though: a drive away
from home stayed a gap or a flat line there, however complete the import
was. This module writes the recorded values of the time Home Assistant
heard nothing from the device as states of that time.

No supported call writes a state of the past. The rows go into the
recorder's own ``states`` table, through a task on the recorder's thread
(the way the recorder writes its own). That is recorder internals, so:

- It runs only on recorder database versions it was tested with
  (``HISTORY_STATES_RECORDER_SCHEMAS``, widened or narrowed by the device
  catalog without an integration release: ``integration.history_states``
  in device_catalog.json). On any other version it does nothing, says so
  once in the log, and the statistics import goes on as before.
- The pieces it needs are looked up before it writes; a Home Assistant
  that lacks one gets the same treatment as an untested version.
- A failure here never reaches the statistics import: this module returns
  a result, it does not raise.

What is written: for each sensor the device logged, the values of each
gap (the time between the last push before a silence and the push that
ended it), in the unit and the rounding of the sensor's states, one state
when the value changed and not sooner than the entry's spacing after the
sensor's last one (the option ``history_states_step``, in seconds; 0 = a
state for every change the device logged, which is the finest there is:
Home Assistant itself keeps no second state of an unchanged value). Each
row takes the attributes of the sensor's newest state. Statistics are not
touched here: they count every logged row, whatever the spacing.

One run writes ``HISTORY_STATES_MAX_PER_RUN`` states at most. A run that
would write more widens the spacing for that run (1 s, then doubling) and
says so in the log and in the diagnostics, instead of cutting the drive
off in time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.recorder import get_instance

from .const import (
    DEFAULT_HISTORY_STATES_STEP,
    HISTORY_STATES_MAX_PER_RUN,
    HISTORY_STATES_RECORDER_SCHEMAS,
    HISTORY_STATES_WRITE_TIMEOUT,
    MAX_HISTORY_STATES_STEP,
    MIN_HISTORY_STATES_STEP,
)

try:  # recorder internals (see the module's notes)
    from homeassistant.components.recorder.db_schema import States
    from homeassistant.components.recorder.tasks import RecorderTask
    from homeassistant.components.recorder.util import session_scope  # type: ignore[attr-defined]
    from sqlalchemy import select

    _RECORDER_PARTS = True
except ImportError:  # a Home Assistant that moved them: no History fill
    States = None  # type: ignore[assignment,misc]
    RecorderTask = object  # type: ignore[assignment,misc]
    session_scope = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]
    _RECORDER_PARTS = False

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# The columns a written state sets. A recorder without one of them is not
# the recorder this was tested with, whatever its version says.
_STATE_COLUMNS = (
    "state",
    "metadata_id",
    "attributes_id",
    "last_updated_ts",
    "last_changed_ts",
    "last_reported_ts",
    "origin_idx",
)

# One log line per reason and Home Assistant run, not one per sync.
_SAID: set[str] = set()

# How often a run over its cap may widen its spacing: 1 s doubled this
# often is more than three months, and a gap that long is two states a
# sensor (its first and its last), which every run can write.
_WIDEN_ROUNDS = 24


@dataclass(slots=True)
class StateSeries:
    """What the device logged for one sensor, and how its states are written."""

    entity_id: str
    points: list[tuple[float, float]]  # (epoch seconds, logged value)
    convert: Callable[[float], float]  # logged unit -> the unit of the states
    decimals: int = 3


@dataclass(slots=True)
class StateFillResult:
    """Outcome of one History fill (surfaced in diagnostics)."""

    active: bool = False  # the fill may write on this Home Assistant
    note: str | None = None  # why nothing was written, when that needs saying
    recorder_schema: int | None = None
    gaps: int = 0
    rows_in_gaps: int = 0
    states_written: int = 0
    sensors: int = 0
    until: float | None = None  # the newest logged moment this run dealt with
    errors: list[str] = field(default_factory=list)
    step: float | None = None  # the spacing the entry asks for, seconds
    step_used: float | None = None  # the spacing this run wrote with
    states_at_step: int = 0  # what the asked spacing alone came to


def coerce_step(value: Any) -> float:
    """The spacing an entry asks for, in seconds; 0 = every change.

    Anything that is not a number is the default; a number outside the
    range is its nearest end.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(DEFAULT_HISTORY_STATES_STEP)
    if value != value:  # not a number
        return float(DEFAULT_HISTORY_STATES_STEP)
    return float(min(max(value, MIN_HISTORY_STATES_STEP), MAX_HISTORY_STATES_STEP))


def schema_allowed(
    schema: Any,
    approved: Iterable[int] = (),
    blocked: Iterable[int] = (),
) -> bool:
    """Whether the History fill may write to this recorder database version.

    The versions it was tested with are built in; the device catalog can
    add one (a new Home Assistant, tested since) and can block one (a
    version that turned out not to work), a block having the last word.
    """
    if isinstance(schema, bool) or not isinstance(schema, int) or schema <= 0:
        return False
    if schema in set(blocked):
        return False
    return schema in HISTORY_STATES_RECORDER_SCHEMAS or schema in set(approved)


def recorder_has_the_parts(instance: Any) -> bool:
    """Whether this recorder has everything a state of the past needs."""
    if not _RECORDER_PARTS or States is None:
        return False
    if not callable(getattr(instance, "queue_task", None)):
        return False
    manager = getattr(instance, "states_meta_manager", None)
    if not callable(getattr(manager, "get_many", None)):
        return False
    return all(hasattr(States, column) for column in _STATE_COLUMNS)


def format_state(value: float, decimals: int) -> str:
    """A value as a sensor's state reads: rounded, a whole number bare."""
    rounded = round(value, decimals)
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return str(int(rounded))
    return f"{rounded:.{decimals}f}".rstrip("0").rstrip(".")


def points_in_gaps(
    points: list[tuple[float, float]],
    gaps: list[tuple[float, float]],
    after_ts: float,
) -> list[list[tuple[float, float]]]:
    """The points of one sensor that fall inside a gap, one list per gap.

    A gap is (the last push before the silence, the push that ended it);
    a point belongs to it strictly between the two. Points at or before
    ``after_ts`` were dealt with by an earlier run.
    """
    out: list[list[tuple[float, float]]] = []
    ordered = sorted(points)
    for start, end in sorted(gaps):
        inside = [
            (ts, value)
            for ts, value in ordered
            if start < ts < end and ts > after_ts
        ]
        if inside:
            out.append(inside)
    return out


def thin(
    states: list[tuple[float, str]],
    min_step: float,
) -> list[tuple[float, str]]:
    """The states to write of one sensor in one gap.

    The first one; then one whenever the value is another than the last
    one written and ``min_step`` seconds have passed since; and the last
    one when the series ends on another value than the last one written.
    With a ``min_step`` of 0 that is every change of the value.
    """
    out: list[tuple[float, str]] = []
    for ts, state in states:
        if not out or (state != out[-1][1] and ts - out[-1][0] >= min_step):
            out.append((ts, state))
    if states and out[-1][1] != states[-1][1]:
        out.append(states[-1])
    return out


def select_states(
    series: list[StateSeries],
    gaps: list[tuple[float, float]],
    after_ts: float,
    result: StateFillResult,
    step: Any = DEFAULT_HISTORY_STATES_STEP,
) -> dict[str, list[tuple[float, str]]]:
    """Pick, convert and thin what one run writes: {entity_id: [(ts, state)]}.

    ``step`` is the entry's spacing in seconds (0 = every change). More
    than ``HISTORY_STATES_MAX_PER_RUN`` states in one run are thinned
    further (the spacing goes to 1 s and doubles from there) instead of
    being cut off in time; ``result`` says what was asked and what was
    used. Pure, and run off the event loop: a long drive is many rows.
    """
    per_gap: dict[str, list[list[tuple[float, str]]]] = {}
    until = after_ts
    for item in series:
        segments = points_in_gaps(item.points, gaps, after_ts)
        if not segments:
            continue
        formatted: list[list[tuple[float, str]]] = []
        for segment in segments:
            result.rows_in_gaps += len(segment)
            until = max(until, segment[-1][0])
            try:
                formatted.append(
                    [
                        (ts, format_state(item.convert(value), item.decimals))
                        for ts, value in segment
                    ],
                )
            except (ValueError, OverflowError, ArithmeticError) as err:
                result.errors.append(f"{item.entity_id}: {err}")
        if formatted:
            per_gap[item.entity_id] = formatted
    result.until = until if until > after_ts else None

    asked = coerce_step(step)
    used = asked
    result.step = asked
    chosen: dict[str, list[tuple[float, str]]] = {}
    for attempt in range(_WIDEN_ROUNDS + 1):
        chosen = {
            entity_id: [state for segment in segments for state in thin(segment, used)]
            for entity_id, segments in per_gap.items()
        }
        count = sum(len(states) for states in chosen.values())
        if attempt == 0:
            result.states_at_step = count
        if count <= HISTORY_STATES_MAX_PER_RUN or attempt == _WIDEN_ROUNDS:
            break
        # a spacing of 0 doubled stays 0: the first widening is one second
        used = max(1.0, used * 2)
    result.step_used = used
    return {entity_id: states for entity_id, states in chosen.items() if states}


@dataclass(slots=True)
class _WriteStatesTask(RecorderTask):
    """Write states of the past on the recorder's own thread."""

    states: dict[str, list[tuple[float, str]]]
    done: Callable[[int, int, str | None], None]

    commit_before = True

    def run(self, instance: Any) -> None:
        """Insert the rows; one transaction, all of them or none."""
        written = 0
        sensors = 0
        error: str | None = None
        try:
            with session_scope(session=instance.get_session()) as session:
                ids = instance.states_meta_manager.get_many(
                    list(self.states), session, True,
                )
                for entity_id, states in self.states.items():
                    metadata_id = ids.get(entity_id)
                    if metadata_id is None:
                        continue  # the recorder never saw a state of it
                    # the newest state's attributes (unit, class, name): the
                    # past rows read like the sensor's own
                    attributes_id = session.execute(
                        select(States.attributes_id)
                        .where(States.metadata_id == metadata_id)
                        .where(States.attributes_id.is_not(None))
                        .order_by(States.last_updated_ts.desc())
                        .limit(1),
                    ).scalar()
                    if attributes_id is None:
                        continue
                    session.add_all(
                        States(
                            state=state,
                            metadata_id=metadata_id,
                            attributes_id=attributes_id,
                            last_updated_ts=ts,
                            last_changed_ts=None,
                            last_reported_ts=None,
                            origin_idx=0,
                        )
                        for ts, state in states
                    )
                    written += len(states)
                    sensors += 1
        except Exception as err:  # recorder internals: anything
            written = 0
            sensors = 0
            error = f"{type(err).__name__}: {err}"
        self.done(written, sensors, error)


def _say_once(key: str, message: str, *args: Any) -> None:
    if key not in _SAID:
        _SAID.add(key)
        _LOGGER.warning(message, *args)


def why_not(
    instance: Any,
    approved: Iterable[int],
    blocked: Iterable[int],
) -> str | None:
    """Why the History fill writes nothing on this recorder; None when it may."""
    schema = getattr(instance, "schema_version", None)
    if not recorder_has_the_parts(instance):
        note = (
            "this Home Assistant's recorder is not built the way the "
            "History fill was tested with: statistics only"
        )
        _say_once("parts", "History fill off: %s", note)
        return note
    if not schema_allowed(schema, approved, blocked):
        note = (
            f"recorder database version {schema} is not one the History "
            "fill was tested with: statistics only (an update of the "
            "integration or of its device catalog can change that)"
        )
        _say_once(f"schema{schema}", "History fill off: %s", note)
        return note
    return None


async def _async_write(
    hass: HomeAssistant,
    instance: Any,
    states: dict[str, list[tuple[float, str]]],
) -> tuple[int, int, str | None]:
    """Hand the states to the recorder's thread and wait for its word."""
    future: asyncio.Future[tuple[int, int, str | None]] = hass.loop.create_future()

    def _resolve(written: int, sensors: int, error: str | None) -> None:
        if not future.done():
            future.set_result((written, sensors, error))

    def _done(written: int, sensors: int, error: str | None) -> None:
        hass.loop.call_soon_threadsafe(_resolve, written, sensors, error)

    instance.queue_task(_WriteStatesTask(states, _done))
    return await asyncio.wait_for(future, HISTORY_STATES_WRITE_TIMEOUT)


async def async_fill_states(
    hass: HomeAssistant,
    *,
    enabled: bool,
    approved: Iterable[int],
    blocked: Iterable[int],
    series: list[StateSeries],
    gaps: list[tuple[float, float]],
    after_ts: float,
    step: Any = DEFAULT_HISTORY_STATES_STEP,
) -> StateFillResult:
    """Write the logged values of the gaps as states. Never raises.

    ``step``: the entry's spacing between two states of one sensor, in
    seconds (0 = every change the device logged).
    """
    result = StateFillResult(gaps=len(gaps), step=coerce_step(step))
    if not enabled:
        result.note = "switched off in the options"
        return result
    try:
        instance = get_instance(hass)
        schema = getattr(instance, "schema_version", None)
        result.recorder_schema = schema if isinstance(schema, int) else None
        result.note = why_not(instance, approved, blocked)
        result.active = result.note is None
        states: dict[str, list[tuple[float, str]]] = {}
        if result.active and gaps and series:
            states = await hass.async_add_executor_job(
                select_states, series, gaps, after_ts, result, step,
            )
            if result.step_used != result.step:
                _LOGGER.warning(
                    "History fill: %d points at the spacing of %g s set in the "
                    "options are more than one catch-up writes (%d); this one "
                    "is written with a spacing of %g s (%d points)",
                    result.states_at_step,
                    result.step,
                    HISTORY_STATES_MAX_PER_RUN,
                    result.step_used,
                    sum(len(found) for found in states.values()),
                )
        if states:
            written, sensors, error = await _async_write(hass, instance, states)
            if error is not None:
                result.errors.append(error)
                result.until = None  # nothing written: the next run tries again
                _LOGGER.warning("History fill: the recorder took no state: %s", error)
            else:
                result.states_written = written
                result.sensors = sensors
    except Exception as err:  # never into the statistics import
        result.errors.append(f"{type(err).__name__}: {err}")
        result.until = None
        _LOGGER.warning("History fill failed: %s", err)
    return result
