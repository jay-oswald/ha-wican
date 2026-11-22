"""The catch-up after 2.0.0-beta1: units, the last hour, the History page.

What users met with 2.0.0-beta1 (found with the firmware's drive bench,
`test.ps1 hadrive`, 2026-11-22):

- a logged value went into a series Home Assistant keeps in another unit
  as it came (79 °C as "79 °F"),
- the hour a car came home in was never imported when the device slept
  before that hour ended,
- the History page stayed empty over a drive, the import being statistics
  only,
- the device's own battery voltage was not in the log at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder import Recorder, history as recorder_history
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.wican.api import LogExportPage, MeatPiApiClient
from custom_components.wican.catalog import (
    IntegrationPolicy,
    integration_policy,
    parse_integration_policy,
)
from custom_components.wican.const import (
    CONF_HISTORY_STATES,
    CONF_HISTORY_STATES_STEP,
    DEFAULT_HISTORY_STATES_STEP,
    DOMAIN,
    HISTORY_HOUR_SETTLE,
    HISTORY_STATES_MAX_PER_RUN,
    HISTORY_STATES_RECORDER_SCHEMAS,
    MAX_HISTORY_STATES_STEP,
)
from custom_components.wican.exceptions import MeatPiApiError
from custom_components.wican.history import (
    HistorySync,
    HistorySyncResult,
    aggregate_rows,
    async_kept_hours_due_in,
    async_sync_history,
    normalize_row,
    open_hour,
)
from custom_components.wican.history_states import (
    StateFillResult,
    StateSeries,
    async_fill_states,
    coerce_step,
    format_state,
    points_in_gaps,
    schema_allowed,
    select_states,
    thin,
)
from custom_components.wican.history_units import unit_converter

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

# A fixed clock: 10:20 of a day, so "the hour still running" is 10:00.
T0 = datetime(2026, 11, 22, 10, 20, 0, tzinfo=UTC)
H10 = int(datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC).timestamp())
H9 = H10 - 3600
H8 = H10 - 7200


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str,
    enable_custom_integrations: None,
) -> None:
    """The recorder's database fixture before the first hass (as in the e2e tests)."""
    return


@pytest.fixture
def mock_recorder_calls():
    """Mock the recorder touchpoints of the statistics import."""
    with (
        patch("custom_components.wican.history.get_instance") as instance,
        patch(
            "custom_components.wican.history.statistics_during_period",
            return_value={},
        ) as during,
        patch(
            "custom_components.wican.history.get_metadata", return_value={},
        ) as metadata,
        patch(
            "custom_components.wican.history.async_import_statistics",
        ) as import_stats,
        # the History fill has its own tests below, against a real recorder
        patch(
            "custom_components.wican.history.async_fill_states",
            AsyncMock(return_value=StateFillResult()),
        ) as fill,
    ):

        async def _run_job(func: Any, *args: Any) -> Any:
            return func(*args)

        instance.return_value.async_add_executor_job = AsyncMock(side_effect=_run_job)
        yield {"import": import_stats, "during": during, "metadata": metadata, "fill": fill}


async def _entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    *,
    pids: dict[str, tuple[str | None, str | None]],
) -> MockConfigEntry:
    """Set the integration up with PID sensors: {name: (device unit, shown unit)}."""
    config = {
        name: {"unit": device_unit} for name, (device_unit, _) in pids.items() if device_unit
    }
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, "config": config},
    )
    with patch(
        "custom_components.wican._async_register_webhook_on_device", return_value=True,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    for name, (_, shown_unit) in pids.items():
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{mock_config_entry.entry_id}_pid_{name}",
            unit_of_measurement=shown_unit,
        )
    mock_config_entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    return mock_config_entry


def _one_page(rows: list[dict[str, Any]], cursor: str = "1793900000:9") -> AsyncMock:
    return AsyncMock(side_effect=[LogExportPage(rows=list(rows), cursor=cursor)])


def _imported(mock: MagicMock) -> dict[str, list[dict[str, Any]]]:
    """{statistic id: the rows of its last import call}."""
    return {call.args[1]["statistic_id"]: call.args[2] for call in mock.call_args_list}


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_unit_converter_rules() -> None:
    """Same or unknown units pass; known pairs convert; strangers refuse."""
    assert unit_converter("°C", "°C")(79.0) == 79.0
    assert unit_converter(None, "°F")(79.0) == 79.0
    assert unit_converter("°C", None)(79.0) == 79.0
    assert unit_converter("°C", "°F")(79.0) == pytest.approx(174.2)
    assert unit_converter("°C", "°F")(-40.0) == pytest.approx(-40.0)
    assert unit_converter("kPa", "hPa")(101.0) == pytest.approx(1010.0)
    assert unit_converter("km/h", "mph")(110.0) == pytest.approx(68.35, abs=0.01)
    assert unit_converter("km", "mi")(100.0) == pytest.approx(62.137, abs=0.001)
    # both known, no conversion between them: never written
    assert unit_converter("km/h", "°C") is None
    assert unit_converter("rpm", "°C") is None


async def test_hours_are_converted_into_the_unit_of_the_statistics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """A US system: the coolant's hour, logged in °C, is imported in °F."""
    freezer.move_to(T0)
    entry = await _entry(
        hass,
        mock_config_entry,
        pids={
            "EngineCoolantTemp": ("°C", "°F"),
            "VehicleSpeed": ("km/h", "mph"),
            "EngineRPM": ("rpm", "rpm"),
        },
    )
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [
            {"ts": (H9 + 60) * 1000, "param": "autopid.EngineCoolantTemp", "value": 30},
            {"ts": (H9 + 120) * 1000, "param": "autopid.EngineCoolantTemp", "value": 92},
            {"ts": (H9 + 60) * 1000, "param": "autopid.VehicleSpeed", "value": 0},
            {"ts": (H9 + 120) * 1000, "param": "autopid.VehicleSpeed", "value": 110},
            {"ts": (H9 + 60) * 1000, "param": "autopid.EngineRPM", "value": 800},
            {"ts": (H9 + 120) * 1000, "param": "autopid.EngineRPM", "value": 3000},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.hours_imported == 3
    assert result.errors == []
    stats = _imported(mock_recorder_calls["import"])
    registry = er.async_get(hass)

    def rows_of(name: str) -> dict[str, Any]:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_pid_{name}",
        )
        return stats[entity_id][0]

    coolant = rows_of("EngineCoolantTemp")
    assert coolant["min"] == pytest.approx(86.0)
    assert coolant["max"] == pytest.approx(197.6)
    assert coolant["mean"] == pytest.approx(141.8)
    speed = rows_of("VehicleSpeed")
    assert speed["min"] == pytest.approx(0.0)
    assert speed["max"] == pytest.approx(68.35, abs=0.01)
    rpm = rows_of("EngineRPM")
    assert (rpm["min"], rpm["max"], rpm["mean"]) == (800, 3000, 1900)


async def test_the_unit_of_existing_statistics_wins_over_the_shown_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Statistics keep the unit they began with; the import follows them."""
    freezer.move_to(T0)
    entry = await _entry(hass, mock_config_entry, pids={"AbsBaroPres": ("kPa", "inHg")})
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_AbsBaroPres",
    )
    # the series began on a metric system: hPa
    mock_recorder_calls["metadata"].return_value = {
        entity_id: (1, {"unit_of_measurement": "hPa", "unit_class": "pressure",
                        "has_sum": False, "mean_type": 1}),
    }
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [{"ts": (H9 + 60) * 1000, "param": "autopid.AbsBaroPres", "value": 101}],
    )

    result = await async_sync_history(hass, entry)

    assert result.hours_imported == 1
    call = mock_recorder_calls["import"].call_args
    assert call.args[1]["unit_of_measurement"] == "hPa"
    assert call.args[2][0]["mean"] == pytest.approx(1010.0)


async def test_units_home_assistant_cannot_convert_are_not_imported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """No number at all is better than a wrong one; the others still import."""
    freezer.move_to(T0)
    entry = await _entry(
        hass,
        mock_config_entry,
        pids={"Odd": ("km/h", "°C"), "SOC": ("%", "%")},
    )
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [
            {"ts": (H9 + 60) * 1000, "param": "autopid.Odd", "value": 50},
            {"ts": (H9 + 60) * 1000, "param": "autopid.SOC", "value": 71},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    assert result.hours_imported == 1
    assert any("no conversion" in error for error in result.errors)
    # the sensor that cannot be imported does not hold the watermark
    assert result.watermark == H9 + 3600


async def test_hours_an_earlier_version_imported_unconverted_are_read_again(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Marks of 2.0.0-beta1 and a sensor kept in °F: the log is read from its start, once."""
    freezer.move_to(T0)
    entry = await _entry(hass, mock_config_entry, pids={"EngineCoolantTemp": ("°C", "°F")})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "pid_keys": ["EngineCoolantTemp"]},
    )
    await hass.async_block_till_done()
    entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    # what 2.0.0-beta1 left: the hours up to 09:00 imported, the cursor behind them
    await HistorySync(hass, entry)._store.async_save(  # noqa: SLF001
        {"watermark": float(H9), "cursor": "1793900000:4096"},
    )
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [{"ts": (H8 + 60) * 1000, "param": "autopid.EngineCoolantTemp", "value": 79}],
    )

    result = await async_sync_history(hass, entry)

    # read from the oldest moment the import takes, not from the stored cursor
    since = entry.runtime_data.api.async_export_log_rows.call_args.args[0]
    assert since == str(int(T0.timestamp() - 30 * 86400))
    assert result.hours_imported == 1
    assert mock_recorder_calls["import"].call_args.args[2][0]["mean"] == pytest.approx(174.2)
    stored = await HistorySync(hass, entry)._async_load()  # noqa: SLF001
    assert stored["generation"] == 2

    # once: the next run goes on from its own marks
    entry.runtime_data.api.async_export_log_rows = _one_page([])
    await async_sync_history(hass, entry)
    assert entry.runtime_data.api.async_export_log_rows.call_args.args[0] == stored["cursor"]


async def test_marks_of_an_entry_without_such_a_sensor_are_left_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every sensor in the device's own unit: nothing was imported wrong."""
    freezer.move_to(T0)
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    hass.config_entries.async_update_entry(entry, data={**entry.data, "pid_keys": ["SOC"]})
    await hass.async_block_till_done()
    entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    await HistorySync(hass, entry)._store.async_save(  # noqa: SLF001
        {"watermark": float(H9), "cursor": "1793900000:4096"},
    )
    entry.runtime_data.api.async_export_log_rows = _one_page([])

    await async_sync_history(hass, entry)

    assert entry.runtime_data.api.async_export_log_rows.call_args.args[0] == "1793900000:4096"
    stored = await HistorySync(hass, entry)._async_load()  # noqa: SLF001
    assert stored["watermark"] == H9
    assert stored["generation"] == 2


# ---------------------------------------------------------------------------
# The device's own values
# ---------------------------------------------------------------------------


def test_device_rows_keep_their_source() -> None:
    """`device.batt_voltage` never meets a PID of the same name."""
    assert normalize_row(
        {"ts": 1795281224006, "param": "device.batt_voltage", "value": 13.86},
    ) == (1795281224.006, "device.batt_voltage", 13.86)
    # a PID that happens to be called the same stays a PID
    assert normalize_row(
        {"ts": 1795281224006, "param": "autopid.batt_voltage", "value": 12.0},
    ) == (1795281224.006, "batt_voltage", 12.0)
    # a device key this integration has no sensor for
    assert normalize_row(
        {"ts": 1795281224006, "param": "device.unknown_thing", "value": 1},
    ) is None


async def test_battery_voltage_fills_the_device_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The log's battery voltage goes to the Battery Voltage sensor."""
    freezer.move_to(T0)
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_batt_voltage",
    )
    assert entity_id is not None
    # a measurement: Home Assistant asks for a repair of a sensor that has
    # statistics and no state class
    assert er.async_get(hass).async_get(entity_id).capabilities == {
        "state_class": "measurement",
    }
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [
            {"ts": (H9 + 10) * 1000, "param": "device.batt_voltage", "value": 14.2},
            {"ts": (H9 + 20) * 1000, "param": "device.batt_voltage", "value": 13.8},
            {"ts": (H9 + 20) * 1000, "param": "autopid.SOC", "value": 71},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 2
    stats = _imported(mock_recorder_calls["import"])
    assert stats[entity_id][0]["mean"] == pytest.approx(14.0)
    assert stats[entity_id][0]["min"] == pytest.approx(13.8)
    assert stats[entity_id][0]["max"] == pytest.approx(14.2)


# ---------------------------------------------------------------------------
# The hour that is not imported yet
# ---------------------------------------------------------------------------


def test_an_hour_waits_until_home_assistant_wrote_its_own_row() -> None:
    """The first seconds past the top of an hour still belong to the old one."""
    assert open_hour(H10 + 1200) == H10
    assert open_hour(H10 + HISTORY_HOUR_SETTLE) == H10
    assert open_hour(H10 + HISTORY_HOUR_SETTLE - 1) == H9
    assert open_hour(H10) == H9

    result = HistorySyncResult()
    deferred: dict[str, Any] = {}
    buckets = aggregate_rows(
        [
            {"ts": H8 + 60, "name": "SOC", "value": 70},
            {"ts": H9 + 60, "name": "SOC", "value": 71},
            {"ts": H9 + 120, "name": "SOC", "value": 75},
        ],
        since=0,
        now_ts=H10 + 5,  # five seconds into the next hour
        result=result,
        deferred=deferred,
    )
    assert set(buckets["SOC"]) == {H8}
    assert result.rows_deferred == 2
    assert deferred["SOC"][H9].count == 2
    assert deferred["SOC"][H9].mean == 73


# ---------------------------------------------------------------------------
# The last hour of a drive
# ---------------------------------------------------------------------------


async def test_the_last_hour_is_imported_from_the_kept_copy_when_the_device_sleeps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Home at 10:50, asleep at 10:56: the 10:00 hour still goes in at 11:01."""
    freezer.move_to(T0 + timedelta(minutes=30))  # 10:50
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    drive = [
        {"ts": (H9 + 1800) * 1000, "param": "autopid.SOC", "value": 80},
        {"ts": (H10 + 600) * 1000, "param": "autopid.SOC", "value": 70},
        {"ts": (H10 + 2400) * 1000, "param": "autopid.SOC", "value": 60},
    ]
    entry.runtime_data.api.async_export_log_rows = _one_page(drive)

    first = await async_sync_history(hass, entry)

    # 09:00 is imported, 10:00 is deferred and kept
    assert first.hours_imported == 1
    assert first.rows_deferred == 2
    assert first.followup_in == pytest.approx(600 + 90)
    stored = await HistorySync(hass, entry)._async_load()  # noqa: SLF001
    assert stored["kept"] == {str(H10): {"SOC": [130.0, 2, 60.0, 70.0]}}
    assert await async_kept_hours_due_in(hass, entry) == pytest.approx(600 + 90)
    watermark = stored["watermark"]
    assert watermark == H10

    # 11:01:30, the device asleep
    freezer.move_to(T0 + timedelta(minutes=41, seconds=30))
    mock_recorder_calls["import"].reset_mock()
    entry.runtime_data.api.async_get_logger_status = AsyncMock(
        side_effect=MeatPiApiError("Cannot connect to host"),
    )
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=MeatPiApiError("Cannot connect to host"),
    )

    second = await async_sync_history(hass, entry)

    assert second.ran is False
    assert second.hours_from_kept_copy == 1
    rows = _imported(mock_recorder_calls["import"])[entity_id]
    assert rows[0]["start"] == dt_util.utc_from_timestamp(H10)
    assert (rows[0]["mean"], rows[0]["min"], rows[0]["max"]) == (65.0, 60.0, 70.0)
    stored = await HistorySync(hass, entry)._async_load()  # noqa: SLF001
    assert "kept" not in stored
    # the watermark and the cursor did not move: the device's own rows of
    # that hour replace the copy at its next wake
    assert stored["watermark"] == watermark
    assert await async_kept_hours_due_in(hass, entry) is None

    # the next wake: the device has more rows of that hour than the copy had
    freezer.move_to(T0 + timedelta(hours=2))
    mock_recorder_calls["import"].reset_mock()
    entry.runtime_data.api.async_get_logger_status = AsyncMock(return_value=None)
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [*drive, {"ts": (H10 + 3000) * 1000, "param": "autopid.SOC", "value": 20}],
    )

    third = await async_sync_history(hass, entry)

    assert third.hours_imported == 1
    assert third.hours_from_kept_copy == 0
    rows = _imported(mock_recorder_calls["import"])[entity_id]
    assert rows[0]["start"] == dt_util.utc_from_timestamp(H10)
    assert (rows[0]["mean"], rows[0]["min"], rows[0]["max"]) == (50.0, 20.0, 70.0)
    assert third.watermark == H10 + 3600


async def test_a_kept_hour_the_device_delivers_is_not_imported_twice(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The device awake at the follow-up: its rows are used, the copy dropped."""
    freezer.move_to(T0 + timedelta(minutes=30))
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    drive = [{"ts": (H10 + 600) * 1000, "param": "autopid.SOC", "value": 70}]
    entry.runtime_data.api.async_export_log_rows = _one_page(drive)
    await async_sync_history(hass, entry)
    assert "kept" in await HistorySync(hass, entry)._async_load()  # noqa: SLF001

    freezer.move_to(T0 + timedelta(minutes=41, seconds=30))
    mock_recorder_calls["import"].reset_mock()
    entry.runtime_data.api.async_export_log_rows = _one_page(drive)

    result = await async_sync_history(hass, entry)

    assert result.hours_imported == 1
    assert result.hours_from_kept_copy == 0
    assert mock_recorder_calls["import"].call_count == 1
    assert "kept" not in await HistorySync(hass, entry)._async_load()  # noqa: SLF001


async def test_a_run_that_did_not_reach_the_device_does_not_hold_the_next_one(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Asleep at the follow-up: the push of its next wake is read at once."""
    from custom_components.wican import _async_request_history_sync  # noqa: PLC0415

    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    entry.runtime_data.last_history_sync = 12345.0
    entry.runtime_data.api.async_get_logger_status = AsyncMock(
        side_effect=MeatPiApiError("Cannot connect to host"),
    )
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=MeatPiApiError("Cannot connect to host"),
    )

    await _async_request_history_sync(hass, entry)

    assert entry.runtime_data.last_history_result.ran is False
    assert entry.runtime_data.last_history_sync == 0.0


async def test_a_return_is_not_dropped_because_the_hours_follow_up_is_booked(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Inside the window after a run: booked for its end, the hour's follow-up or not."""
    import time as _time  # noqa: PLC0415

    from custom_components.wican import (  # noqa: PLC0415
        _schedule_history_followup,
        _schedule_history_sync,
    )
    from custom_components.wican.const import HISTORY_SYNC_MIN_INTERVAL  # noqa: PLC0415

    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    runtime = entry.runtime_data
    runtime.capabilities = MagicMock()
    runtime.capabilities.has_component.return_value = True
    # a run a few seconds ago left the hour's follow-up booked, 40 min away
    runtime.last_history_sync = _time.monotonic() - 5
    _schedule_history_followup(hass, entry, 2400)
    hour_due = runtime.history_followup_due

    _schedule_history_sync(hass, entry)  # the push that ended a silence

    assert runtime.history_followup is not None
    assert runtime.history_followup_due < hour_due
    assert runtime.history_followup_due - _time.time() <= HISTORY_SYNC_MIN_INTERVAL

    # a follow-up that is due sooner than the window's end stays as it is
    _schedule_history_followup(hass, entry, 3)
    soon = runtime.history_followup_due
    _schedule_history_sync(hass, entry)
    assert runtime.history_followup_due == soon
    runtime.history_followup()
    runtime.history_followup = None


# ---------------------------------------------------------------------------
# The History page: the pure rules
# ---------------------------------------------------------------------------


def test_schema_rule() -> None:
    """Built-in versions run; the catalog adds one or blocks one."""
    tested = next(iter(HISTORY_STATES_RECORDER_SCHEMAS))
    assert schema_allowed(tested)
    assert not schema_allowed(tested + 1)
    assert schema_allowed(tested + 1, approved=[tested + 1])
    assert not schema_allowed(tested, blocked=[tested])
    assert not schema_allowed(tested + 1, approved=[tested + 1], blocked=[tested + 1])
    for junk in (None, 0, -1, "53", True, 53.0):
        assert not schema_allowed(junk, approved=[53])


def test_catalog_policy_is_data() -> None:
    """The catalog's integration section: lists of version numbers, else empty."""
    policy = parse_integration_policy(
        {
            "integration": {
                "history_states": {
                    "recorder_schemas": [53, 54, "55", True, -1, 54.5],
                    "blocked_recorder_schemas": [50],
                },
            },
        },
    )
    assert policy.history_states_schemas == frozenset({53, 54})
    assert policy.history_states_blocked == frozenset({50})
    for junk in (None, [], {"integration": 5}, {"integration": {"history_states": []}},
                 {"integration": {"history_states": {"recorder_schemas": "53"}}}):
        assert parse_integration_policy(junk) == IntegrationPolicy()


async def test_the_bundled_catalog_carries_the_policy(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> None:
    """After setup the policy of the catalog in use is the bundled one."""
    assert integration_policy(hass) == IntegrationPolicy()
    await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    assert 53 in integration_policy(hass).history_states_schemas


def test_state_text() -> None:
    """A state reads like the sensor's own: rounded, a whole number bare."""
    assert format_state(800.0, 3) == "800"
    assert format_state(25.0980392156863, 3) == "25.098"
    assert format_state(197.60000000000002, 3) == "197.6"
    assert format_state(-0.0004, 3) == "0"
    assert format_state(51.123456789, 6) == "51.123457"


def test_points_of_the_gaps_only() -> None:
    """Strictly between the last push before and the push that ended it."""
    points = [(100.0, 1.0), (150.0, 2.0), (200.0, 3.0), (250.0, 4.0), (400.0, 5.0)]
    assert points_in_gaps(points, [(100.0, 250.0)], 0.0) == [[(150.0, 2.0), (200.0, 3.0)]]
    # dealt with by an earlier run
    assert points_in_gaps(points, [(100.0, 250.0)], 150.0) == [[(200.0, 3.0)]]
    # two gaps, two lists; a point outside both is nobody's
    assert points_in_gaps(points, [(300.0, 500.0), (100.0, 160.0)], 0.0) == [
        [(150.0, 2.0)],
        [(400.0, 5.0)],
    ]
    assert points_in_gaps(points, [], 0.0) == []


def test_thinning() -> None:
    """A state when the value changed, not more often than the step."""
    states = [(0.0, "1"), (2.0, "1"), (4.0, "2"), (6.0, "3"), (12.0, "3"), (30.0, "3"), (31.0, "4")]
    # the first; "2" and "3" come too soon after it; "3" at 12 s; the last value
    assert thin(states, 10.0) == [(0.0, "1"), (12.0, "3"), (31.0, "4")]
    # a value that never changes is one state
    assert thin([(0.0, "7"), (50.0, "7"), (100.0, "7")], 10.0) == [(0.0, "7")]
    assert thin([], 10.0) == []


def test_a_spacing_of_zero_is_every_change() -> None:
    """history_states_step 0: a state for each change, none for a repeat."""
    states = [(0.0, "1"), (2.0, "1"), (4.0, "2"), (6.0, "3"), (12.0, "3"), (30.0, "3"), (31.0, "4")]
    assert thin(states, 0.0) == [(0.0, "1"), (4.0, "2"), (6.0, "3"), (31.0, "4")]

    # four rows a second for 100 s, the value another one every half second
    points = [(1000.0 + 0.25 * index, float(index // 2)) for index in range(400)]
    series = [StateSeries("sensor.x", points, lambda value: value, 3)]
    gaps = [(999.0, 2000.0)]
    every = StateFillResult()
    chosen = select_states(series, gaps, 0.0, every, 0)
    assert len(chosen["sensor.x"]) == 200
    assert (every.step, every.step_used, every.states_at_step) == (0.0, 0.0, 200)
    # the same rows at a spacing of 10 s: one every 10 s and the last value
    ten = StateFillResult()
    chosen = select_states(series, gaps, 0.0, ten, 10)
    assert [round(ts - 1000.0, 2) for ts, _ in chosen["sensor.x"]] == [
        0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 99.75,
    ]
    assert (ten.step, ten.step_used) == (10.0, 10.0)
    # and with nothing said: the default, one a second and the last value
    plain = StateFillResult()
    chosen = select_states(series, gaps, 0.0, plain)
    assert DEFAULT_HISTORY_STATES_STEP == 1
    assert [round(ts - 1000.0, 2) for ts, _ in chosen["sensor.x"]] == [
        *[float(second) for second in range(100)], 99.75,
    ]
    assert (plain.step, plain.step_used) == (1.0, 1.0)


def test_rows_exactly_one_spacing_apart_are_all_written() -> None:
    """Millisecond stamps as float seconds: a spacing apart does not read as short of it.

    The default spacing is one second and a usual poll table reads a value
    once a second: every such reading must be a point.
    """
    # a value read every second, on the second and off it, at today's epoch
    for first_ms in (1_795_310_000_000, 1_795_310_000_123, 1_795_310_000_999):
        states = [((first_ms + 1000 * index) / 1000, str(index)) for index in range(600)]
        assert len(thin(states, 1.0)) == 600
        # the battery voltage: a row every 10 s at a spacing of 10 s
        tens = [((first_ms + 10_000 * index) / 1000, str(index)) for index in range(600)]
        assert len(thin(tens, 10.0)) == 600
    # a row that is a millisecond short of the spacing still waits
    short = [(1_795_310_000.000, "1"), (1_795_310_000.999, "2"), (1_795_310_001.998, "3")]
    assert thin(short, 1.0) == [short[0], short[2]]


def test_what_an_options_value_comes_to() -> None:
    """A number inside the range as it is, outside its nearest end, junk the default."""
    assert coerce_step(0) == 0.0
    assert coerce_step(5) == 5.0
    assert coerce_step(2.5) == 2.5
    assert coerce_step(-3) == 0.0
    assert coerce_step(10**6) == float(MAX_HISTORY_STATES_STEP)
    for junk in (None, "5", True, float("nan"), [5]):
        assert coerce_step(junk) == float(DEFAULT_HISTORY_STATES_STEP)


def test_a_run_writes_no_more_than_its_cap() -> None:
    """Too many states for one run: thinned further, not cut off in time."""
    count = HISTORY_STATES_MAX_PER_RUN + 5000
    points = [(1000.0 + 10.0 * index, float(index)) for index in range(count)]
    result = StateFillResult()
    chosen = select_states(
        [StateSeries("sensor.x", points, lambda value: value, 3)],
        [(999.0, 1000.0 + 10.0 * count)],
        0.0,
        result,
    )
    assert result.rows_in_gaps == count
    assert len(chosen["sensor.x"]) <= HISTORY_STATES_MAX_PER_RUN
    assert chosen["sensor.x"][0][0] == 1000.0
    assert chosen["sensor.x"][-1][0] == points[-1][0]
    assert result.until == points[-1][0]


def test_a_run_over_its_cap_widens_a_spacing_of_zero() -> None:
    """0 doubled is still 0: the first widening is one second, then it doubles."""
    count = HISTORY_STATES_MAX_PER_RUN + 5000
    points = [(1000.0 + index, float(index)) for index in range(count)]  # a change a second
    result = StateFillResult()
    chosen = select_states(
        [StateSeries("sensor.x", points, lambda value: value, 3)],
        [(999.0, 1000.0 + count)],
        0.0,
        result,
        0,
    )
    assert result.step == 0.0
    assert result.states_at_step == count
    # 1 s keeps every row of this series, 2 s every second one
    assert result.step_used == 2.0
    assert len(chosen["sensor.x"]) == count // 2 + 1
    assert chosen["sensor.x"][0][0] == 1000.0
    assert chosen["sensor.x"][-1][0] == points[-1][0]


# ---------------------------------------------------------------------------
# The History page: against a real recorder
# ---------------------------------------------------------------------------


async def _states_of(hass: HomeAssistant, entity_id: str, start: datetime) -> list[Any]:
    await async_wait_recording_done(hass)
    found = await get_instance(hass).async_add_executor_job(
        partial(
            recorder_history.state_changes_during_period,
            hass,
            start,
            None,
            entity_id,
            include_start_time_state=False,
        ),
    )
    return found.get(entity_id, [])


async def test_states_of_the_past_show_in_history(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """The recorder takes them and History returns them at their own times."""
    now = dt_util.utcnow()
    hass.states.async_set("sensor.coolant", "176", {"unit_of_measurement": "°F"})
    await async_wait_recording_done(hass)

    start = now.timestamp() - 3600
    points = [(start + 10.0 * index, 30.0 + index) for index in range(40)]
    result = await async_fill_states(
        hass,
        enabled=True,
        approved=(),
        blocked=(),
        series=[
            StateSeries("sensor.coolant", points, unit_converter("°C", "°F"), 3),
            # a sensor the recorder never saw a state of: left out, no error
            StateSeries("sensor.unknown", points, lambda value: value, 3),
        ],
        gaps=[(start - 5, start + 1000)],
        after_ts=0.0,
    )

    assert result.active is True
    assert result.errors == []
    assert result.states_written == 40
    assert result.sensors == 1
    assert result.until == points[-1][0]

    found = await _states_of(hass, "sensor.coolant", now - timedelta(hours=2))
    past = [state for state in found if state.last_updated.timestamp() < now.timestamp() - 60]
    assert len(past) == 40
    assert past[0].state == "86"
    assert past[0].last_updated.timestamp() == pytest.approx(start)
    assert past[-1].state == "156.2"
    assert past[-1].last_updated.timestamp() == pytest.approx(points[-1][0])
    # they read like the sensor's own states
    assert past[0].attributes["unit_of_measurement"] == "°F"
    # and the live state is still the newest
    assert found[-1].state == "176"

    # a second run over the same rows writes nothing
    again = await async_fill_states(
        hass,
        enabled=True,
        approved=(),
        blocked=(),
        series=[StateSeries("sensor.coolant", points, unit_converter("°C", "°F"), 3)],
        gaps=[(start - 5, start + 1000)],
        after_ts=result.until,
    )
    assert again.states_written == 0
    assert len(await _states_of(hass, "sensor.coolant", now - timedelta(hours=2))) == len(found)


async def test_no_state_is_written_on_an_untested_recorder(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """A blocked or unknown database version, or the option off: statistics only."""
    now = dt_util.utcnow()
    hass.states.async_set("sensor.coolant", "176", {"unit_of_measurement": "°F"})
    await async_wait_recording_done(hass)
    start = now.timestamp() - 3600
    series = [StateSeries("sensor.coolant", [(start, 30.0), (start + 60, 40.0)], lambda v: v, 3)]
    gaps = [(start - 5, start + 1000)]
    schema = get_instance(hass).schema_version

    blocked = await async_fill_states(
        hass, enabled=True, approved=(), blocked=(schema,), series=series, gaps=gaps, after_ts=0.0,
    )
    assert blocked.active is False
    assert blocked.states_written == 0
    assert "not one the History fill was tested with" in blocked.note
    assert blocked.until is None

    with patch.object(get_instance(hass), "schema_version", schema + 1):
        unknown = await async_fill_states(
            hass, enabled=True, approved=(), blocked=(), series=series, gaps=gaps, after_ts=0.0,
        )
        assert unknown.active is False
        # the catalog approved it since
        approved = await async_fill_states(
            hass, enabled=True, approved=(schema + 1,), blocked=(), series=series, gaps=gaps,
            after_ts=0.0,
        )
        assert approved.states_written == 2

    off = await async_fill_states(
        hass, enabled=False, approved=(), blocked=(), series=series, gaps=gaps, after_ts=0.0,
    )
    assert off.active is False
    assert off.note == "switched off in the options"

    found = await _states_of(hass, "sensor.coolant", now - timedelta(hours=2))
    assert len(found) == 3  # the two the approved run wrote, the live one


async def test_a_recorder_that_refuses_is_contained(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """A failing write is an error in the result, never an exception."""
    now = dt_util.utcnow()
    hass.states.async_set("sensor.coolant", "176", {"unit_of_measurement": "°F"})
    await async_wait_recording_done(hass)
    start = now.timestamp() - 3600
    with patch(
        "custom_components.wican.history_states.session_scope",
        side_effect=RuntimeError("database is locked"),
    ):
        result = await async_fill_states(
            hass,
            enabled=True,
            approved=(),
            blocked=(),
            series=[StateSeries("sensor.coolant", [(start, 30.0)], lambda v: v, 3)],
            gaps=[(start - 5, start + 1000)],
            after_ts=0.0,
        )
    assert result.states_written == 0
    assert result.until is None  # the next run tries again
    assert any("database is locked" in error for error in result.errors)


async def test_a_drive_shows_in_history_after_the_return(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The whole path: a silence, the pull at the return, states of the away time."""
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    now = dt_util.utcnow().timestamp()
    left, back = now - 3000, now - 20
    # the sensor had a state before the car left
    hass.states.async_set(entity_id, "90", {"unit_of_measurement": "%"})
    await async_wait_recording_done(hass)
    # the push that ended the silence
    entry.runtime_data.coordinator._data_gaps.append((left, back))  # noqa: SLF001
    drive = [
        {"ts": (left - 30) * 1000, "param": "autopid.SOC", "value": 90},  # before the gap
        {"ts": (left + 60) * 1000, "param": "autopid.SOC", "value": 88},
        {"ts": (left + 600) * 1000, "param": "autopid.SOC", "value": 80.12345},
        {"ts": (left + 1200) * 1000, "param": "autopid.SOC", "value": 80.12345},
        {"ts": (left + 2400) * 1000, "param": "autopid.SOC", "value": 61},
        {"ts": (back + 5) * 1000, "param": "autopid.SOC", "value": 60},  # after the gap
    ]
    entry.runtime_data.api.async_get_logger_status = AsyncMock(return_value=None)
    entry.runtime_data.api.async_get_clock_journal = AsyncMock(return_value=[])
    entry.runtime_data.api.async_export_log_rows = _one_page(drive)

    result = await async_sync_history(hass, entry)

    assert result.states["active"] is True
    assert result.states["states_written"] == 3
    assert result.states["rows_in_gaps"] == 4
    found = await _states_of(hass, entity_id, dt_util.utc_from_timestamp(left - 60))
    away = [
        (round(state.last_updated.timestamp() - left), state.state)
        for state in found
        if left < state.last_updated.timestamp() < back
    ]
    assert away == [(60, "88"), (600, "80.123"), (2400, "61")]
    stored = await HistorySync(hass, entry)._async_load()  # noqa: SLF001
    assert stored["states_until"] == pytest.approx(left + 2400)
    assert "gaps" not in stored  # the log was read over it

    # the option off: nothing is written, the gap is dropped all the same
    hass.config_entries.async_update_entry(entry, options={CONF_HISTORY_STATES: False})
    await hass.async_block_till_done()
    entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    entry.runtime_data.api.async_get_logger_status = AsyncMock(return_value=None)
    entry.runtime_data.api.async_get_clock_journal = AsyncMock(return_value=[])
    entry.runtime_data.coordinator._data_gaps.append((back + 100, back + 200))  # noqa: SLF001
    entry.runtime_data.api.async_export_log_rows = _one_page(
        [{"ts": (back + 150) * 1000, "param": "autopid.SOC", "value": 55}],
    )
    off = await async_sync_history(hass, entry)
    assert off.states["active"] is False
    assert off.states["states_written"] == 0


async def test_a_run_over_its_cap_says_so(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The spacing asked for, the one used and the reason are told, and written."""
    now = dt_util.utcnow()
    hass.states.async_set("sensor.coolant", "80", {"unit_of_measurement": "°C"})
    await async_wait_recording_done(hass)
    start = now.timestamp() - 3600
    points = [(start + 10.0 * index, 30.0 + index) for index in range(40)]
    with patch("custom_components.wican.history_states.HISTORY_STATES_MAX_PER_RUN", 20):
        result = await async_fill_states(
            hass,
            enabled=True,
            approved=(),
            blocked=(),
            series=[StateSeries("sensor.coolant", points, lambda value: value, 3)],
            gaps=[(start - 5, start + 1000)],
            after_ts=0.0,
            step=0,
        )
    # 40 changes are over the cap of 20; rows 10 s apart: 16 s still leaves
    # 21 (every second one and the last), 32 s leaves 11
    assert (result.step, result.step_used, result.states_at_step) == (0.0, 32.0, 40)
    assert result.states_written == 11
    assert result.errors == []
    assert "written with a spacing of 32 s (11 points)" in caplog.text
    found = await _states_of(hass, "sensor.coolant", now - timedelta(hours=2))
    past = [state for state in found if state.last_updated.timestamp() < now.timestamp() - 60]
    assert len(past) == 11
    assert past[0].last_updated.timestamp() == pytest.approx(start)
    assert past[-1].last_updated.timestamp() == pytest.approx(points[-1][0])


async def test_the_spacing_of_the_history_points_is_the_entrys_option(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """history_states_step: 10 s unless set, 0 = every change, a number spaces them."""
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    now = dt_util.utcnow().timestamp()
    hass.states.async_set(entity_id, "90", {"unit_of_measurement": "%"})
    await async_wait_recording_done(hass)

    async def away(left: float, step: int | None) -> tuple[dict[str, Any], list[float]]:
        """A silence of 100 s, a value every half second for 30 s of it, read with that option."""
        if step is not None:
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_HISTORY_STATES_STEP: step},
            )
            await hass.async_block_till_done()
        entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
        entry.runtime_data.api.async_get_logger_status = AsyncMock(return_value=None)
        entry.runtime_data.api.async_get_clock_journal = AsyncMock(return_value=[])
        entry.runtime_data.coordinator._data_gaps.append((left, left + 100))  # noqa: SLF001
        entry.runtime_data.api.async_export_log_rows = _one_page(
            [
                {"ts": (left + 10) * 1000 + 500 * index, "param": "autopid.SOC", "value": 80 - index}
                for index in range(60)
            ],
        )
        result = await async_sync_history(hass, entry)
        found = await _states_of(hass, entity_id, dt_util.utc_from_timestamp(left - 60))
        return result.states, [
            round(state.last_updated.timestamp() - left, 1)
            for state in found
            if left < state.last_updated.timestamp() < left + 100
        ]

    # nothing set: a point a second, and the value the drive ended on
    states, at = await away(float(int(now)) - 3000, None)
    assert at == [*[10.0 + second for second in range(30)], 39.5]
    assert (states["step"], states["step_used"], states["states_at_step"]) == (1.0, 1.0, 31)
    # 0: every change the device logged
    states, at = await away(float(int(now)) - 2000, 0)
    assert at == [10.0 + 0.5 * index for index in range(60)]
    assert (states["step"], states["step_used"], states["states_written"]) == (0.0, 0.0, 60)
    # 10: the first, one every 10 s, and the last value
    states, at = await away(float(int(now)) - 1000, 10)
    assert at == [10.0, 20.0, 30.0, 39.5]
    assert (states["step"], states["states_written"]) == (10.0, 4)


# ---------------------------------------------------------------------------
# The gaps
# ---------------------------------------------------------------------------


async def test_a_silence_that_ends_is_handed_to_the_catch_up(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The coordinator notes (last push before, the push that ended it)."""
    freezer.move_to(T0)
    entry = await _entry(hass, mock_config_entry, pids={"SOC": ("%", "%")})
    coordinator = entry.runtime_data.coordinator
    device_id = entry.data.get("device_id")
    push = {"status": {"device_id": device_id}} if device_id else {"status": {}}

    assert coordinator.handle_webhook_data(dict(push)) is False
    assert coordinator.take_data_gaps() == []
    freezer.tick(timedelta(seconds=20))
    assert coordinator.handle_webhook_data(dict(push)) is False
    left = dt_util.utcnow().timestamp()
    freezer.tick(timedelta(seconds=coordinator.staleness_threshold() + 2400))
    assert coordinator.handle_webhook_data(dict(push)) is True
    back = dt_util.utcnow().timestamp()

    assert coordinator.take_data_gaps() == [(left, back)]
    assert coordinator.take_data_gaps() == []  # handed over once
