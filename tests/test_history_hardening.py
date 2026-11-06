"""Edge-branch tests for the history backfill: caps, pagination, failures."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.wican.api import LogExportPage, MeatPiApiClient
from custom_components.wican.const import DOMAIN
from custom_components.wican.diagnostics import async_get_config_entry_diagnostics
from custom_components.wican.exceptions import MeatPiApiError
from custom_components.wican.history import (
    HistorySyncResult,
    aggregate_rows,
    async_sync_history,
)
from tests.test_history import _entry_with_api, mock_recorder_calls  # noqa: F401

NOW = time.time()
HOUR = int(NOW // 3600) * 3600
H1 = HOUR - 3 * 3600
BASE_URL = "http://wican_test.local:80"


def test_aggregate_row_cap_defers_remainder() -> None:
    """The per-sync row cap stops processing and defers the partial hour.

    The hour the cap lands in has only some of its rows aggregated;
    importing it would freeze a wrong mean/min/max behind the watermark.
    The whole hour is dropped so the next sync re-imports it complete.
    """
    result = HistorySyncResult()
    rows = [{"ts": H1 + i, "name": "SOC", "value": i} for i in range(10)]
    with patch("custom_components.wican.history.HISTORY_MAX_ROWS_PER_SYNC", 3):
        buckets = aggregate_rows(rows, since=0, now_ts=NOW, result=result)
    assert buckets == {}
    assert result.rows_used == 3
    assert any("row cap" in e for e in result.errors)
    assert any("cut short" in e for e in result.errors)


async def test_export_pagination_follows_cursor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """Full pages advance the cursor; a short page ends the fetch."""
    entry = await _entry_with_api(hass, mock_config_entry)
    page_rows = 3
    first = [{"ts": H1 + i, "name": "SOC", "value": i} for i in range(page_rows)]
    second = [{"ts": H1 + 100, "name": "SOC", "value": 9}]
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(
        side_effect=[
            LogExportPage(rows=first, cursor="1793900000:3", more=True),
            LogExportPage(rows=second, cursor="1793900000:4", more=False),
        ],
    )

    with patch("custom_components.wican.history.HISTORY_EXPORT_PAGE_ROWS", page_rows):
        result = await async_sync_history(hass, entry)

    assert api.async_export_log_rows.await_count == 2
    # Second call resumed from the advanced cursor.
    assert api.async_export_log_rows.call_args_list[1].args[0] == "1793900000:3"
    assert result.rows_fetched == 4


async def test_export_cut_page_voids_the_run(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """A page without a cursor line (the device cut its answer: firmware
    before 6.0.0-beta.8, about one page in sixteen) imports nothing, keeps
    the stored cursor and books a run a minute later."""
    entry = await _entry_with_api(hass, mock_config_entry)
    first = [{"ts": H1 + i, "name": "SOC", "value": i} for i in range(3)]
    cut = [{"ts": H1 + 3600 + i, "name": "SOC", "value": i} for i in range(2)]
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(
        side_effect=[
            # run 1: a good page, the cursor lands
            LogExportPage(rows=first, cursor="1793900000:3", more=False),
            # run 2: a good page, then a cut one
            LogExportPage(rows=first, cursor="1793900000:6", more=True),
            LogExportPage(rows=cut, cursor=None, more=False),
            # run 3 starts where run 1 ended
            LogExportPage(rows=[], cursor="1793900000:3", more=False),
        ],
    )

    first_run = await async_sync_history(hass, entry)
    assert first_run.hours_imported == 1
    assert first_run.cursor == "1793900000:3"
    mock_recorder_calls["import"].reset_mock()

    result = await async_sync_history(hass, entry)

    assert api.async_export_log_rows.await_count == 3
    assert result.pages == 2
    assert result.rows_fetched == 0
    assert result.hours_imported == 0
    assert mock_recorder_calls["import"].call_count == 0
    assert any("without a cursor line" in e for e in result.errors)
    assert result.more_pending is True
    assert result.followup_in == 60
    assert result.cursor == "1793900000:3"

    await async_sync_history(hass, entry)
    assert api.async_export_log_rows.call_args_list[3].args[0] == "1793900000:3"


async def test_export_row_cap_stops_pagination(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """Reaching the row budget stops fetching more pages."""
    entry = await _entry_with_api(hass, mock_config_entry)
    page = [{"ts": H1 + i, "name": "SOC", "value": i} for i in range(3)]
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(
        side_effect=[
            LogExportPage(rows=page, cursor="1793900000:3", more=True),
            LogExportPage(rows=page, cursor="1793900000:6", more=True),
            LogExportPage(rows=page, cursor="1793900000:9", more=True),
        ],
    )

    with (
        patch("custom_components.wican.history.HISTORY_EXPORT_PAGE_ROWS", 3),
        patch("custom_components.wican.history.HISTORY_MAX_ROWS_PER_SYNC", 5),
    ):
        await async_sync_history(hass, entry)

    assert api.async_export_log_rows.await_count == 2  # 3 + 3 >= 5 → stop


async def test_file_download_failure_skips_that_file(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """One unreadable file is noted; the others still import."""
    entry = await _entry_with_api(hass, mock_config_entry)
    api = entry.runtime_data.api
    api.async_export_log_rows = AsyncMock(return_value=None)
    api.async_get_logger_status = AsyncMock(return_value=None)  # default dir
    api.async_fs_list = AsyncMock(
        return_value=[
            {"name": "dl_100.jsonl", "dir": False, "size": 10},
            {"name": "dl_200.jsonl", "dir": False, "size": 10},
        ],
    )
    api.async_fs_download = AsyncMock(
        side_effect=[
            MeatPiApiError("locked", status=423),
            f'{{"ts": {H1 + 60}, "name": "SOC", "value": 50}}',
        ],
    )

    result = await async_sync_history(hass, entry)

    assert any("download failed" in e for e in result.errors)
    assert result.hours_imported == 1


async def test_all_rows_rejected_imports_nothing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """Rows that all fail validation lead to a clean no-op."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=[LogExportPage(rows=[{"garbage": True}, "not a row"], cursor="1793900000:2")],
    )

    result = await async_sync_history(hass, entry)

    assert result.ran
    assert result.rows_rejected == 2
    mock_recorder_calls["import"].assert_not_called()


async def test_existing_compatible_metadata_is_reused(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """A PID with live statistics reuses its unit/unit_class metadata."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=[LogExportPage(rows=[{"ts": H1 + 60, "name": "SOC", "value": 70}], cursor="1793900000:9")],
    )
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_SOC",
    )
    mock_recorder_calls["metadata"].return_value = {
        entity_id: (
            1,
            {
                "has_sum": False,
                "mean_type": StatisticMeanType.ARITHMETIC,
                "unit_of_measurement": "%",
                "unit_class": None,
            },
        ),
    }

    result = await async_sync_history(hass, entry)

    assert result.pids_imported == 1
    metadata = mock_recorder_calls["import"].call_args.args[1]
    assert metadata["unit_of_measurement"] == "%"


async def test_watermark_save_failure_is_contained(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """A storage failure only means the sync repeats later."""
    entry = await _entry_with_api(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=[LogExportPage(rows=[{"ts": H1 + 60, "name": "SOC", "value": 70}], cursor="1793900000:9")],
    )

    with patch(
        "homeassistant.helpers.storage.Store.async_save",
        side_effect=OSError("disk full"),
    ):
        result = await async_sync_history(hass, entry)

    assert result.hours_imported == 1
    assert any("watermark save failed" in e for e in result.errors)


async def test_diagnostics_expose_last_sync(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder_calls: dict[str, MagicMock],  # noqa: F811
) -> None:
    """Diagnostics carry the last sync's counters (and None before any)."""
    entry = await _entry_with_api(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["history_sync"] is None

    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=[LogExportPage(rows=[{"ts": H1 + 60, "name": "SOC", "value": 70}], cursor="1793900000:9")],
    )
    result = await async_sync_history(hass, entry)
    entry.runtime_data.last_history_result = result

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["history_sync"]["hours_imported"] == 1
    assert diagnostics["history_sync"]["source"] == "export"


# ---------------------------------------------------------------------------
# API-client edge branches (real client over the aiohttp mocker)
# ---------------------------------------------------------------------------


async def test_export_invalid_cursor_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """The proposal shape with a garbage X-Next-Since header yields no cursor."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(
        f"{BASE_URL}/api/logger/export",
        text='{"ts": 1, "name": "SOC", "value": 1}\nnot json',
        headers={"X-Next-Since": "not-a-number"},
    )
    client = MeatPiApiClient(async_get_clientsession(hass), BASE_URL)

    page = await client.async_export_log_rows(None, 100)

    assert page is not None
    assert len(page.rows) == 1
    assert page.cursor is None and not page.more


async def test_export_parses_the_firmware_shape(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """_file lines, ms rows, csv rows and the final _cursor line; 400 = unsupported."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.get(
        f"{BASE_URL}/api/logger/export",
        text="\n".join(
            [
                '{"_file":"dl_1793900000.jsonl"}',
                '{"ts":1793900000123,"param":"autopid.SOC","value":71.5}',
                "not json",
                '{"_file":"dl_1793903600.jsonl"}',
                '{"ts":1793903600000,"param":"autopid.SOC","value":70}',
                '{"_cursor":"1793903600:58","more":true}',
            ],
        ) + "\n",
    )
    client = MeatPiApiClient(async_get_clientsession(hass), BASE_URL)

    page = await client.async_export_log_rows("1793900000:0", 100)

    assert page is not None
    assert [row["value"] for row in page.rows] == [71.5, 70]
    assert page.cursor == "1793903600:58" and page.more
    assert page.files == ["dl_1793900000.jsonl", "dl_1793903600.jsonl"]
    assert aioclient_mock.mock_calls[-1][1].query["since"] == "1793900000:0"

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{BASE_URL}/api/logger/export",
        text="\n".join(
            [
                '{"_file":"dl_1793900000.csv"}',
                "ts_ms,param,value",
                "1793900000123,autopid.SOC,71.5",
                '{"_cursor":"1793900000:40","more":false}',
            ],
        ),
    )
    page = await client.async_export_log_rows(None, 100)
    assert page is not None
    assert page.rows == [{"ts": "1793900000123", "param": "autopid.SOC", "value": "71.5", "_file": "dl_1793900000.csv"}]
    assert page.cursor == "1793900000:40" and not page.more

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{BASE_URL}/api/logger/export",
        text="params stream is not jsonl or csv",
        status=400,
    )
    page = await client.async_export_log_rows(None, 100)
    assert page is not None and page.unsupported_format

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}/api/logger/export", text="nope", status=404)
    assert await client.async_export_log_rows(None, 100) is None


async def test_fs_list_garbage_bodies(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    """Non-JSON, non-dict, and non-list /api/fs/list bodies yield []."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = MeatPiApiClient(async_get_clientsession(hass), BASE_URL)
    for body in ("not json", '["a"]', '{"entries": "nope"}'):
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}/api/fs/list", text=body)
        assert await client.async_fs_list("/sd/logs") == []

    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{BASE_URL}/api/fs/list",
        json={"entries": [{"name": "dl_1.jsonl"}, "junk", 42]},
    )
    entries = await client.async_fs_list("/sd/logs")
    assert entries == [{"name": "dl_1.jsonl"}]
