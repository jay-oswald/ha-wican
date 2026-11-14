"""Client for the HTTP API of MeatPi devices (WiCAN firmware V6+).

Telemetry always arrives via webhook push (the only channel legacy firmware
has). This client is the *control and discovery* channel: it probes whether
the device exposes the V6 ``/api/*`` surface, learns which firmware
components are present (the capability set), and executes control actions
such as reboot or time sync.

The probe is deliberately forgiving: any failure yields legacy capabilities,
never an exception, so a pre-V6 device — or an unreachable one — simply gets
no control entities and everything else behaves exactly as before.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from yarl import URL

from .const import (
    API_LEVEL_LEGACY,
    API_LEVEL_V6,
    API_PROBE_TIMEOUT,
    API_REQUEST_TIMEOUT,
    API_RESTART_PATH,
    API_RTC_SYNC_PATH,
    API_SETTINGS_PATH,
    API_STATUS_PATH,
    FS_DOWNLOAD_PATH,
    FS_LIST_PATH,
    GPX_PAGE_BYTES,
    HISTORY_EXPORT_PAGE_BYTES,
    CLOCK_JOURNAL_MAX_BYTES,
    CLOCK_JOURNAL_PATH,
    LOGGER_EXPORT_PATH,
    LOGGER_GPX_PATH,
    LOGGER_STATUS_PATH,
    MAX_API_COMPONENT_NAME_LENGTH,
    MAX_API_COMPONENTS,
    MAX_API_RESPONSE_BYTES,
)
from .exceptions import MeatPiApiConnectionError, MeatPiApiError
from .helpers import async_read_capped

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)


@dataclass
class LogExportPage:
    """One page of the device's params log export (history catch-up)."""

    rows: list[Any] = field(default_factory=list)  # raw rows; history.py validates
    cursor: str | None = None  # the device's resume cursor, None when absent
    more: bool = False  # the device says: call again at once
    files: list[str] = field(default_factory=list)  # the files the rows came from
    unsupported_format: bool = False  # the params stream is not jsonl/csv (400)


_GPX_TRAILER = re.compile(r'<!-- cursor="([^"]*)" more="(true|false)" points="(\d+)" -->')


@dataclass
class GpxPage:
    """One GPX document from the device (the GPS track, 2026-11-06)."""

    text: str = ""
    cursor: str | None = None  # where the device's scan stopped
    more: bool = False  # the device has more points past the cursor
    points: int = 0
    unsupported_format: bool = False


@dataclass(frozen=True)
class DeviceCapabilities:
    """What a running device can do, learned from the capability probe."""

    api_level: int = API_LEVEL_LEGACY
    components: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_http_api(self) -> bool:
        """Return True when the device exposes the V6 control API."""
        return self.api_level >= API_LEVEL_V6

    def has_component(self, name: str) -> bool:
        """Return True when the named firmware component is present."""
        return name in self.components


LEGACY_CAPABILITIES = DeviceCapabilities()


class MeatPiApiClient:
    """Minimal async client for the MeatPi device HTTP API.

    Uses the injected shared Home Assistant aiohttp session (never creates
    its own) and raises :class:`MeatPiApiError` for any transport or HTTP
    failure so callers have exactly one error surface to handle.
    """

    def __init__(self, session: ClientSession, base_url: str) -> None:
        """Initialize the client against a device base URL (http://host[:port])."""
        self._session = session
        self._base = URL(base_url)

    @property
    def base_url(self) -> str:
        """Return the device base URL this client talks to."""
        return str(self._base)

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        request_timeout: float = API_REQUEST_TIMEOUT,
        max_bytes: int = MAX_API_RESPONSE_BYTES,
    ) -> tuple[str, Mapping[str, str]]:
        """Perform one API request; return the body text and headers.

        Raises MeatPiApiConnectionError on transport failures/timeouts and
        MeatPiApiError (with ``status``) on HTTP-level errors.
        """
        url = self._base.with_path(path)
        if params:
            url = url.with_query(params)
        try:
            async with asyncio.timeout(request_timeout):
                response = await self._session.request(method, url)
                # A glitching or hostile device must not be able to balloon
                # memory with a giant body: an honest declared length is
                # refused before any read, and a chunked body (no length)
                # is streamed so the read stops at the cap.
                raw = await async_read_capped(response, max_bytes)
                if raw is None:
                    raise MeatPiApiError(
                        f"Device API {method} {path} response too large "
                        f"(> {max_bytes} bytes)",
                    )
                text = raw.decode("utf-8", errors="replace")
                if response.status >= 400:
                    raise MeatPiApiError(
                        f"Device API {method} {path} failed with "
                        f"HTTP {response.status}: {text[:200]}",
                        status=response.status,
                    )
                return text, response.headers
        except TimeoutError as err:
            raise MeatPiApiConnectionError(
                f"Device API {method} {path} timed out after {request_timeout}s",
            ) from err
        except (ClientError, ValueError, UnicodeDecodeError) as err:
            raise MeatPiApiConnectionError(
                f"Device API {method} {path} failed: {err}",
            ) from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        request_timeout: float = API_REQUEST_TIMEOUT,
    ) -> Any:
        """Perform one API request and return the decoded JSON body (or None).

        Parses tolerantly instead of trusting the Content-Type header; a
        non-JSON body (e.g. a legacy web UI answering the probe) simply
        yields None.
        """
        text, _ = await self._request_text(
            method, path, request_timeout=request_timeout,
        )
        try:
            return json.loads(text)
        except ValueError:
            return None

    async def async_probe(self) -> DeviceCapabilities | None:
        """Probe the device for its API level and firmware components.

        A V6 device is recognized by the ``bits`` object in ``GET
        /api/status``; the component list then comes from ``GET
        /api/settings``.

        Returns None when the device could not be reached at all (asleep,
        offline, mid-reboot) — the caller keeps its last-known capabilities.
        A reachable device that answers anything other than the V6 shape
        (HTTP error, HTML page, legacy JSON) is a *positive* legacy signal
        and yields legacy capabilities. Never raises.
        """
        try:
            status = await self._request(
                "GET", API_STATUS_PATH, request_timeout=API_PROBE_TIMEOUT,
            )
        except MeatPiApiConnectionError as err:
            _LOGGER.debug(
                "Capability probe of %s: device unreachable (%s)", self._base, err,
            )
            return None
        except MeatPiApiError as err:
            _LOGGER.debug("Capability probe of %s: no HTTP API (%s)", self._base, err)
            return LEGACY_CAPABILITIES

        if not isinstance(status, dict) or not isinstance(status.get("bits"), dict):
            _LOGGER.debug(
                "Capability probe of %s: /api/status has no V6 shape", self._base,
            )
            return LEGACY_CAPABILITIES

        components: frozenset[str] = frozenset()
        try:
            settings = await self._request(
                "GET", API_SETTINGS_PATH, request_timeout=API_PROBE_TIMEOUT,
            )
        except MeatPiApiConnectionError as err:
            # The device flaked out between the two probe requests; report
            # unreachable so the caller keeps its last-known capabilities
            # instead of downgrading to "V6 with no components".
            _LOGGER.debug(
                "Capability probe of %s: device lost mid-probe (%s)",
                self._base,
                err,
            )
            return None
        except MeatPiApiError as err:
            # Still a V6 device; component-gated entities just stay absent.
            _LOGGER.debug(
                "Capability probe of %s: component list unavailable (%s)",
                self._base,
                err,
            )
        else:
            components = self._parse_components(settings)

        capabilities = DeviceCapabilities(
            api_level=API_LEVEL_V6, components=components,
        )
        _LOGGER.debug(
            "Capability probe of %s: V6 API with %d components",
            self._base,
            len(components),
        )
        return capabilities

    def _parse_components(self, settings: Any) -> frozenset[str]:
        """Extract the firmware component names from a /api/settings body.

        Bounds what a glitching device can put in the capability set: caps
        the count and skips absurd or non-string names.
        """
        raw_components = (
            settings.get("components") if isinstance(settings, dict) else None
        )
        if not isinstance(raw_components, list):
            return frozenset()
        names: set[str] = set()
        for component in raw_components:
            if len(names) >= MAX_API_COMPONENTS:
                _LOGGER.debug(
                    "Capability probe of %s: component list capped at %d entries",
                    self._base,
                    MAX_API_COMPONENTS,
                )
                break
            if not isinstance(component, dict):
                continue
            name = component.get("name")
            if (
                isinstance(name, str)
                and name
                and len(name) <= MAX_API_COMPONENT_NAME_LENGTH
            ):
                names.add(name)
        return frozenset(names)

    async def async_restart(self) -> None:
        """Reboot the device (POST /api/restart)."""
        await self._request("POST", API_RESTART_PATH)

    async def async_sync_time(self) -> None:
        """Trigger an on-demand SNTP sync (POST /api/rtc/sync).

        The firmware blocks for up to ~32 s in the worst case, hence the
        generous request timeout.
        """
        await self._request("POST", API_RTC_SYNC_PATH)

    async def async_get_logger_status(self) -> dict[str, Any] | None:
        """Return the data-logger status (GET /api/logger), or None."""
        result = await self._request("GET", LOGGER_STATUS_PATH)
        return result if isinstance(result, dict) else None

    async def async_export_log_rows(
        self,
        since: str | None,
        limit: int,
    ) -> LogExportPage | None:
        """Fetch one page of logged rows via the cursor export route.

        ``since`` is the device's own cursor from a previous page
        (``<file-epoch>:<offset>``), a plain epoch (seconds or
        milliseconds: the rows from that moment on), or None for the
        oldest retained file. The firmware answers NDJSON: a ``_file``
        line ahead of each file's rows, the rows verbatim (``ts`` in epoch
        milliseconds, ``param`` as ``<source>.<name>``), then a FINAL
        ``_cursor`` line with ``more``; a CSV params stream answers
        ``ts_ms,param,value`` rows under one header per file. The old
        proposal shape (bare rows + an ``X-Next-Since`` header) is read
        too.

        Returns None when the firmware does not implement the export route
        (HTTP 404) so the caller can fall back to file downloads; a page
        flagged ``unsupported_format`` when the device's params stream is
        in a format the route cannot serve (HTTP 400).
        """
        params = {"stream": "params", "limit": str(limit)}
        if since:
            params["since"] = since
        try:
            text, headers = await self._request_text(
                "GET",
                LOGGER_EXPORT_PATH,
                params=params,
                max_bytes=HISTORY_EXPORT_PAGE_BYTES,
            )
        except MeatPiApiError as err:
            if err.status == HTTPStatus.NOT_FOUND:
                return None
            if err.status == HTTPStatus.BAD_REQUEST:
                return LogExportPage(unsupported_format=True)
            raise

        page = LogExportPage()
        skipped = 0
        current_file: str | None = None  # the `_file` line ahead of a file's rows
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("{"):
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    skipped += 1
                    continue
                if not isinstance(decoded, dict):
                    skipped += 1
                    continue
                if "_cursor" in decoded:
                    cursor = decoded["_cursor"]
                    page.cursor = cursor if isinstance(cursor, str) and cursor else None
                    page.more = bool(decoded.get("more"))
                    continue
                if "_file" in decoded:
                    if isinstance(decoded["_file"], str):
                        page.files.append(decoded["_file"])
                        current_file = decoded["_file"]
                    continue
                # the file a row came from rides along (the clock journal
                # re-bases rows per file, history.rebase_row)
                if current_file is not None and "_file" not in decoded:
                    decoded["_file"] = current_file
                page.rows.append(decoded)
                continue
            # csv: ts_ms,source.name,value (the header row is not numeric)
            fields = stripped.split(",", 2)
            if len(fields) == 3 and fields[0].strip().isdigit():
                row = {"ts": fields[0].strip(), "param": fields[1].strip(), "value": fields[2].strip()}
                if current_file is not None:
                    row["_file"] = current_file
                page.rows.append(row)
            else:
                skipped += 1
        if skipped:
            _LOGGER.debug(
                "Log export from %s: skipped %d undecodable line(s)",
                self._base,
                skipped,
            )

        if page.cursor is None:
            raw_cursor = headers.get("X-Next-Since")
            if raw_cursor is not None:
                try:
                    float(raw_cursor)
                except ValueError:
                    pass
                else:
                    page.cursor = raw_cursor
                    page.more = len(page.rows) >= limit
        return page

    async def async_get_clock_journal(self) -> list[dict[str, Any]]:
        """The device's clock journal (``/sd/logs/clock.jsonl``) as events.

        One JSON object per line: ``boot``, ``valid``, ``step`` (with
        ``ts_before``, ``ts_after``, ``delta_ms`` and the log files that
        were open) and ``rotate``. Empty when the device has no journal
        (no card, nothing happened yet, an older firmware): the rows are
        then taken as stamped.
        """
        try:
            text = await self.async_fs_download(
                CLOCK_JOURNAL_PATH, max_bytes=CLOCK_JOURNAL_MAX_BYTES,
            )
        except MeatPiApiError:
            return []
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                events.append(decoded)
        return events

    async def async_download_gpx(
        self,
        since: str | None,
        *,
        limit: int,
        until: int | None = None,
    ) -> GpxPage | None:
        """Fetch the recorded GPS fix as a GPX document (GET /api/logger/gpx).

        ``since`` as for the export (the device's cursor or a plain epoch).
        The device closes the document with a comment naming where its
        scan stopped and whether more follows. Returns None when the
        firmware has no such route (HTTP 404); a page flagged
        ``unsupported_format`` on HTTP 400 (a sqlite or binary log).
        """
        params = {"limit": str(limit)}
        if since:
            params["since"] = since
        if until:
            params["until"] = str(int(until))
        try:
            text, _ = await self._request_text(
                "GET",
                LOGGER_GPX_PATH,
                params=params,
                request_timeout=90,
                max_bytes=GPX_PAGE_BYTES,
            )
        except MeatPiApiError as err:
            if err.status == HTTPStatus.NOT_FOUND:
                return None
            if err.status == HTTPStatus.BAD_REQUEST:
                return GpxPage(unsupported_format=True)
            raise
        page = GpxPage(text=text, points=text.count("<trkpt "))
        match = _GPX_TRAILER.search(text)
        if match:
            page.cursor = match.group(1)
            page.more = match.group(2) == "true"
            page.points = int(match.group(3))
        return page

    async def async_fs_list(self, path: str) -> list[dict[str, Any]]:
        """List a device directory (GET /api/fs/list)."""
        result = await self._request_text(
            "GET", FS_LIST_PATH, params={"path": path},
        )
        try:
            listing = json.loads(result[0])
        except ValueError:
            return []
        entries = listing.get("entries") if isinstance(listing, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    async def async_fs_download(self, path: str, *, max_bytes: int) -> str:
        """Download a device file as text (GET /api/fs/download), bounded."""
        text, _ = await self._request_text(
            "GET", FS_DOWNLOAD_PATH, params={"path": path}, max_bytes=max_bytes,
        )
        return text
