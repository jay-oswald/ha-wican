"""End-to-end tests for the SD-card history backfill.

Drives the full pipeline with the device simulator: real webhook pushes,
real capability probe, real API client fetching from the simulated device's
data-logger routes — through to the recorder statistics import (mocked for
flow tests, real in-memory recorder for the round-trip tests).
"""

from __future__ import annotations

import json

from functools import partial
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.wican.api import LEGACY_CAPABILITIES, LogExportPage, MeatPiApiClient
from custom_components.wican.const import CONF_HISTORY_SYNC, DOMAIN
from custom_components.wican.history import HistorySync, async_sync_history
from tests.device_sim import MeatPiDeviceSimulator

NOW = time.time()
HOUR = int(NOW // 3600) * 3600
H1 = HOUR - 3 * 3600
H2 = HOUR - 2 * 3600

OFFLINE_DRIVE_ROWS = [
    {"ts": H1 + 60, "name": "SOC", "value": 70},
    {"ts": H1 + 600, "name": "SOC", "value": 74},
    {"ts": H2 + 60, "name": "SOC", "value": 65},
    {"ts": H2 + 60, "name": "RPM", "value": 1500},
]


@pytest.fixture
def mock_capability_probe() -> None:
    """Override conftest's autouse probe stub: run the REAL API client."""
    return


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_db_url: str,
    enable_custom_integrations: None,
) -> None:
    """Override conftest's autouse fixture with recorder-safe ordering.

    The recorder DB fixture must initialize before the first ``hass``
    instance; requesting it first here makes the in-memory recorder tests
    in this module possible.
    """
    return


@pytest.fixture
def mock_recorder_calls():
    """Mock the recorder touchpoints used by the sync."""
    with (
        patch("custom_components.wican.history.get_instance") as instance,
        patch(
            "custom_components.wican.history.statistics_during_period",
            return_value={},
        ),
        patch(
            "custom_components.wican.history.get_metadata", return_value={},
        ),
        patch(
            "custom_components.wican.history.async_import_statistics",
        ) as import_stats,
    ):

        async def _run_job(func: Any, *args: Any) -> Any:
            return func(*args)

        instance.return_value.async_add_executor_job = AsyncMock(
            side_effect=_run_job,
        )
        yield import_stats


async def _online_device(
    hass: HomeAssistant, hass_client: Any, aioclient_mock: Any,
) -> MeatPiDeviceSimulator:
    """A V6 WiCAN Pro that has pushed once (PID entities exist)."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup()
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 71, "RPM": 900})},
    )
    return sim


async def _reconnect_after_gap(sim: MeatPiDeviceSimulator) -> None:
    """Simulate the device coming home after an offline drive."""
    sim.go_stale()
    sim.entry.runtime_data.last_history_sync = 0.0  # bypass rate limit
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 65, "RPM": 800})},
    )


async def test_offline_drive_backfilled_via_export_route(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """Reconnecting after a gap imports the SD-card rows (export route)."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)

    assert sim.api.count("GET", "/api/logger/export") >= 1
    imported = {
        call.args[1]["statistic_id"]: call.args[2]
        for call in mock_recorder_calls.call_args_list
    }
    soc_stats = imported["sensor.wican_pro_sim_soc"]
    assert soc_stats[0]["start"] == dt_util.utc_from_timestamp(H1)
    assert soc_stats[0]["mean"] == pytest.approx(72.0)
    assert soc_stats[1]["mean"] == 65
    rpm_stats = imported["sensor.wican_pro_sim_rpm"]
    assert rpm_stats[0]["mean"] == 1500
    # Diagnostics surface the run.
    result = sim.entry.runtime_data.last_history_result
    assert result is not None and result.hours_imported == 3


@pytest.mark.parametrize("file_format", ["jsonl", "csv"])
async def test_offline_drive_backfilled_via_files(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
    file_format: str,
) -> None:
    """Current firmware (no export route) is served from rotated files."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="file", file_format=file_format)
    # An active (write-locked) file must be skipped without failing the sync.
    sim.seed_log_rows(
        [{"ts": H2 + 90, "name": "SOC", "value": 1}],
        via="file",
        filename="dl_active.jsonl",
        active=True,
    )

    await _reconnect_after_gap(sim)

    result = sim.entry.runtime_data.last_history_result
    assert result is not None
    assert result.source == "files"
    assert result.hours_imported == 3
    imported = {
        call.args[1]["statistic_id"]: call.args[2]
        for call in mock_recorder_calls.call_args_list
    }
    assert imported["sensor.wican_pro_sim_soc"][0]["mean"] == pytest.approx(72.0)


async def test_sync_disabled_by_option(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """The options kill switch prevents any backfill."""
    sim = MeatPiDeviceSimulator.from_preset(hass, hass_client, "wican_pro_v6")
    sim.attach_api(aioclient_mock)
    await sim.async_setup(options={CONF_HISTORY_SYNC: False})
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 71})},
    )
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)

    mock_recorder_calls.assert_not_called()
    assert sim.api.count("GET", "/api/logger/export") == 0


async def test_sync_rate_limited(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """Back-to-back gap pushes cannot hammer the device."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")
    baseline = sim.api.count("GET", "/api/logger/export")

    await _reconnect_after_gap(sim)
    first_round = sim.api.count("GET", "/api/logger/export")
    assert first_round > baseline

    # Second gap immediately after: rate limit (not reset this time) holds,
    # and the run is booked for the end of the window instead of dropped
    # (a car home again minutes after the run at Home Assistant's start).
    sim.entry.runtime_data.history_followup = None
    sim.go_stale()
    await sim.push_and_settle(sim.status())
    assert sim.api.count("GET", "/api/logger/export") == first_round
    assert sim.entry.runtime_data.history_followup is not None
    assert sim.entry.runtime_data.history_followup_due is not None


async def test_device_offline_during_sync_is_contained(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """The device vanishing right after reconnect harms nothing."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")
    sim.api.mode = "offline"

    sim.go_stale()
    sim.entry.runtime_data.last_history_sync = 0.0
    # The push itself still arrives (webhook is device→HA, not HTTP API).
    await sim.push_and_settle(sim.status())

    mock_recorder_calls.assert_not_called()
    # Telemetry unaffected; a later successful sync is still possible.
    sim.api.mode = "ok"
    sim.entry.runtime_data.last_history_sync = 0.0
    sim.go_stale()
    await sim.push_and_settle(sim.status())
    assert mock_recorder_calls.called


async def test_hostile_export_body_is_contained(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """A garbage export body degrades to zero rows, never an exception."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.api.export_supported = True
    sim.api.export_payload_override = "ceci n'est pas du JSON\n{{{{\n\x00"

    await _reconnect_after_gap(sim)

    mock_recorder_calls.assert_not_called()
    # Integration alive and pushing.
    await sim.push_and_settle(
        {**sim.status(), **sim.pids({"SOC": 42})},
    )
    assert hass.states.get("sensor.wican_pro_sim_soc").state == "42"


async def test_watermark_prevents_reimport(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """A second sync fetches only rows newer than the watermark."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)
    first_imports = mock_recorder_calls.call_count
    assert first_imports > 0

    # Reconnect again with no new rows: the export filter (ts > since)
    # returns nothing, so no further imports happen.
    await _reconnect_after_gap(sim)
    assert mock_recorder_calls.call_count == first_imports


# ---------------------------------------------------------------------------
# Real recorder round-trip
# ---------------------------------------------------------------------------


async def _recorder_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry,
) -> tuple[MockConfigEntry, str]:
    """Set up the integration against the real in-memory recorder."""
    mock_config_entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ),
        # The round-trip tests swap in a mocked API client below; keep the
        # setup-time capability probe off the network too.
        patch("custom_components.wican.MeatPiApiClient") as client_cls,
    ):
        client_cls.return_value = AsyncMock()
        client_cls.return_value.async_probe.return_value = LEGACY_CAPABILITIES
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    entry_reg = registry.async_get_or_create(
        "sensor", DOMAIN, f"{mock_config_entry.entry_id}_pid_SOC",
    )
    mock_config_entry.runtime_data.api = AsyncMock(spec=MeatPiApiClient)
    return mock_config_entry, entry_reg.entity_id


async def _read_back(hass: HomeAssistant, entity_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            dt_util.utc_from_timestamp(H1 - 3600),
            None,
            {entity_id},
            "hour",
            None,
            {"mean", "min", "max"},
        ),
    )
    return stats.get(entity_id, [])


async def test_round_trip_into_real_recorder(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Imported statistics land in the recorder and read back correctly."""
    entry, entity_id = await _recorder_entry(hass, mock_config_entry)
    entry.runtime_data.api.async_export_log_rows = AsyncMock(
        side_effect=[
            LogExportPage(
                rows=[
                    {"ts": H1 + 60, "name": "SOC", "value": 70},
                    {"ts": H1 + 600, "name": "SOC", "value": 74},
                    {"ts": H2 + 60, "name": "SOC", "value": 65},
                ],
                cursor="1793900000:9",
            ),
        ],
    )

    result = await async_sync_history(hass, entry)
    assert result.hours_imported == 2

    rows = await _read_back(hass, entity_id)
    assert len(rows) == 2
    assert rows[0]["mean"] == pytest.approx(72.0)
    assert rows[0]["min"] == 70
    assert rows[0]["max"] == 74
    assert rows[1]["mean"] == pytest.approx(65.0)
    hour_starts = {int(row["start"]) for row in rows}
    assert hour_starts == {H1, H2}


async def test_round_trip_reimport_is_idempotent(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Re-importing the same hours (watermark lost) creates no duplicates."""
    entry, entity_id = await _recorder_entry(hass, mock_config_entry)
    rows = [
        LogExportPage(rows=[{"ts": H1 + 60, "name": "SOC", "value": 70}], cursor="1793900000:9"),
        LogExportPage(rows=[{"ts": H1 + 60, "name": "SOC", "value": 70}], cursor="1793900000:9"),
    ]
    entry.runtime_data.api.async_export_log_rows = AsyncMock(side_effect=rows)

    first = await async_sync_history(hass, entry)
    assert first.hours_imported == 1
    await async_wait_recording_done(hass)

    # Simulate a lost watermark (e.g. storage wiped) → same rows re-fetched.
    from custom_components.wican.history import HistorySync

    await HistorySync(hass, entry)._async_save(0.0, None)
    second = await async_sync_history(hass, entry)
    await async_wait_recording_done(hass)

    read = await _read_back(hass, entity_id)
    assert len(read) == 1  # updated in place, not duplicated
    assert read[0]["mean"] == pytest.approx(70.0)
    assert second.errors == [] or second.hours_imported <= 1



# ---------------------------------------------------------------------------
# Store and forward against the simulator's firmware shape (2026-11-06)
# ---------------------------------------------------------------------------


async def test_rows_before_a_clock_step_are_rebased_with_the_journal(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """A drive recorded before the device's clock was set (a dead RTC until
    the first time sync) is stamped an hour early; the clock journal names
    the file and the step, and the rows land at the corrected hour."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    early = [{"ts": H1 - 3600 + i, "name": "SOC", "value": 50 + i} for i in range(0, 1200, 60)]
    sim.seed_log_rows(early, via="export")
    fname = f"dl_{sim.api.export_file_epoch}.jsonl"
    boot_ms = (H1 - 3600 - 10) * 1000
    before_ms = (H1 - 3600 + 1300) * 1000
    sim.api.log_files["clock.jsonl"] = "\n".join([
        json.dumps({"ev": "boot", "boot": 7, "ts": boot_ms, "up_ms": 1000, "valid": False}),
        json.dumps({"ev": "step", "boot": 7, "ts_before": before_ms, "ts_after": before_ms + 3600000,
                    "delta_ms": 3600000, "source": "sntp", "files": [fname]}),
    ]) + "\n"

    await _reconnect_after_gap(sim)
    result = sim.entry.runtime_data.last_history_result
    assert result is not None
    assert result.rows_rebased == len(early)
    assert result.rows_rejected == 0
    assert result.hours_imported == 1
    # the watermark is the end of the imported hour: the corrected one
    assert result.watermark == H1 + 3600


async def test_cursor_is_kept_and_reused_across_runs(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """The second pull starts at the device cursor the first one saved."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")

    await _reconnect_after_gap(sim)
    first = sim.entry.runtime_data.last_history_result
    assert first is not None and first.hours_imported == 3
    assert first.cursor == f"{sim.api.export_file_epoch}:{len(OFFLINE_DRIVE_ROWS)}"
    # nothing known yet: the pull starts at the age limit as a plain epoch
    assert sim.api.export_calls[0].isdigit()

    sim.seed_log_rows([{"ts": H2 + 120, "name": "SOC", "value": 60}], via="export")
    await _reconnect_after_gap(sim)
    assert sim.api.export_calls[-1] == first.cursor
    second = sim.entry.runtime_data.last_history_result
    assert second is not None and second.rows_fetched == 1


async def test_current_hour_rows_wait_for_a_followup(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """Rows of the hour still running book a follow-up and hold the cursor."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(
        OFFLINE_DRIVE_ROWS + [{"ts": HOUR + 30, "name": "SOC", "value": 55}],
        via="export",
    )

    await _reconnect_after_gap(sim)

    result = sim.entry.runtime_data.last_history_result
    assert result is not None
    assert result.hours_imported == 3
    assert result.rows_deferred == 1
    assert result.followup_in is not None and result.followup_in > 0
    assert sim.entry.runtime_data.history_followup is not None
    assert sim.entry.runtime_data.history_followup_due is not None
    # the follow-up re-reads the page that carried the deferred row: the
    # one and only page, so the cursor stays where that page started
    assert result.cursor == sim.api.export_calls[0]
    assert await HistorySync(hass, sim.entry).async_get_cursor() == sim.api.export_calls[0]


async def test_catch_up_history_button_runs_a_sync(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """The Catch up history button pulls the log without a reconnect."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.seed_log_rows(OFFLINE_DRIVE_ROWS, via="export")
    entity_id = f"button.{sim.entry.title.lower().replace(' ', '_')}_catch_up_history"
    state = hass.states.get(entity_id)
    assert state is not None, [s.entity_id for s in hass.states.async_all("button")]

    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True,
    )
    await hass.async_block_till_done()

    assert sim.api.count("GET", "/api/logger/export") >= 1
    result = sim.entry.runtime_data.last_history_result
    assert result is not None and result.hours_imported == 3


async def test_unsupported_params_format_is_reported(
    hass: HomeAssistant,
    hass_client: Any,
    aioclient_mock: Any,
    mock_recorder_calls: MagicMock,
) -> None:
    """A device logging sqlite answers 400: reported, nothing imported."""
    sim = await _online_device(hass, hass_client, aioclient_mock)
    sim.api.export_supported = True
    sim.api.export_unsupported_format = True

    await _reconnect_after_gap(sim)

    result = sim.entry.runtime_data.last_history_result
    assert result is not None and result.ran
    assert result.hours_imported == 0
    assert any("not JSON lines or CSV" in e for e in result.errors)
    mock_recorder_calls.assert_not_called()
