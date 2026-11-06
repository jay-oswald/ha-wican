"""Unit tests for the SD-card history backfill engine (history.py)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wican.api import LogExportPage, MeatPiApiClient
from custom_components.wican.const import (
    CONF_HISTORY_PREFER_LIVE,
    DOMAIN,
    HISTORY_MAX_FILES_PER_SYNC,
    HISTORY_MAX_PIDS_PER_SYNC,
)
from custom_components.wican.exceptions import MeatPiApiError
from custom_components.wican.history import (
    HistorySync,
    HistorySyncResult,
    aggregate_rows,
    async_remove_history_store,
    async_sync_history,
    cursor_is_plausible,
    followup_delay,
    normalize_row,
    parse_csv,
    parse_jsonl,
)

NOW = time.time()
HOUR = int(NOW // 3600) * 3600
H1 = HOUR - 3 * 3600  # three full hours ago (top of hour)
H2 = HOUR - 2 * 3600


# ---------------------------------------------------------------------------
# Row normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"ts": 100.0, "name": "SOC", "value": 71.5}, (100.0, "SOC", 71.5)),
        ({"timestamp": 100, "param": "RPM", "val": "900"}, (100.0, "RPM", 900.0)),
        ({"time": "100.5", "pid": " SOC ", "value": 1}, (100.5, "SOC", 1.0)),
        ({"epoch": 100, "name": "x", "value": -3}, (100.0, "x", -3.0)),
    ],
)
def test_normalize_row_accepts_aliases(raw: dict, expected: tuple) -> None:
    """Field aliases and string numbers are handled."""
    assert normalize_row(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not a dict",
        ["ts", 1],
        None,
        {},
        {"ts": 100, "name": "SOC"},  # no value
        {"ts": 100, "value": 1},  # no name
        {"name": "SOC", "value": 1},  # no ts
        {"ts": 0, "name": "SOC", "value": 1},  # zero ts
        {"ts": -5, "name": "SOC", "value": 1},
        {"ts": 100, "name": "", "value": 1},
        {"ts": 100, "name": "   ", "value": 1},
        {"ts": 100, "name": "x" * 500, "value": 1},  # absurd name
        {"ts": 100, "name": "SOC", "value": "garbage"},
        {"ts": 100, "name": "SOC", "value": float("nan")},
        {"ts": 100, "name": "SOC", "value": float("inf")},
        {"ts": 100, "name": "SOC", "value": True},  # bool is not a reading
        {"ts": 100, "name": "SOC", "value": {"nested": 1}},
        {"ts": 100, "name": 42, "value": 1},
        {"ts": "soon", "name": "SOC", "value": 1},
    ],
)
def test_normalize_row_rejects_garbage(raw: Any) -> None:
    """Every malformed row shape is rejected, never raises."""
    assert normalize_row(raw) is None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_parse_jsonl_mixed_content() -> None:
    """Valid lines survive; garbage lines are counted, not fatal."""
    text = "\n".join(
        [
            '{"ts": 1, "name": "SOC", "value": 70}',
            "",
            "not json at all",
            '["a", "list"]',
            '{"ts": 2, "name": "SOC", "value": 71}',
            "   ",
        ],
    )
    rows, bad = parse_jsonl(text)
    assert len(rows) == 2
    assert bad == 2


def test_parse_csv_with_aliases_and_extras() -> None:
    """CSV headers may use aliases; unknown columns are ignored."""
    text = "timestamp,param,val,extra\n100,SOC,70,x\n200,RPM,900,y\n"
    rows, bad = parse_csv(text)
    assert bad == 0
    assert normalize_row(rows[0]) == (100.0, "SOC", 70.0)
    assert normalize_row(rows[1]) == (200.0, "RPM", 900.0)


def test_parse_csv_garbage() -> None:
    """Unparseable CSV never raises."""
    rows, _bad = parse_csv('a,b\x00,c\n"unclosed')
    assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregate_rows_buckets_by_hour() -> None:
    """Rows land in per-PID hourly buckets with correct mean/min/max."""
    result = HistorySyncResult()
    rows = [
        {"ts": H1 + 60, "name": "SOC", "value": 70},
        {"ts": H1 + 120, "name": "SOC", "value": 74},
        {"ts": H1 + 180, "name": "SOC", "value": 72},
        {"ts": H2 + 60, "name": "SOC", "value": 60},
        {"ts": H1 + 60, "name": "RPM", "value": 900},
    ]
    buckets = aggregate_rows(rows, since=0, now_ts=NOW, result=result)

    soc_h1 = buckets["SOC"][H1]
    assert soc_h1.mean == pytest.approx(72.0)
    assert soc_h1.minimum == 70
    assert soc_h1.maximum == 74
    assert buckets["SOC"][H2].mean == 60
    assert buckets["RPM"][H1].count == 1
    assert result.rows_used == 5
    assert result.rows_rejected == 0


def test_aggregate_rows_window_filters() -> None:
    """Rows outside the time window are rejected: old, future, current hour."""
    result = HistorySyncResult()
    rows = [
        {"ts": H1 - 40 * 86400, "name": "SOC", "value": 1},  # too old
        {"ts": NOW + 4000, "name": "SOC", "value": 2},  # future
        {"ts": HOUR + 60, "name": "SOC", "value": 3},  # current hour (live)
        {"ts": H1 + 60, "name": "SOC", "value": 4},  # good
        {"ts": H1 + 30, "name": "SOC", "value": 5},  # before since
    ]
    buckets = aggregate_rows(rows, since=H1 + 45, now_ts=NOW, result=result)

    assert buckets["SOC"][H1].count == 1
    assert buckets["SOC"][H1].mean == 4
    assert result.rows_rejected == 3
    assert result.rows_deferred == 1  # the current hour waits for its end


def test_aggregate_rows_pid_cap() -> None:
    """A device inventing thousands of PID names cannot balloon memory."""
    result = HistorySyncResult()
    rows = [
        {"ts": H1 + i % 300, "name": f"PID_{i}", "value": i}
        for i in range(HISTORY_MAX_PIDS_PER_SYNC * 2)
    ]
    buckets = aggregate_rows(rows, since=0, now_ts=NOW, result=result)
    assert len(buckets) == HISTORY_MAX_PIDS_PER_SYNC
    assert result.rows_rejected == HISTORY_MAX_PIDS_PER_SYNC


def test_aggregate_row_cap_discards_partial_trailing_hour() -> None:
    """Hitting the row cap drops hours the leftover rows could touch.

    Complete hours before the cut are kept; the hour cut short mid-way
    is deferred whole, so it is never imported from partial data.
    """
    result = HistorySyncResult()
    rows = [{"ts": H1 + i, "name": "SOC", "value": 1} for i in range(3)]
    rows += [{"ts": H2 + i, "name": "SOC", "value": 1} for i in range(3)]
    with patch("custom_components.wican.history.HISTORY_MAX_ROWS_PER_SYNC", 4):
        buckets = aggregate_rows(rows, since=0, now_ts=NOW, result=result)
    assert buckets["SOC"][H1].count == 3
    assert H2 not in buckets["SOC"]
    assert any("cut short" in e for e in result.errors)


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------


def test_select_log_files_filters_and_caps() -> None:
    """Only rotated dl_* log files below the size cap are selected."""
    result = HistorySyncResult()
    entries = [
        {"name": "dl_100.jsonl", "dir": False, "size": 10},
        {"name": "dl_200.csv", "dir": False, "size": 10},
        {"name": "dl_300.jsonl", "dir": False, "size": 10},  # active
        {"name": "dl_400.jsonl", "dir": False, "size": 10**9},  # oversized
        {"name": "can_100.wdl", "dir": False, "size": 10},  # other stream
        {"name": "notes.txt", "dir": False, "size": 10},
        {"name": "dl_dir.jsonl", "dir": True, "size": 0},
        {"nonsense": True},
        {"name": 42, "dir": False},
    ]
    selected = HistorySync._select_log_files(entries, "dl_300.jsonl", result)
    assert selected == ["dl_100.jsonl", "dl_200.csv"]
    assert any("too large" in e for e in result.errors)


def test_select_log_files_defers_beyond_cap() -> None:
    """More files than the per-sync cap keeps the OLDEST, defers the newest.

    Deferring the newest is self-healing: their rows stay ahead of the
    watermark and the next sync picks them up. Keeping the newest instead
    would advance the watermark past the dropped oldest files, losing
    their rows forever.
    """
    result = HistorySyncResult()
    entries = [
        {"name": f"dl_{1000 + i}.jsonl", "dir": False, "size": 5}
        for i in range(HISTORY_MAX_FILES_PER_SYNC + 10)
    ]
    selected = HistorySync._select_log_files(entries, None, result)
    assert len(selected) == HISTORY_MAX_FILES_PER_SYNC
    assert selected[0] == "dl_1000.jsonl"
    assert selected[-1] == f"dl_{1000 + HISTORY_MAX_FILES_PER_SYNC - 1}.jsonl"
    assert any("deferred" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Full sync with a mocked device API + mocked recorder
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_recorder_calls():
    """Mock the recorder touchpoints used by the sync."""
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
    ):

        async def _run_job(func: Any, *args: Any) -> Any:
            return func(*args)

        instance.return_value.async_add_executor_job = AsyncMock(
            side_effect=_run_job,
        )
        yield {
            "import": import_stats,
            "during": during,
            "metadata": metadata,
        }


async def _entry_with_api(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    *,
    pids: tuple[str, ...] = ("SOC",),
) -> MockConfigEntry:
    """Set up the integration, register PID entities, attach a mock API."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.wican._async_register_webhook_on_device",
        return_value=True,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    for pid in pids:
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_pid_{pid}",
        )
    mock_config_entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    return mock_config_entry


def _page(rows: list[Any], cursor: str | None = None, more: bool = False) -> LogExportPage:
    """One page in the firmware's shape (the client already parsed it)."""
    return LogExportPage(
        rows=list(rows),
        cursor=cursor if cursor is not None else f"1793900000:{len(rows)}",
        more=more,
    )


def _export_rows(rows: list[dict[str, Any]]) -> AsyncMock:
    """Return an export mock serving all rows in one page, then empty."""
    return AsyncMock(side_effect=[_page(rows)])


async def test_sync_imports_hourly_statistics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """The happy path: rows become hourly statistics on the PID entity."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H1 + 120, "name": "SOC", "value": 74},
            {"ts": H2 + 60, "name": "SOC", "value": 65},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.ran
    assert result.source == "export"
    assert result.pids_imported == 1
    assert result.hours_imported == 2
    imported = mock_recorder_calls["import"].call_args
    metadata, statistics = imported.args[1], imported.args[2]
    assert metadata["statistic_id"].startswith("sensor.")
    assert metadata["source"] == "recorder"
    assert statistics[0]["start"] == dt_util.utc_from_timestamp(H1)
    assert statistics[0]["mean"] == pytest.approx(72.0)
    assert statistics[0]["min"] == 70
    assert statistics[0]["max"] == 74
    assert statistics[1]["mean"] == 65
    # Watermark = end of the newest imported hour.
    assert result.watermark == H2 + 3600
    sync = HistorySync(hass, entry)
    assert await sync.async_get_watermark() == H2 + 3600


async def test_sync_falls_back_to_files(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Firmware without the export route is served from rotated files."""
    entry = await _entry_with_api(hass, mock_config_entry)
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(return_value=None)  # 404 route
    api.async_get_logger_status = AsyncMock(
        return_value={"dir": "/sd/logs", "file": "dl_900.jsonl"},
    )
    api.async_fs_list = AsyncMock(
        return_value=[
            {"name": "dl_800.jsonl", "dir": False, "size": 100},
            {"name": "dl_900.jsonl", "dir": False, "size": 100},  # active
        ],
    )
    api.async_fs_download = AsyncMock(
        return_value=f'{{"ts": {H1 + 60}, "name": "SOC", "value": 50}}',
    )

    result = await async_sync_history(hass, entry)

    assert result.source == "files"
    assert result.hours_imported == 1
    api.async_fs_download.assert_awaited_once()  # active file never fetched
    assert "dl_800.jsonl" in api.async_fs_download.call_args.args[0]


async def test_sync_fetch_failure_is_contained(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A device that dies mid-fetch aborts cleanly; watermark untouched."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=MeatPiApiError("boom"),
    )

    result = await async_sync_history(hass, entry)

    assert not result.ran
    assert result.errors
    mock_recorder_calls["import"].assert_not_called()
    assert await HistorySync(hass, entry).async_get_watermark() == 0.0


async def test_sync_skips_unknown_pids(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Rows for PIDs that back no entity are counted, not imported."""
    entry = await _entry_with_api(hass, mock_config_entry, pids=("SOC",))
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H1 + 60, "name": "NEVER_SEEN", "value": 1},
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    assert result.pids_skipped == 1


async def test_sync_live_hours_win(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """With history_prefer_live, hours carrying live statistics are left alone."""
    entry = await _entry_with_api(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_HISTORY_PREFER_LIVE: True},
    )
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H2 + 60, "name": "SOC", "value": 65},
        ],
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    mock_recorder_calls["during"].return_value = {
        entity_id: [{"start": float(H1)}],
    }

    result = await async_sync_history(hass, entry)

    assert result.hours_skipped_existing == 1
    assert result.hours_imported == 1
    statistics = mock_recorder_calls["import"].call_args.args[2]
    assert statistics[0]["start"] == dt_util.utc_from_timestamp(H2)


async def test_sync_incompatible_metadata_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Sum-type existing statistics are never polluted with mean rows."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [{"ts": H1 + 60, "name": "SOC", "value": 70}],
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    mock_recorder_calls["metadata"].return_value = {
        entity_id: (1, {"has_sum": True, "unit_of_measurement": None}),
    }

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 0
    assert any("incompatible" in e for e in result.errors)
    mock_recorder_calls["import"].assert_not_called()


async def test_sync_per_pid_isolation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """One PID failing to import never blocks the others."""
    entry = await _entry_with_api(hass, mock_config_entry, pids=("SOC", "RPM"))
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H1 + 60, "name": "RPM", "value": 900},
        ],
    )
    calls: list[str] = []

    def _import(hass_arg: Any, metadata: dict, statistics: list) -> None:
        calls.append(metadata["statistic_id"])
        if len(calls) == 1:
            raise ValueError("simulated import failure")

    mock_recorder_calls["import"].side_effect = _import

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    assert len(calls) == 2
    assert result.errors


async def test_sync_watermark_held_at_failed_pid_hours(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A failed PID's hours stay ahead of the watermark and recover.

    Regression: the watermark used to advance to the newest hour any PID
    imported, stranding a failed PID's rows behind it — silent permanent
    loss of that PID's offline history.
    """
    entry = await _entry_with_api(hass, mock_config_entry, pids=("SOC", "RPM"))
    rows = [
        {"ts": H1 + 60, "name": "SOC", "value": 70},
        {"ts": H2 + 60, "name": "SOC", "value": 72},
        {"ts": H1 + 90, "name": "RPM", "value": 900},
    ]
    entry.runtime_data.api.async_export_log_rows = _export_rows(rows)

    def _fail_rpm(hass_arg: Any, metadata: dict, statistics: list) -> None:
        if metadata["statistic_id"].endswith("rpm"):
            raise ValueError("simulated import failure")

    mock_recorder_calls["import"].side_effect = _fail_rpm

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    # SOC imported through H2, but the watermark holds at H1 where RPM
    # failed — not at H2 + 3600.
    assert result.watermark == H1

    # The next sync re-fetches from the failed hour and recovers RPM.
    mock_recorder_calls["import"].side_effect = None
    mock_recorder_calls["import"].reset_mock()
    entry.runtime_data.api.async_export_log_rows = _export_rows(rows)

    result2 = await async_sync_history(hass, entry)

    imported_ids = [
        call.args[1]["statistic_id"]
        for call in mock_recorder_calls["import"].call_args_list
    ]
    assert any(sid.endswith("rpm") for sid in imported_ids)
    assert result2.watermark == H2 + 3600


async def test_sync_row_cap_partial_hour_recovers_next_run(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """An hour cut short by the row cap is deferred and imported next run.

    Regression: the partial hour used to be imported and frozen behind
    the watermark with a wrong mean/min/max forever.
    """
    entry = await _entry_with_api(hass, mock_config_entry)
    rows = [{"ts": H1 + 10 * i, "name": "SOC", "value": i} for i in range(3)]
    rows += [{"ts": H2 + 10 * i, "name": "SOC", "value": i} for i in range(3)]
    entry.runtime_data.api.async_export_log_rows = _export_rows(rows)

    with patch("custom_components.wican.history.HISTORY_MAX_ROWS_PER_SYNC", 4):
        result = await async_sync_history(hass, entry)

    # Only the complete hour H1 imported; watermark parked at H2.
    assert result.hours_imported == 1
    assert result.watermark == H2

    entry.runtime_data.api.async_export_log_rows = _export_rows(rows)
    mock_recorder_calls["import"].reset_mock()

    result2 = await async_sync_history(hass, entry)

    assert result2.hours_imported == 1
    assert result2.watermark == H2 + 3600


async def test_sync_export_cursor_stall_stops(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A device that never advances the export cursor cannot loop forever."""
    entry = await _entry_with_api(hass, mock_config_entry)
    full_page = [
        {"ts": H1 + i, "name": "SOC", "value": i} for i in range(2000)
    ]
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        return_value=LogExportPage(rows=full_page, cursor=None, more=True),
    )

    result = await async_sync_history(hass, entry)

    assert any("cursor" in e for e in result.errors)
    assert entry.runtime_data.api.async_export_log_rows.await_count == 1


async def test_sync_without_api_is_noop(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """No API client (legacy/unprobed device) → nothing happens."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api = None

    result = await async_sync_history(hass, entry)

    assert not result.ran
    mock_recorder_calls["import"].assert_not_called()


async def test_sync_recorder_failure_contained(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Recorder being unavailable degrades to a logged error."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [{"ts": H1 + 60, "name": "SOC", "value": 70}],
    )
    mock_recorder_calls["during"].side_effect = KeyError("recorder")

    result = await async_sync_history(hass, entry)

    assert any("recorder" in e for e in result.errors)
    mock_recorder_calls["import"].assert_not_called()


async def test_watermark_store_roundtrip_and_removal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """The watermark persists and is deleted with the entry."""
    entry = await _entry_with_api(hass, mock_config_entry)
    sync = HistorySync(hass, entry)
    assert await sync.async_get_watermark() == 0.0

    await sync._async_save(12345.0, "1793900000:77")
    assert await HistorySync(hass, entry).async_get_watermark() == 12345.0
    assert await HistorySync(hass, entry).async_get_cursor() == "1793900000:77"

    await async_remove_history_store(hass, entry)
    assert await HistorySync(hass, entry).async_get_watermark() == 0.0
    assert await HistorySync(hass, entry).async_get_cursor() is None


async def test_unexpected_error_never_escapes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """async_sync_history never raises, whatever breaks inside."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=RuntimeError("totally unexpected"),
    )

    result = await async_sync_history(hass, entry)

    assert result == HistorySyncResult()



# ---------------------------------------------------------------------------
# The firmware's shape and the device cursor (store and forward, 2026-11-06)
# ---------------------------------------------------------------------------


def test_normalize_row_firmware_shape() -> None:
    """Epoch milliseconds and the source prefix read as seconds and the key."""
    assert normalize_row(
        {"ts": 1793900000123, "param": "autopid.SOC", "value": 71.5},
    ) == (1793900000.123, "SOC", 71.5)
    # csv rows arrive as strings
    assert normalize_row(
        {"ts": "1793900000500", "param": "autopid.EngineRPM", "value": "812"},
    ) == (1793900000.5, "EngineRPM", 812.0)
    # the proposal shape still reads
    assert normalize_row({"ts": 1793900000, "name": "SOC", "value": 70}) == (
        1793900000.0, "SOC", 70.0,
    )
    # a trailing dot leaves no name
    assert normalize_row({"ts": 1793900000, "param": "autopid.", "value": 1}) is None


def test_parse_jsonl_skips_the_firmware_meta_lines() -> None:
    """_file, _cursor and clock-step marker lines are not rows."""
    text = "\n".join(
        [
            '{"_file":"dl_1793900000.jsonl"}',
            '{"ts":1793900000000,"param":"autopid.SOC","value":70}',
            '{"ts":1793900001000,"event":"clock_step","delta_ms":120000,"source":"sntp"}',
            '{"_cursor":"1793900000:120","more":false}',
        ],
    )
    rows, bad = parse_jsonl(text)
    assert len(rows) == 1 and bad == 0
    assert normalize_row(rows[0]) == (1793900000.0, "SOC", 70.0)


def test_cursor_plausibility() -> None:
    """A cursor past the file being written is dropped; others are kept."""
    status = {"file": "dl_1793900000.jsonl"}
    assert cursor_is_plausible("1793899000:10", status)
    assert cursor_is_plausible("1793900000:10", status)
    assert not cursor_is_plausible("1793900001:0", status)
    assert cursor_is_plausible("1793900001:0", {"file": ""})  # nothing open: trust it
    assert cursor_is_plausible("1793900001:0", None)
    assert cursor_is_plausible("1793999999", status)  # a plain epoch: always
    assert not cursor_is_plausible("garbage", status)
    assert not cursor_is_plausible(None, status)


def test_followup_delay_rules() -> None:
    """Deferred rows wait for the top of the hour; more rows come back soon."""
    now = float(HOUR + 1800)
    result = HistorySyncResult()
    assert followup_delay(result, now) is None
    result.rows_deferred = 3
    assert followup_delay(result, now) == pytest.approx(1800 + 90)
    result.more_pending = True
    assert followup_delay(result, now) == 60


async def test_sync_recorded_hours_replace_live_by_default(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """The log is the complete record: its hour replaces the live one."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [
            {"ts": H1 + 60, "name": "SOC", "value": 70},
            {"ts": H2 + 60, "name": "SOC", "value": 65},
        ],
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    mock_recorder_calls["during"].return_value = {
        entity_id: [{"start": float(H1)}],
    }

    result = await async_sync_history(hass, entry)

    assert result.hours_skipped_existing == 0
    assert result.hours_overwritten_live == 1
    assert result.hours_imported == 2


async def test_sync_deferred_rows_hold_the_cursor_and_book_a_followup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Rows of the hour still running keep the cursor at their page.

    The next run must read them again once the hour has ended, so the
    saved cursor is the START of the first page that carried one, and
    the run asks for a follow-up past the top of the hour.
    """
    entry = await _entry_with_api(hass, mock_config_entry)
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(
        side_effect=[
            _page([{"ts": (H1 + 60) * 1000, "param": "autopid.SOC", "value": 70}], cursor="1793900000:3", more=True),
            _page([{"ts": (HOUR + 60) * 1000, "param": "autopid.SOC", "value": 65}], cursor="1793900000:5", more=False),
        ],
    )

    result = await async_sync_history(hass, entry)

    assert result.hours_imported == 1
    assert result.rows_deferred == 1
    assert result.cursor == "1793900000:3"
    assert result.followup_in is not None and result.followup_in > 0
    assert await HistorySync(hass, entry).async_get_cursor() == "1793900000:3"
    assert result.watermark == H1 + 3600
    # the first call started at the age limit as a plain epoch (no cursor
    # yet), the second at the page cursor; a later run starts at the saved
    # cursor
    first_since = api.async_export_log_rows.call_args_list[0].args[0]
    assert first_since.isdigit()
    assert abs(int(first_since) - (time.time() - 30 * 86400)) < 3600
    assert api.async_export_log_rows.call_args_list[1].args[0] == "1793900000:3"
    api.async_export_log_rows = AsyncMock(side_effect=[_page([], cursor="1793900000:3")])
    await async_sync_history(hass, entry)
    assert api.async_export_log_rows.call_args_list[0].args[0] == "1793900000:3"


async def test_sync_consumed_rows_advance_the_cursor_without_an_import(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """Rows for PIDs without an entity are consumed: the cursor moves on."""
    entry = await _entry_with_api(hass, mock_config_entry, pids=("SOC",))
    entry.runtime_data.api.async_export_log_rows = _export_rows(
        [{"ts": H1 + 60, "name": "NEVER_SEEN", "value": 1}],
    )

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 0
    assert result.cursor == "1793900000:1"
    assert await HistorySync(hass, entry).async_get_cursor() == "1793900000:1"
    assert await HistorySync(hass, entry).async_get_watermark() == 0.0


async def test_sync_implausible_cursor_restarts_from_the_watermark(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A cursor past the file being written (card wiped) is dropped."""
    entry = await _entry_with_api(hass, mock_config_entry)
    await HistorySync(hass, entry)._async_save(float(H1), "1793999999:40")
    api = entry.runtime_data.api
    api.async_get_logger_status = AsyncMock(
        return_value={"enabled": True, "format": "jsonl", "file": "dl_1793900000.jsonl"},
    )
    api.async_export_log_rows = _export_rows(
        [{"ts": H2 + 60, "name": "SOC", "value": 65}],
    )

    result = await async_sync_history(hass, entry)

    assert any("past the file being written" in e for e in result.errors)
    assert api.async_export_log_rows.call_args_list[0].args[0] == str(H1)
    assert result.hours_imported == 1
    assert result.device["format"] == "jsonl"


async def test_sync_unsupported_format_imports_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],
) -> None:
    """A params stream in sqlite or binary is reported, never fetched as files."""
    entry = await _entry_with_api(hass, mock_config_entry)
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(
        return_value=LogExportPage(unsupported_format=True),
    )

    result = await async_sync_history(hass, entry)

    assert result.ran
    assert result.source == "export"
    assert any("not JSON lines or CSV" in e for e in result.errors)
    api.async_fs_list.assert_not_called()
    mock_recorder_calls["import"].assert_not_called()
