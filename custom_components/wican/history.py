"""SD-card history catch-up into Home Assistant long-term statistics.

A MeatPi device that was driving away from home logs timestamped AutoPID
rows to its SD card (the firmware ``data_logger`` params stream). When the
device reconnects, this module fetches the rows recorded since the last
run, aggregates them into hourly mean/min/max buckets, and imports them
onto the matching PID sensor entities via Home Assistant's official
recorder statistics API, filling the gap in each sensor's long-term
history at the times the data was actually recorded.

Design and constraints: notes/HISTORICAL_DATA_SYNC.md. Key invariants:

- Statistics go in through the official ``async_import_statistics`` API
  only (hourly resolution). The states of the time the device was away
  (the History page) are history_states.py's job, called from here.
- A logged value is converted from the device's unit into the unit Home
  Assistant keeps that sensor's statistics in (history_units.py); a value
  Home Assistant cannot convert is not imported at all.
- The hour still running is never imported, and neither is an hour that
  ended less than ``HISTORY_HOUR_SETTLE`` seconds ago: Home Assistant
  writes its own row of an hour ten seconds past its end and that write
  fails, for every sensor of the installation, when a row is there
  already. The rows of such an hour are deferred, a follow-up run picks
  them up, and their hourly figures are kept meanwhile: when the device
  does not answer the follow-up (asleep a few minutes after it got home)
  the hour is imported from that copy, and imported again from the
  device's own rows at its next wake.
- Rows named ``device.<key>`` (the unit's battery voltage) fill the
  status sensor of that key.
- Hours the device recorded replace the live statistics of the same hour
  by default (the log is the complete record, the live pushes are samples
  of it: the drive's last hour would otherwise keep only the minutes after
  the car got home); the ``history_prefer_live`` option keeps the old
  "live wins" rule.
- Everything is bounded (rows, pages, files, bytes, distinct PIDs, age
  window) so a glitching or hostile device cannot balloon memory or
  statistics; what did not fit continues in a follow-up run.
- Two marks are kept per entry: the TIME watermark (the newest imported
  hour end; the import guard) and the device's READ cursor (where the
  next pull starts). The cursor never passes a row that was not imported
  or discarded for good: deferred rows and a failed PID's hours roll it
  back to the start of the page that holds them. An interrupted sync
  simply re-runs; re-importing an hour is idempotent.
- The device's export (firmware ``GET /api/logger/export``) stamps rows in
  epoch MILLISECONDS and names them ``<source>.<name>``; the old proposal
  (seconds, bare names) is accepted too.
- A sync failure is contained: it never disturbs telemetry or control.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import partial
import io
import json
import logging
from math import isfinite
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .attributes import SENSOR_DESCRIPTIONS
from .catalog import integration_policy
from .const import (
    CLOCK_JOURNAL_BOOT_SLACK_MS,
    CONF_HISTORY_PREFER_LIVE,
    CONF_HISTORY_STATES,
    CONF_HISTORY_STATES_STEP,
    DEFAULT_HISTORY_PREFER_LIVE,
    DEFAULT_HISTORY_STATES,
    DEFAULT_HISTORY_STATES_STEP,
    DOMAIN,
    HISTORY_DEVICE_SENSOR_KEYS,
    HISTORY_DEVICE_SOURCE,
    HISTORY_EXPORT_MAX_PAGES,
    HISTORY_EXPORT_PAGE_ROWS,
    HISTORY_FOLLOWUP_HOUR_MARGIN,
    HISTORY_FOLLOWUP_MORE_DELAY,
    HISTORY_FUTURE_SKEW,
    HISTORY_GAPS_MAX,
    HISTORY_HOUR_SETTLE,
    HISTORY_IMPORT_GENERATION,
    HISTORY_KEPT_MAX_AGE,
    HISTORY_MAX_AGE_DAYS,
    HISTORY_MAX_FILE_BYTES,
    HISTORY_MAX_FILES_PER_SYNC,
    HISTORY_MAX_PIDS_PER_SYNC,
    HISTORY_MAX_ROWS_PER_SYNC,
    HISTORY_STORAGE_VERSION,
    HISTORY_TS_MS_THRESHOLD,
    MAX_PID_KEY_LENGTH,
    PID_VALUE_DECIMALS,
)
from .exceptions import MeatPiApiError
from .history_states import StateSeries, async_fill_states
from .history_units import unit_converter

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from . import MeatPiConfigEntry
    from .api import MeatPiApiClient

_LOGGER = logging.getLogger(__name__)

# Row-field aliases accepted from device logs (jsonl/csv headers vary by
# firmware version; the firmware's export route uses ts / param / value).
_TS_KEYS = ("ts", "ts_ms", "timestamp", "time", "epoch")
_NAME_KEYS = ("name", "param", "pid")
_VALUE_KEYS = ("value", "val")

# The firmware names its params files dl_<10-digit epoch>.<ext>.
_LOG_FILE_RE = re.compile(r"^dl_(\d{10})\.[a-z0-9]+$")


@dataclass(slots=True)
class HistorySyncResult:
    """Outcome counters of one sync run (surfaced in diagnostics)."""

    ran: bool = False
    source: str | None = None  # "export" | "files" | None
    pages: int = 0
    rows_fetched: int = 0
    rows_used: int = 0
    rows_rejected: int = 0
    rows_rebased: int = 0  # stamps corrected with the device's clock journal
    rows_deferred: int = 0  # in the current hour: a follow-up takes them
    hours_imported: int = 0
    hours_skipped_existing: int = 0  # prefer_live: live hours left alone
    hours_overwritten_live: int = 0  # default: recorded hours replaced live ones
    # hours imported from the rows an earlier pull kept, the device not
    # answering (or not having them any more) when they had ended
    hours_from_kept_copy: int = 0
    pids_imported: int = 0
    pids_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    watermark: float | None = None
    cursor: str | None = None  # the device read cursor saved by this run
    more_pending: bool = False  # the device has more than one run could take
    followup_in: float | None = None  # seconds until a follow-up run is due
    device: dict[str, Any] | None = None  # the device logger's state
    states: dict[str, Any] | None = None  # the History fill (history_states.py)
    finished_at: float | None = None  # epoch seconds


@dataclass(slots=True)
class _Bucket:
    """Accumulates one PID's values within one hour."""

    total: float = 0.0
    count: int = 0
    minimum: float = 0.0
    maximum: float = 0.0

    def add(self, value: float) -> None:
        if self.count == 0:
            self.minimum = value
            self.maximum = value
        else:
            self.minimum = min(self.minimum, value)
            self.maximum = max(self.maximum, value)
        self.total += value
        self.count += 1

    @property
    def mean(self) -> float:
        return self.total / self.count

    def as_list(self) -> list[float]:
        """The figures as they are kept between two runs."""
        return [self.total, self.count, self.minimum, self.maximum]

    @classmethod
    def from_list(cls, figures: Any) -> _Bucket | None:
        """A kept bucket back, or None when it is not one."""
        if not isinstance(figures, list) or len(figures) != 4:
            return None
        total, count, minimum, maximum = (_coerce_float(item) for item in figures)
        if (
            total is None
            or minimum is None
            or maximum is None
            or count is None
            or count < 1
            or count != int(count)
            or minimum > maximum
        ):
            return None
        return cls(total=total, count=int(count), minimum=minimum, maximum=maximum)


@dataclass(slots=True)
class _Target:
    """The sensor a logged name fills, and the units on both sides."""

    entity_id: str
    unit: str | None  # the unit Home Assistant shows the sensor in (its states)
    logged_unit: str | None  # the unit of the device's values
    decimals: int = PID_VALUE_DECIMALS  # of a state written for it


@dataclass(slots=True)
class _PageMark:
    """Where a fetched page started and the newest row it carried."""

    start_cursor: str | None
    max_ts: float | None


def _coerce_float(value: Any) -> float | None:
    """Return a finite float from a device value, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if isfinite(number) else None
    return None


def _first_key(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


@dataclass(slots=True)
class _ClockStep:
    """One set or step of the device's clock while a log file was open."""

    before_ms: float  # the wall clock just before the step
    delta_ms: float  # what the step added (negative for a backward one)
    origin_ms: float | None  # the boot's wall origin as that clock had it


def journal_steps_by_file(events: list[dict[str, Any]]) -> dict[str, list[_ClockStep]]:
    """The clock journal's steps per log file, in journal order.

    Mirrors the device's own `tools/log_clock.py` and the web UI: a
    ``boot`` line fixes the boot's wall origin (its ``ts`` minus
    ``up_ms``), every ``valid`` or ``step`` line lists the files open at
    the time and moves the origin along by its ``delta_ms``.
    """
    out: dict[str, list[_ClockStep]] = {}
    origin: float | None = None
    for event in events:
        kind = event.get("ev")
        if kind == "boot":
            ts = _coerce_float(event.get("ts"))
            up_ms = _coerce_float(event.get("up_ms"))
            origin = (ts - (up_ms or 0.0)) if ts is not None else None
            continue
        if kind not in ("valid", "step"):
            continue
        delta = _coerce_float(event.get("delta_ms")) or 0.0
        before = _coerce_float(event.get("ts_before"))
        files = event.get("files")
        if before is not None and isinstance(files, list):
            for name in files:
                if isinstance(name, str):
                    out.setdefault(name, []).append(_ClockStep(before, delta, origin))
        if origin is not None:
            origin += delta
    return out


def rebase_row(raw: Any, steps: dict[str, list[_ClockStep]]) -> bool:
    """Correct a row's stamp as the device's clock would have written it.

    Every step that happened while the row's file was open and after the
    row (its stamp at or before the step's ``ts_before``) is added; a row
    older than the boot's origin was written by an earlier boot into a
    resumed file and keeps its stamp. The row is changed in place; True
    when it moved. Rows stamped in seconds (the proposal shape) are moved
    in seconds.
    """
    if not isinstance(raw, dict):
        return False
    name = raw.get("_file")
    if not isinstance(name, str) or name not in steps:
        return False
    key = next((k for k in _TS_KEYS if k in raw), None)
    if key is None:
        return False
    ts = _coerce_float(raw.get(key))
    if ts is None or ts <= 0:
        return False
    in_seconds = ts <= HISTORY_TS_MS_THRESHOLD
    ts_ms = ts * 1000.0 if in_seconds else ts
    moved = ts_ms
    for step in steps[name]:
        if moved <= step.before_ms and (
            step.origin_ms is None
            or moved >= step.origin_ms - CLOCK_JOURNAL_BOOT_SLACK_MS
        ):
            moved += step.delta_ms
    if moved == ts_ms:
        return False
    raw[key] = moved / 1000.0 if in_seconds else moved
    return True


def row_timestamp(raw: Any) -> float | None:
    """The row's timestamp in epoch SECONDS, or None.

    The firmware stamps in milliseconds (issue #984: the logger's wall
    stamps are epoch ms everywhere); a value past the threshold is read
    as milliseconds, anything else as seconds (the old proposal shape).
    """
    if not isinstance(raw, dict):
        return None
    ts = _coerce_float(_first_key(raw, _TS_KEYS))
    if ts is None or ts <= 0:
        return None
    if ts > HISTORY_TS_MS_THRESHOLD:
        ts /= 1000.0
    return ts


def normalize_row(raw: Any) -> tuple[float, str, float] | None:
    """Validate one raw log row into (ts seconds, name, value), or None.

    Accepts dict rows with aliased field names. The firmware's rows name
    the parameter ``<source>.<name>`` (``autopid.SOC``): the name after
    the last dot is the push's ``autopid_data`` key, which is what the PID
    entity is registered under. A value of the device itself
    (``device.batt_voltage``, firmware 6.0.0-beta.15) keeps its source in
    the name, so it never meets a PID of the same name; a device key this
    integration has no sensor for is rejected. Anything malformed (non-dict,
    missing fields, non-finite values, absurd names) is rejected; the
    caller counts rejects, a bad row never aborts a sync.
    """
    ts = row_timestamp(raw)
    if ts is None:
        return None
    name = _first_key(raw, _NAME_KEYS)
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > MAX_PID_KEY_LENGTH
    ):
        return None
    name = name.strip()
    if "." in name:
        source, _, name = name.rpartition(".")
        if not name:
            return None
        if source == HISTORY_DEVICE_SOURCE:
            if name not in HISTORY_DEVICE_SENSOR_KEYS:
                return None
            name = f"{HISTORY_DEVICE_SOURCE}.{name}"
    value = _coerce_float(_first_key(raw, _VALUE_KEYS))
    if value is None:
        return None
    return ts, name, value


def parse_jsonl(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse NDJSON/JSONL text into raw rows; returns (rows, undecodable).

    The firmware's meta lines (``{"_file": ...}``, ``{"_cursor": ...}``)
    and its clock-step markers are not rows and are left out.
    """
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped)
        except ValueError:
            bad += 1
            continue
        if not isinstance(decoded, dict):
            bad += 1
            continue
        if "_cursor" in decoded or "_file" in decoded or "event" in decoded:
            continue
        rows.append(decoded)
    return rows, bad


def parse_csv(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse CSV log text into raw rows; returns (rows, undecodable).

    The first row must be a header naming (aliases of) ts/name/value
    columns (the firmware writes ``ts_ms,param,value``); unknown columns
    are ignored.
    """
    rows: list[dict[str, Any]] = []
    bad = 0
    try:
        reader = csv.DictReader(io.StringIO(text))
        for record in reader:
            if not isinstance(record, dict):
                bad += 1
                continue
            rows.append({k: v for k, v in record.items() if isinstance(k, str)})
    except csv.Error:
        return rows, bad + 1
    return rows, bad


def open_hour(now_ts: float) -> int:
    """The start of the first hour that is not imported yet.

    The hour still running; or the one before it while that one has been
    over for less than ``HISTORY_HOUR_SETTLE`` seconds (Home Assistant
    writes its own row of an hour ten seconds past its end, and must find
    no row of ours there).
    """
    return int((now_ts - HISTORY_HOUR_SETTLE) // 3600) * 3600


def split_kept(
    kept: dict[str, Any],
    now_ts: float,
    covered: set[tuple[str, int]],
) -> tuple[dict[str, Any], dict[str, dict[int, _Bucket]]]:
    """Sort the hours kept between two runs.

    Returns (the hours that are not over yet, as they are kept; the
    buckets of the ended hours, per name, without the (name, hour) pairs
    in ``covered``). Hours kept for longer than ``HISTORY_KEPT_MAX_AGE``
    and anything that is not a kept hour are dropped.
    """
    limit = open_hour(now_ts)
    left: dict[str, Any] = {}
    buckets: dict[str, dict[int, _Bucket]] = {}
    for hour_key, names in kept.items():
        if not str(hour_key).isdigit() or not isinstance(names, dict):
            continue
        hour = int(hour_key)
        if hour >= limit:
            left[hour_key] = names
        elif hour + 3600 >= now_ts - HISTORY_KEPT_MAX_AGE:
            for name, figures in names.items():
                bucket = _Bucket.from_list(figures)
                if bucket is not None and (name, hour) not in covered:
                    buckets.setdefault(name, {})[hour] = bucket
    return left, buckets


def statistics_rows(
    hours: list[int],
    buckets: dict[int, _Bucket],
    convert: Callable[[float], float],
) -> list[dict[str, Any]]:
    """The statistics rows of one sensor's hours, in the unit they are kept in."""
    rows: list[dict[str, Any]] = []
    for hour in hours:
        bucket = buckets[hour]
        low, high = sorted((convert(bucket.minimum), convert(bucket.maximum)))
        rows.append(
            {
                "start": dt_util.utc_from_timestamp(hour),
                "mean": convert(bucket.mean),
                "min": low,
                "max": high,
            },
        )
    return rows


def _in_window(
    ts: float, *, oldest: float, now_ts: float, current_hour: int,
) -> bool:
    """Sanity window: nothing older than the watermark/age limit, nothing
    from the future (bad RTC), nothing in the current (incomplete,
    live-recorded) hour. ``oldest`` is inclusive-start: a row exactly at
    the watermark belongs to the first hour not yet imported."""
    return oldest <= ts <= now_ts + HISTORY_FUTURE_SKEW and ts < current_hour


def _deferred(ts: float, *, now_ts: float, current_hour: int) -> bool:
    """A sane row of the hour still running: imported by a later run."""
    return current_hour <= ts <= now_ts + HISTORY_FUTURE_SKEW


def _discard_unfinished_hours(
    remaining: list[Any],
    buckets: dict[str, dict[int, _Bucket]],
    result: HistorySyncResult,
    *,
    oldest: float,
    now_ts: float,
    current_hour: int,
) -> None:
    """Drop bucket hours that may be missing rows left beyond the row cap.

    Any hour at or after the earliest unprocessed in-window row may hold
    only part of its rows; importing it would freeze wrong mean/min/max
    behind the watermark forever. Dropping the whole hour keeps it ahead
    of the watermark, so the next sync re-fetches and imports it complete.
    """
    cutoff: int | None = None
    for raw in remaining:
        row = normalize_row(raw)
        if row is None:
            continue
        ts = row[0]
        if not _in_window(
            ts, oldest=oldest, now_ts=now_ts, current_hour=current_hour,
        ):
            continue
        hour = int(ts // 3600) * 3600
        if cutoff is None or hour < cutoff:
            cutoff = hour
    if cutoff is None:
        return
    dropped = 0
    for name in list(buckets):
        pid_buckets = buckets[name]
        for hour in [h for h in pid_buckets if h >= cutoff]:
            del pid_buckets[hour]
            dropped += 1
        if not pid_buckets:
            del buckets[name]
    if dropped:
        result.errors.append(
            f"{dropped} hour bucket(s) cut short by the row cap deferred",
        )
        result.more_pending = True


def aggregate_rows(
    raw_rows: list[Any],
    *,
    since: float,
    now_ts: float,
    result: HistorySyncResult,
    deferred: dict[str, dict[int, _Bucket]] | None = None,
) -> dict[str, dict[int, _Bucket]]:
    """Aggregate raw rows into per-PID, per-hour buckets, bounded.

    Rows outside [since, current-hour) or beyond the sanity window are
    rejected; rows of the hour that is not imported yet (``open_hour``)
    are deferred (counted apart, the earliest kept in
    ``result.followup_in``'s reckoning) and, when ``deferred`` is given,
    aggregated into it the same way: the caller keeps those figures for
    the day the device does not answer the follow-up. Returns
    {pid_name: {hour_epoch: bucket}}. When the row cap is hit, hours the
    leftover rows could still touch are discarded so a partially
    aggregated hour is never imported.
    """
    current_hour = open_hour(now_ts)
    oldest = max(since, now_ts - HISTORY_MAX_AGE_DAYS * 86400)
    buckets: dict[str, dict[int, _Bucket]] = {}
    used = 0
    for index, raw in enumerate(raw_rows):
        if used >= HISTORY_MAX_ROWS_PER_SYNC:
            result.errors.append("row cap reached; remaining rows deferred")
            _discard_unfinished_hours(
                raw_rows[index:],
                buckets,
                result,
                oldest=oldest,
                now_ts=now_ts,
                current_hour=current_hour,
            )
            break
        row = normalize_row(raw)
        if row is None:
            result.rows_rejected += 1
            continue
        ts, name, value = row
        if not _in_window(
            ts, oldest=oldest, now_ts=now_ts, current_hour=current_hour,
        ):
            if ts >= oldest and _deferred(
                ts, now_ts=now_ts, current_hour=current_hour,
            ):
                result.rows_deferred += 1
                if deferred is not None and (
                    name in deferred or len(deferred) < HISTORY_MAX_PIDS_PER_SYNC
                ):
                    hour = int(ts // 3600) * 3600
                    deferred.setdefault(name, {}).setdefault(hour, _Bucket()).add(value)
            else:
                result.rows_rejected += 1
            continue
        pid_buckets = buckets.get(name)
        if pid_buckets is None:
            if len(buckets) >= HISTORY_MAX_PIDS_PER_SYNC:
                result.rows_rejected += 1
                continue
            pid_buckets = buckets[name] = {}
        hour = int(ts // 3600) * 3600
        bucket = pid_buckets.get(hour)
        if bucket is None:
            bucket = pid_buckets[hour] = _Bucket()
        bucket.add(value)
        used += 1
        result.rows_used = used
    return buckets


def cursor_file_epoch(cursor: str | None) -> int | None:
    """The file epoch a device cursor (``<epoch>:<offset>``) points at."""
    if not isinstance(cursor, str) or ":" not in cursor:
        return None
    head = cursor.split(":", 1)[0]
    return int(head) if head.isdigit() else None


def log_file_epoch(name: Any) -> int | None:
    """The epoch in a params log file name (``dl_<epoch>.jsonl``)."""
    if not isinstance(name, str):
        return None
    match = _LOG_FILE_RE.match(name)
    return int(match.group(1)) if match else None


def cursor_is_plausible(cursor: str | None, status: dict[str, Any] | None) -> bool:
    """Whether a stored device cursor can still name a place on the card.

    A cursor past the file being written now (the card was wiped or
    swapped, the device's clock went back) would make the device answer
    nothing for good; such a cursor is dropped and the pull restarts
    from the time watermark. A cursor at a retired file is fine: the
    device resumes at the next newer one.
    """
    if isinstance(cursor, str) and cursor.isdigit():
        return True  # a plain epoch: the device finds the file itself
    epoch = cursor_file_epoch(cursor)
    if epoch is None:
        return False
    if not isinstance(status, dict):
        return True
    active = log_file_epoch(status.get("file"))
    if active is None:
        return True
    return epoch <= active


def followup_delay(result: HistorySyncResult, now_ts: float) -> float | None:
    """Seconds until a follow-up run is due, or None when nothing waits.

    Rows of the hour still running are imported once that hour has ended
    (a margin past the top of the hour: the device's last batch of it is
    written up to ``flush_ms`` later); a device with more than one run
    could take is read again soon.
    """
    if result.more_pending:
        return float(HISTORY_FOLLOWUP_MORE_DELAY)
    if result.rows_deferred:
        next_hour = open_hour(now_ts) + 3600
        return max(
            float(HISTORY_FOLLOWUP_MORE_DELAY),
            next_hour + HISTORY_FOLLOWUP_HOUR_MARGIN - now_ts,
        )
    return None


class HistorySync:
    """Per-entry history catch-up orchestrator."""

    def __init__(self, hass: HomeAssistant, entry: MeatPiConfigEntry) -> None:
        """Initialize the sync for one config entry."""
        self._hass = hass
        self._entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, HISTORY_STORAGE_VERSION, f"{DOMAIN}.history_{entry.entry_id}",
        )
        self._marks: list[_PageMark] = []
        self._end_cursor: str | None = None
        self._deferred_from: float | None = None
        self._raw_rows: list[Any] = []
        self._deferred_buckets: dict[str, dict[int, _Bucket]] = {}

    # -- the stored marks --------------------------------------------------

    async def _async_load(self) -> dict[str, Any]:
        data = await self._store.async_load()
        return data if isinstance(data, dict) else {}

    async def async_get_watermark(self) -> float:
        """Return the newest timestamp already imported (0.0 = never)."""
        watermark = _coerce_float((await self._async_load()).get("watermark"))
        if watermark is not None and watermark > 0:
            return watermark
        return 0.0

    async def async_get_cursor(self) -> str | None:
        """Return the device read cursor of the last run (None = none)."""
        cursor = (await self._async_load()).get("cursor")
        return cursor if isinstance(cursor, str) and cursor else None

    async def _async_save(self, watermark: float, cursor: str | None) -> None:
        data = await self._async_load()
        data["watermark"] = watermark
        data["cursor"] = cursor
        await self._store.async_save(data)

    async def _async_put(self, **values: Any) -> None:
        """Store other things than the two marks (None removes one)."""
        data = await self._async_load()
        changed = False
        for key, value in values.items():
            if value is None:
                if key in data:
                    del data[key]
                    changed = True
            elif data.get(key) != value:
                data[key] = value
                changed = True
        if changed:
            await self._store.async_save(data)

    async def _async_reread_after_upgrade(self) -> None:
        """Read the log again, once, where an earlier version imported wrong.

        Until 2.0.0-beta1 a logged value went into the statistics as it
        came. For a sensor Home Assistant keeps in another unit than the
        device logs (°F for °C, hPa for kPa) every imported hour is off
        by the conversion. The device's log still holds those hours for a
        while: with the marks back at zero the next runs read it from its
        start and import them again, converted (bounded as every read is;
        the default rule replaces the hours that are there). Entries
        without such a sensor, and marks this version wrote, are left alone.
        """
        data = await self._async_load()
        if data.get("generation") == HISTORY_IMPORT_GENERATION:
            return
        watermark = _coerce_float(data.get("watermark")) or 0.0
        if watermark > 0:
            pids = (getattr(self._entry, "data", None) or {}).get("pid_keys")
            names = [name for name in pids if isinstance(name, str)] if isinstance(pids, list) else []
            off = [
                name
                for name, target in self._resolve_entities(names).items()
                if target.logged_unit
                and target.unit
                and target.logged_unit != target.unit
                and unit_converter(target.logged_unit, target.unit) is not None
            ]
            if off:
                _LOGGER.info(
                    "History sync for %s: %d sensor(s) are kept in another unit "
                    "than the device logs (%s); the device's log is read again "
                    "from its start so the hours an earlier version imported "
                    "unconverted are imported again",
                    self._entry.title,
                    len(off),
                    ", ".join(off[:8]),
                )
                data["watermark"] = 0.0
                data["cursor"] = None
        data["generation"] = HISTORY_IMPORT_GENERATION
        await self._store.async_save(data)

    @staticmethod
    def _stored_gaps(data: dict[str, Any]) -> list[tuple[float, float]]:
        gaps: list[tuple[float, float]] = []
        raw = data.get("gaps")
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                start, end = _coerce_float(item[0]), _coerce_float(item[1])
                if start is not None and end is not None and end > start:
                    gaps.append((start, end))
        return gaps

    async def _async_note_gaps(self, runtime: Any, now_ts: float) -> None:
        """Remember the silences that ended since the last run.

        A gap is (the last push before a silence, the push that ended it):
        the time Home Assistant has no states of the device's sensors. The
        History fill writes the logged values of that time; a gap stays
        until the log was read over it.
        """
        take = getattr(getattr(runtime, "coordinator", None), "take_data_gaps", None)
        fresh = take() if callable(take) else []
        if not isinstance(fresh, list) or not fresh:
            return
        data = await self._async_load()
        gaps = self._stored_gaps(data)
        for item in fresh:
            if isinstance(item, tuple) and len(item) == 2 and item[1] > item[0]:
                gaps.append((float(item[0]), float(item[1])))
        oldest = now_ts - HISTORY_MAX_AGE_DAYS * 86400
        gaps = sorted({gap for gap in gaps if gap[1] > oldest})[-HISTORY_GAPS_MAX:]
        await self._async_put(gaps=[list(gap) for gap in gaps])

    async def async_remove(self) -> None:
        """Delete the persisted marks (config entry removed)."""
        await self._store.async_remove()

    # -- fetching ----------------------------------------------------------

    @staticmethod
    def _device_summary(status: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(status, dict):
            return None
        export = status.get("export")
        return {
            "logger": bool(status.get("enabled")),
            "format": status.get("format"),
            "autopid_log": status.get("autopid_log"),
            "files": status.get("files"),
            "storage_ok": status.get("storage_ok"),
            "file": status.get("file"),
            "export": export if isinstance(export, dict) else None,
        }

    async def _async_fetch_export(
        self,
        api: MeatPiApiClient,
        since: float,
        result: HistorySyncResult,
    ) -> list[Any] | None:
        """Fetch rows via the cursor export route; None when unsupported.

        The pull starts at the device cursor the last run saved (when it
        can still name a place on the card), else at the time watermark
        as a plain epoch (the device then starts at the file that was
        being written at that moment), else at the oldest file.
        """
        status: dict[str, Any] | None = None
        try:
            status = await api.async_get_logger_status()
        except MeatPiApiError as err:
            _LOGGER.debug("History sync: no logger status (%s)", err)
        result.device = self._device_summary(status)

        stored = await self.async_get_cursor()
        if stored is not None and not cursor_is_plausible(stored, status):
            result.errors.append(
                f"device cursor {stored} is past the file being written: "
                "restarting from the time watermark",
            )
            stored = None
        start: str | None = stored
        if start is None and since > 0:
            start = str(int(since))

        rows: list[Any] = []
        self._marks = []
        self._end_cursor = stored
        # the device's clock journal: rows written before its clock was set
        # or stepped (a dead RTC until the first time sync) carry the old
        # clock; the journal names the files and the steps (firmware #984)
        steps: dict[str, list[_ClockStep]] | None = None
        for _ in range(HISTORY_EXPORT_MAX_PAGES):
            page = await api.async_export_log_rows(start, HISTORY_EXPORT_PAGE_ROWS)
            if page is None:
                return None
            if page.rows and steps is None:
                steps = journal_steps_by_file(await api.async_get_clock_journal())
            if steps:
                for raw in page.rows:
                    if rebase_row(raw, steps):
                        result.rows_rebased += 1
            if page.unsupported_format:
                result.errors.append(
                    "the device's parameter log is not JSON lines or CSV: "
                    "nothing to catch up (set the logger format on the device)",
                )
                return rows
            if page.cursor is None and page.rows:
                # The device's answer was cut (no cursor line): nothing of
                # this run is trusted, the stored cursor stays and a run a
                # minute later reads the pages again. Firmware before
                # 6.0.0-beta.8 glued a cut row to the cursor line on about
                # one page in sixteen (bench run 5, 2026-11-06); importing
                # the rows before the cut would seal a half-read hour.
                result.pages += 1
                result.errors.append(
                    "export page without a cursor line (the device cut the "
                    "answer; firmware before 6.0.0-beta.8 does so on about "
                    "one page in sixteen): read again in a minute",
                )
                result.more_pending = True
                self._marks = []
                self._end_cursor = stored
                return []
            result.pages += 1
            max_ts: float | None = None
            for raw in page.rows:
                ts = row_timestamp(raw)
                if ts is not None and (max_ts is None or ts > max_ts):
                    max_ts = ts
            self._marks.append(_PageMark(start, max_ts))
            rows.extend(page.rows)
            if page.cursor is not None:
                self._end_cursor = page.cursor
            if not page.more:
                break
            if page.cursor is None or page.cursor == start:
                # Device did not advance the cursor; stop rather than loop.
                result.errors.append("export cursor did not advance")
                break
            if len(rows) >= HISTORY_MAX_ROWS_PER_SYNC:
                result.more_pending = True
                break
            start = page.cursor
        else:
            result.more_pending = True
        return rows

    @staticmethod
    def _select_log_files(
        entries: list[dict[str, Any]],
        active_file: str | None,
        result: HistorySyncResult,
    ) -> list[str]:
        """Pick downloadable rotated log files, bounded and sorted oldest-first."""
        candidates: list[str] = []
        for entry in entries:
            name = entry.get("name")
            if (
                not isinstance(name, str)
                or entry.get("dir")
                or not name.startswith("dl_")
                or not name.endswith((".jsonl", ".csv", ".ndjson", ".log"))
            ):
                continue
            if name == active_file:
                continue  # write-locked; synced after rotation
            size = entry.get("size")
            if isinstance(size, int) and size > HISTORY_MAX_FILE_BYTES:
                result.errors.append(f"file too large, skipped: {name}")
                continue
            candidates.append(name)

        # Names embed the rotation epoch (dl_<epoch>.<ext>); oldest first.
        # Over the cap, the NEWEST files are the ones deferred: their rows
        # stay ahead of the watermark and the next sync picks them up,
        # whereas dropping the oldest would strand rows behind it forever.
        candidates.sort()
        if len(candidates) > HISTORY_MAX_FILES_PER_SYNC:
            result.errors.append(
                f"{len(candidates) - HISTORY_MAX_FILES_PER_SYNC} log file(s) "
                "beyond the per-sync cap were deferred",
            )
            candidates = candidates[:HISTORY_MAX_FILES_PER_SYNC]
        return candidates

    async def _async_fetch_files(
        self,
        api: MeatPiApiClient,
        result: HistorySyncResult,
    ) -> list[Any]:
        """Fetch rows by downloading rotated log files (no export route).

        The per-row time-window filter in :func:`aggregate_rows` enforces
        the watermark; this fetch only bounds volume.
        """
        status = await api.async_get_logger_status()
        result.device = self._device_summary(status)
        log_dir = "/sd/logs"
        active_file: str | None = None
        if status is not None:
            raw_dir = status.get("dir")
            if isinstance(raw_dir, str) and raw_dir.startswith("/"):
                log_dir = raw_dir
            raw_active = status.get("file")
            if isinstance(raw_active, str) and raw_active:
                active_file = raw_active

        entries = await api.async_fs_list(log_dir)
        candidates = self._select_log_files(entries, active_file, result)

        rows: list[Any] = []
        for name in candidates:
            try:
                text = await api.async_fs_download(
                    f"{log_dir}/{name}", max_bytes=HISTORY_MAX_FILE_BYTES,
                )
            except MeatPiApiError as err:
                result.errors.append(f"download failed for {name}: {err}")
                continue
            file_rows, bad = (
                parse_csv(text) if name.endswith(".csv") else parse_jsonl(text)
            )
            result.rows_rejected += bad
            rows.extend(file_rows)
            if len(rows) >= HISTORY_MAX_ROWS_PER_SYNC:
                break
        return rows

    # -- the History page --------------------------------------------------

    def _state_series(
        self, gaps: list[tuple[float, float]], after: float,
    ) -> list[StateSeries]:
        """What this run's rows hold of the gaps, per sensor that takes states."""
        first = min(gap[0] for gap in gaps)
        last = max(gap[1] for gap in gaps)
        by_name: dict[str, list[tuple[float, float]]] = {}
        for raw in self._raw_rows:
            row = normalize_row(raw)
            if row is None or row[0] <= after or not first < row[0] < last:
                continue
            ts, name, value = row
            if name in by_name or len(by_name) < HISTORY_MAX_PIDS_PER_SYNC:
                by_name.setdefault(name, []).append((ts, value))
        series: list[StateSeries] = []
        for name, target in self._resolve_entities(list(by_name)).items():
            convert = unit_converter(target.logged_unit, target.unit)
            if convert is not None:
                series.append(
                    StateSeries(
                        target.entity_id, by_name[name], convert, target.decimals,
                    ),
                )
        return series

    async def _async_fill_states(
        self, now_ts: float, result: HistorySyncResult,
    ) -> None:
        """Write the logged values of the gaps as states (history_states.py)."""
        options = getattr(self._entry, "options", None) or {}
        enabled = bool(options.get(CONF_HISTORY_STATES, DEFAULT_HISTORY_STATES))
        data = await self._async_load()
        gaps = self._stored_gaps(data)
        after = _coerce_float(data.get("states_until")) or 0.0
        series = self._state_series(gaps, after) if enabled and gaps else []
        policy = integration_policy(self._hass)
        fill = await async_fill_states(
            self._hass,
            enabled=enabled,
            approved=policy.history_states_schemas,
            blocked=policy.history_states_blocked,
            series=series,
            gaps=gaps,
            after_ts=after,
            step=options.get(CONF_HISTORY_STATES_STEP, DEFAULT_HISTORY_STATES_STEP),
        )
        result.states = {
            "active": fill.active,
            "note": fill.note,
            "recorder_schema": fill.recorder_schema,
            "gaps": fill.gaps,
            "rows_in_gaps": fill.rows_in_gaps,
            "states_written": fill.states_written,
            "sensors": fill.sensors,
            # the spacing asked for in the options, the one this run wrote
            # with (wider when the run was over its cap) and what the asked
            # one alone came to
            "step": fill.step,
            "step_used": fill.step_used,
            "states_at_step": fill.states_at_step,
            "errors": list(fill.errors),
        }
        updates: dict[str, Any] = {}
        if fill.until is not None and fill.until > after:
            updates["states_until"] = fill.until
        if (
            gaps
            and result.source == "export"
            and not result.more_pending
            and not fill.errors
        ):
            # the log was read to its end: every silence that ended before
            # this run is dealt with (a write that failed is tried again)
            left = [list(gap) for gap in gaps if gap[1] > now_ts]
            if len(left) != len(gaps):
                updates["gaps"] = left or None  # None removes the key
        if updates:
            try:
                await self._async_put(**updates)
            except Exception as err:  # storage failure: the fill repeats, harmlessly
                _LOGGER.warning("History sync: saving the History marks failed: %s", err)

    # -- the hours kept between two runs ------------------------------------

    async def _async_keep_deferred(self, result: HistorySyncResult) -> None:
        """Keep the hourly figures of the rows this run deferred.

        The next run reads those rows again from the device (the cursor
        rolls back for them); the copy is for the run that finds the
        device asleep. A later pull's figures replace an earlier pull's
        unless they hold fewer rows (the device retired the file).
        """
        if not result.ran or not self._deferred_buckets:
            return
        try:
            data = await self._async_load()
            kept = data.get("kept")
            kept = dict(kept) if isinstance(kept, dict) else {}
            for name, hours in self._deferred_buckets.items():
                for hour, bucket in hours.items():
                    slot = kept.get(str(hour))
                    slot = dict(slot) if isinstance(slot, dict) else {}
                    old = _Bucket.from_list(slot.get(name))
                    if old is None or bucket.count >= old.count:
                        slot[name] = bucket.as_list()
                    kept[str(hour)] = slot
            await self._async_put(kept=kept)
        except Exception as err:  # storage failure: only the copy is missing
            _LOGGER.warning("History sync: keeping the deferred hours failed: %s", err)

    async def _async_import_kept(
        self,
        now_ts: float,
        result: HistorySyncResult,
        covered: set[tuple[str, int]],
    ) -> None:
        """Import the kept hours that have ended and this run did not get.

        ``covered`` names the (sensor name, hour) pairs this run imported
        from the device's own rows: their copies are dropped unused. The
        watermark does not move for an hour imported from a copy, so the
        device's rows of that hour replace it at its next wake.
        """
        try:
            data = await self._async_load()
            kept = data.get("kept")
            if not isinstance(kept, dict) or not kept:
                return
            left, buckets = split_kept(kept, now_ts, covered)
            resolved = self._resolve_entities(list(buckets)) if buckets else {}
            if resolved:
                earliest = min(hour for name in resolved for hour in buckets[name])
                existing = await self._async_existing_hours(
                    {target.entity_id for target in resolved.values()},
                    dt_util.utc_from_timestamp(earliest),
                )
                before = result.hours_imported
                _, failed_floor = await self._async_import_buckets(
                    resolved, buckets, existing, result,
                )
                result.hours_from_kept_copy += result.hours_imported - before
                if failed_floor is not None:
                    # the recorder refused: the copies stay for the next run
                    for hour_key, names in kept.items():
                        if str(hour_key).isdigit() and int(hour_key) >= failed_floor:
                            left.setdefault(hour_key, names)
            await self._async_put(kept=left or None)
        except Exception as err:  # the copy is a second chance, never a failure
            _LOGGER.warning("History sync: importing the kept hours failed: %s", err)
            result.errors.append(f"kept hours: {err}")

    # -- importing ---------------------------------------------------------

    async def _async_existing_hours(
        self,
        entity_ids: set[str],
        start: datetime,
    ) -> dict[str, set[int]]:
        """Return hours that already have statistics per entity."""
        stats = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass,
            start,
            None,
            entity_ids,
            "hour",
            None,
            {"mean"},
        )
        existing: dict[str, set[int]] = {}
        for entity_id, rows in stats.items():
            hours: set[int] = set()
            for row in rows:
                raw_start = row.get("start")
                ts = (
                    raw_start
                    if isinstance(raw_start, (int, float))
                    else getattr(raw_start, "timestamp", lambda: None)()
                )
                if isinstance(ts, (int, float)):
                    hours.add(int(ts))
            existing[entity_id] = hours
        return existing

    async def _async_entity_metadata(
        self,
        entity_id: str,
        unit: str | None,
    ) -> dict[str, Any] | None:
        """Build import metadata for an entity, reusing existing stats metadata.

        Returns None when the entity's existing statistics are incompatible
        (sum-type): importing mean rows there would corrupt them.
        """
        metadata_map = await get_instance(self._hass).async_add_executor_job(
            partial(get_metadata, self._hass, statistic_ids={entity_id}),
        )
        if entity_id in metadata_map:
            existing = metadata_map[entity_id][1]
            if existing.get("has_sum") or (
                existing.get("mean_type") == StatisticMeanType.NONE
            ):
                return None
            return {
                "source": "recorder",
                "statistic_id": entity_id,
                "name": None,
                "unit_of_measurement": existing.get("unit_of_measurement"),
                "unit_class": existing.get("unit_class"),
                "mean_type": existing.get(
                    "mean_type", StatisticMeanType.ARITHMETIC,
                ),
                "has_sum": False,
            }

        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
        return {
            "source": "recorder",
            "statistic_id": entity_id,
            "name": None,
            "unit_of_measurement": unit,
            "unit_class": converter.UNIT_CLASS if converter else None,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
        }

    def _logged_unit(self, name: str, device_key: str | None) -> str | None:
        """The unit the device's values of this name are in, when known."""
        if device_key is not None:
            for description in SENSOR_DESCRIPTIONS:
                if description.key == device_key:
                    return description.native_unit_of_measurement
            return None
        # the sensor as it runs; else as its stored description builds it
        from .sensor import (  # noqa: PLC0415  (sensor.py imports this package)
            DYNAMIC_PID_SENSORS,
            _build_pid_entity_description,
        )

        entity = DYNAMIC_PID_SENSORS.get(self._entry.entry_id, {}).get(name)
        if entity is not None:
            return entity.native_unit_of_measurement
        config = (getattr(self._entry, "data", None) or {}).get("config")
        pid_config = config.get(name) if isinstance(config, dict) else None
        if isinstance(pid_config, dict):
            return _build_pid_entity_description(
                name, pid_config,
            ).native_unit_of_measurement
        return None

    def _resolve_entities(self, names: list[str]) -> dict[str, _Target]:
        """Map logged names to the sensors they fill.

        A PID name fills the PID sensor of that name; ``device.<key>`` the
        status sensor of that key. Names without a sensor are left out.
        """
        registry = er.async_get(self._hass)
        resolved: dict[str, _Target] = {}
        prefix = f"{HISTORY_DEVICE_SOURCE}."
        for name in names:
            device_key = name[len(prefix):] if name.startswith(prefix) else None
            unique_id = (
                f"{self._entry.entry_id}_{device_key}"
                if device_key is not None
                else f"{self._entry.entry_id}_pid_{name}"
            )
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is None:
                continue
            registry_entry = registry.async_get(entity_id)
            unit = (
                registry_entry.unit_of_measurement
                if registry_entry is not None
                else None
            )
            try:
                logged_unit = self._logged_unit(name, device_key)
            except Exception:  # a description that does not build
                logged_unit = None
            resolved[name] = _Target(
                entity_id,
                unit,
                logged_unit,
                6 if name in ("gps_latitude", "gps_longitude") else PID_VALUE_DECIMALS,
            )
        return resolved

    def _prefer_live(self) -> bool:
        options = getattr(self._entry, "options", None) or {}
        return bool(options.get(CONF_HISTORY_PREFER_LIVE, DEFAULT_HISTORY_PREFER_LIVE))

    # -- the sync ----------------------------------------------------------

    async def _async_import_buckets(
        self,
        resolved: dict[str, _Target],
        buckets: dict[str, dict[int, _Bucket]],
        existing_hours: dict[str, set[int]],
        result: HistorySyncResult,
    ) -> tuple[float, float | None]:
        """Import per-PID hourly buckets.

        Returns ``(max_imported_ts, failed_floor)``: the newest imported
        hour end, and the earliest hour whose import *failed* (None when
        every import succeeded). The watermark must never pass
        ``failed_floor``, or the failed hours would be unrecoverable.
        Permanently incompatible entities (sum-type statistics) do not
        hold the watermark: retrying cannot fix them.

        Each PID is isolated: one failing import never blocks the others.
        Hours that already have live statistics are replaced by the
        recorded ones (the log is the complete record of that hour) unless
        the entry prefers the live statistics, in which case they are
        skipped.

        The device logs in its own unit; the figures are converted into
        the unit the sensor's statistics are kept in. A sensor whose two
        units Home Assistant cannot convert between is not imported (and,
        like a sum-type one, does not hold the watermark).
        """
        prefer_live = self._prefer_live()
        max_imported_ts = 0.0
        failed_floor: float | None = None
        for name, target in resolved.items():
            entity_id = target.entity_id
            taken = existing_hours.get(entity_id, set())
            pending: list[int] = []
            skipped_hours = 0
            overwritten = 0
            for hour in sorted(buckets[name]):
                if hour in taken:
                    if prefer_live:
                        skipped_hours += 1
                        continue
                    overwritten += 1
                pending.append(hour)
            result.hours_skipped_existing += skipped_hours
            if not pending:
                continue
            try:
                metadata = await self._async_entity_metadata(entity_id, target.unit)
                if metadata is None:
                    result.errors.append(
                        f"{entity_id}: incompatible existing statistics",
                    )
                    continue
                kept_unit = metadata.get("unit_of_measurement")
                convert = unit_converter(target.logged_unit, kept_unit)
                if convert is None:
                    result.errors.append(
                        f"{entity_id}: the device logs {target.logged_unit}, the "
                        f"statistics are kept in {kept_unit} and Home Assistant "
                        "has no conversion between the two: not imported",
                    )
                    continue
                statistics = statistics_rows(pending, buckets[name], convert)
                async_import_statistics(self._hass, metadata, statistics)  # type: ignore[arg-type]
            except Exception as err:  # isolate per PID
                _LOGGER.warning(
                    "History sync: import failed for %s: %s", entity_id, err,
                )
                result.errors.append(f"{entity_id}: {err}")
                if failed_floor is None or pending[0] < failed_floor:
                    failed_floor = float(pending[0])
                continue
            result.pids_imported += 1
            result.hours_imported += len(pending)
            result.hours_overwritten_live += overwritten
            last_hour_end = max(hour for hour in buckets[name]) + 3600
            max_imported_ts = max(max_imported_ts, last_hour_end)
        return max_imported_ts, failed_floor

    async def _async_collect(
        self,
        api: MeatPiApiClient,
        since: float,
        now_ts: float,
        result: HistorySyncResult,
    ) -> tuple[dict[str, _Target], dict[str, dict[int, _Bucket]]] | None:
        """Fetch, aggregate, and resolve; None when there is nothing to import."""
        try:
            raw_rows = await self._async_fetch_export(api, since, result)
            if raw_rows is None:
                result.source = "files"
                raw_rows = await self._async_fetch_files(api, result)
            else:
                result.source = "export"
        except MeatPiApiError as err:
            result.errors.append(f"fetch failed: {err}")
            return None

        result.ran = True
        result.rows_fetched = len(raw_rows)
        self._raw_rows = raw_rows
        if not raw_rows:
            return None

        current_hour = open_hour(now_ts)
        self._deferred_from = None
        for raw in raw_rows:
            ts = row_timestamp(raw)
            if ts is not None and ts >= since and _deferred(
                ts, now_ts=now_ts, current_hour=current_hour,
            ) and (self._deferred_from is None or ts < self._deferred_from):
                self._deferred_from = ts

        buckets = aggregate_rows(
            raw_rows,
            since=since,
            now_ts=now_ts,
            result=result,
            deferred=self._deferred_buckets,
        )
        if not buckets:
            return None

        resolved = self._resolve_entities(list(buckets))
        result.pids_skipped = len(buckets) - len(resolved)
        if not resolved:
            return None
        return resolved, buckets

    def _cursor_to_save(self, hold_ts: float | None) -> str | None:
        """The device cursor the next run starts from.

        Rows at or after ``hold_ts`` (the earliest deferred row, a failed
        PID's first hour) must be read again: the cursor rolls back to
        the start of the first page that carried such a row. With nothing
        to hold, the pull continues where the device stopped.
        """
        if hold_ts is not None:
            for mark in self._marks:
                if mark.max_ts is not None and mark.max_ts >= hold_ts:
                    return mark.start_cursor
        return self._end_cursor

    async def async_sync(self) -> HistorySyncResult:
        """Run one catch-up pass. Never raises."""
        result = HistorySyncResult()
        runtime = getattr(self._entry, "runtime_data", None)
        if runtime is None:
            return result
        api = getattr(runtime, "api", None)
        now_ts = dt_util.utcnow().timestamp()
        self._raw_rows = []
        self._deferred_buckets = {}
        try:
            await self._async_note_gaps(runtime, now_ts)
        except Exception as err:  # the History fill's input only
            _LOGGER.warning("History sync: noting the gaps failed: %s", err)
        if api is None:
            # no address to ask: the hours an earlier pull kept still go in
            await self._async_import_kept(now_ts, result, set())
            return result

        try:
            await self._async_reread_after_upgrade()
        except Exception as err:  # the marks stay as they are
            _LOGGER.warning("History sync: the marks could not be checked: %s", err)
        watermark = await self.async_get_watermark()
        since = max(watermark, now_ts - HISTORY_MAX_AGE_DAYS * 86400)

        collected = await self._async_collect(api, since, now_ts, result)
        if result.ran:
            # the History page first: states have no hourly rule, the whole
            # away time shows the moment the car is home
            await self._async_fill_states(now_ts, result)
        if collected is None:
            await self._async_import_kept(now_ts, result, set())
            await self._async_keep_deferred(result)
            await self._async_finish(result, watermark, watermark, None, now_ts)
            return result
        resolved, buckets = collected

        earliest_hour = min(
            hour for pid in resolved for hour in buckets[pid]
        )
        try:
            existing_hours = await self._async_existing_hours(
                {target.entity_id for target in resolved.values()},
                dt_util.utc_from_timestamp(earliest_hour),
            )
        except Exception as err:  # recorder unavailable: never break setup
            _LOGGER.warning("History sync: cannot query recorder: %s", err)
            result.errors.append(f"recorder query failed: {err}")
            return result

        max_imported_ts, failed_floor = await self._async_import_buckets(
            resolved, buckets, existing_hours, result,
        )

        new_watermark = max_imported_ts
        if failed_floor is not None:
            # A failed PID's hours must stay ahead of the watermark so
            # the next sync re-fetches and re-imports them (idempotent).
            new_watermark = min(new_watermark, failed_floor)
        if not result.pids_imported or new_watermark <= watermark:
            new_watermark = watermark

        # what an earlier pull kept and this one did not bring (the device
        # lost the rows): imported from the copy; then this run's own
        # deferred hours are kept for the next one
        await self._async_import_kept(
            now_ts,
            result,
            {(name, hour) for name in resolved for hour in buckets[name]},
        )
        await self._async_keep_deferred(result)

        await self._async_finish(result, watermark, new_watermark, failed_floor, now_ts)

        _LOGGER.info(
            "History sync for %s (%s): %d rows in %d page(s) -> %d hour(s) "
            "across %d sensor(s); %d row(s) of the current hour deferred; "
            "%d row(s) rejected%s",
            self._entry.title,
            result.source,
            result.rows_fetched,
            result.pages,
            result.hours_imported,
            result.pids_imported,
            result.rows_deferred,
            result.rows_rejected,
            f"; follow-up in {result.followup_in:.0f} s"
            if result.followup_in else "",
        )
        return result

    async def _async_finish(
        self,
        result: HistorySyncResult,
        old_watermark: float,
        new_watermark: float,
        failed_floor: float | None,
        now_ts: float,
    ) -> None:
        """Save the marks a run earned and say when to come back."""
        result.finished_at = now_ts
        if not result.ran:
            return
        if result.source != "export":
            # The file path has no cursor; the time watermark is the mark.
            if result.pids_imported and new_watermark > old_watermark:
                result.watermark = new_watermark
                await self._async_save_marks(new_watermark, None, result)
            result.followup_in = followup_delay(result, now_ts)
            return

        hold_candidates = [
            ts for ts in (self._deferred_from, failed_floor) if ts is not None
        ]
        # Hours the row cap cut short stay ahead of the watermark: read
        # them again from the watermark on.
        if result.more_pending and result.rows_used >= HISTORY_MAX_ROWS_PER_SYNC:
            hold_candidates.append(new_watermark)
        hold_ts = min(hold_candidates) if hold_candidates else None
        cursor = self._cursor_to_save(hold_ts)
        result.cursor = cursor
        result.followup_in = followup_delay(result, now_ts)
        stored_cursor = await self.async_get_cursor()
        if result.watermark is None and new_watermark > old_watermark:
            result.watermark = new_watermark
        if new_watermark != old_watermark or cursor != stored_cursor:
            await self._async_save_marks(new_watermark, cursor, result)

    async def _async_save_marks(
        self, watermark: float, cursor: str | None, result: HistorySyncResult,
    ) -> None:
        try:
            await self._async_save(watermark, cursor)
        except Exception as err:  # storage failure only re-syncs later
            _LOGGER.warning("History sync: saving the marks failed: %s", err)
            result.errors.append(f"watermark save failed: {err}")


async def async_sync_history(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> HistorySyncResult:
    """Run one history catch-up pass for a config entry. Never raises."""
    try:
        return await HistorySync(hass, entry).async_sync()
    except Exception:  # Defensive: the catch-up must never break anything else.
        _LOGGER.exception("Unexpected error during history sync")
        return HistorySyncResult()


async def async_kept_hours_due_in(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> float | None:
    """Seconds until the earliest kept hour can be imported; None for none.

    Home Assistant restarted between the pull at the car's return and the
    end of that hour: the follow-up's timer is gone with the old run, the
    kept hours are on disk. The entry's setup books a run with this.
    """
    try:
        data = await HistorySync(hass, entry)._async_load()  # noqa: SLF001
    except Exception:  # storage unreadable: nothing kept
        return None
    kept = data.get("kept")
    hours = [
        int(key) for key in (kept if isinstance(kept, dict) else {}) if str(key).isdigit()
    ]
    if not hours:
        return None
    now_ts = dt_util.utcnow().timestamp()
    return max(
        float(HISTORY_FOLLOWUP_MORE_DELAY),
        min(hours) + 3600 + HISTORY_FOLLOWUP_HOUR_MARGIN - now_ts,
    )


async def async_remove_history_store(
    hass: HomeAssistant,
    entry: MeatPiConfigEntry,
) -> None:
    """Delete the marks store when the config entry is removed."""
    await HistorySync(hass, entry).async_remove()
