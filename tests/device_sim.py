"""A simulated MeatPi device for end-to-end integration tests.

:class:`MeatPiDeviceSimulator` drives the *real* code paths: it pushes data to
Home Assistant's actual webhook endpoint via ``hass_client`` (so the real
``handle_webhook`` handler, coordinator, and entities run), and Home Assistant
registers / OTA-updates the device through a configurable mock aiohttp session.

:class:`SimulatedDeviceApi` is the device-side HTTP surface (firmware V6+):
wired into ``aioclient_mock``, it answers the integration's *real* capability
probe and control commands, with programmable failure modes (offline, timeout,
HTTP 403/500, garbage bodies, legacy shapes). Together with the device-type
presets it can impersonate every MeatPi product: legacy WiCAN, V6 WiCAN
Pro/USB, ESPNetlink, or a generic future device.

Used by ``test_device_simulation.py`` (baseline behaviour),
``test_device_chaos.py`` (failure injection / hostile scenarios) and
``test_device_type_matrix.py`` (per-device-type end-to-end + control API).
"""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.wican.const import CONF_POST_INTERVAL, DOMAIN
from tests.conftest import MockConfigEntry

# Marker for "no payload override" (None must stay usable as a payload).
_UNSET = object()

# Firmware components a fully loaded V6 build reports.
V6_COMPONENTS_FULL: tuple[str, ...] = (
    "wifi_manager",
    "rtc_manager",
    "battery_monitor",
    "imu_manager",
    "autopid",
    "data_logger",
    "event_manager",
)

# Device-type presets: constructor overrides for MeatPiDeviceSimulator plus
# the device-side API shape for SimulatedDeviceApi. Each preset gets its own
# identity so several presets can coexist in one test.
DEVICE_PRESETS: dict[str, dict[str, Any]] = {
    "wican_legacy": {
        "title": "WiCAN OBD Sim",
        "fw_version": "v3.30",
        "hw_version": "WiCAN-OBD",
        "device_id": "wican_legacy_001",
        "mac": "AA:BB:CC:00:00:01",
        "hostname": "wican_legacy.local",
        "ip": "192.168.1.101",
        "webhook_id": "sim_hook_legacy",
        "api": {"api_level": 0},
    },
    "wican_pro_v6": {
        "title": "WiCAN Pro Sim",
        "fw_version": "6.0.1",
        "hw_version": "WiCAN-PRO v1.4",
        "device_id": "wican_pro_001",
        "mac": "AA:BB:CC:00:00:02",
        "hostname": "wican_pro.local",
        "ip": "192.168.1.102",
        "webhook_id": "sim_hook_pro",
        "api": {"api_level": 6, "components": V6_COMPONENTS_FULL},
    },
    "wican_usb_v6": {
        "title": "WiCAN USB Sim",
        "fw_version": "6.0.1",
        "hw_version": "WiCAN-USB",
        "device_id": "wican_usb_001",
        "mac": "AA:BB:CC:00:00:03",
        "hostname": "wican_usb.local",
        "ip": "192.168.1.103",
        "webhook_id": "sim_hook_usb",
        # No RTC on this build: the sync-time button must not appear.
        "api": {
            "api_level": 6,
            "components": ("wifi_manager", "battery_monitor", "autopid"),
        },
    },
    "espnetlink": {
        "title": "ESPNetlink Sim",
        "fw_version": "1.0.0",
        "hw_version": "ESPNetlink R1",
        "device_id": "espnetlink_001",
        "mac": "AA:BB:CC:00:00:04",
        "hostname": "espnetlink_sim.local",
        "ip": "192.168.1.104",
        "webhook_id": "sim_hook_netlink",
        # LTE/GPS product: no OBD fields, extra LTE status keys.
        "omit_obd_fields": True,
        "status_fields": {
            "lte_rssi": -71,
            "lte_operator": "TestNet",
            "lte_connected": "online",
        },
        "api": {
            "api_level": 6,
            "components": (
                "wifi_manager",
                "rtc_manager",
                "battery_monitor",
                "imu_manager",
            ),
        },
    },
    "meatpi_generic": {
        "title": "MeatPi Proto Sim",
        "fw_version": "0.9.0",
        "hw_version": "MeatPi ProtoBoard X",
        "device_id": "meatpi_proto_001",
        "mac": "AA:BB:CC:00:00:05",
        "hostname": "meatpi_proto.local",
        "ip": "192.168.1.105",
        "webhook_id": "sim_hook_proto",
        "api": {"api_level": 6, "components": ("wifi_manager", "rtc_manager")},
    },
}


class SimulatedWebhookFirmware:
    """The webhook settings a simulated device holds, and how its firmware treats them.

    ``reads_replies`` picks the firmware generation. False is every firmware
    before settings in the push reply: it reports no revision and ignores
    whatever Home Assistant answers. True is a firmware that reports
    ``status.webhook_rev`` in every push, applies the ``webhook`` object of
    a 200 reply and stores the ``rev`` of a registration.

    ``trusts_replies`` is the device's own verdict on the path a reply came
    in on (False: plain http to a public address, a Wi-Fi network marked
    untrusted). ``pinned`` is the user's ``manual_override`` on the device:
    it ignores a registration and a reply, and reports no revision.
    """

    URL_MAX = 191
    REPLY_MAX = 2048
    REV_MAX = 16
    _REV_CHARS = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
    )

    def __init__(self, *, reads_replies: bool = False) -> None:
        self.reads_replies = reads_replies
        self.trusts_replies = True
        self.pinned = False
        self.url = ""
        self.urls: list[str] = []
        self.enabled = False
        self.interval = 60
        self.rev = ""
        # Every registration body the device was sent, in order.
        self.registrations: list[dict[str, Any]] = []
        self.replies_applied = 0
        # The verdict on every reply that carried a body, in order.
        self.reply_verdicts: list[str] = []

    def reported_rev(self) -> str | None:
        """The revision this device puts in a push, None when it sends none."""
        if not self.reads_replies or self.pinned:
            return None
        return self.rev

    def changed_on_device(self, **fields: Any) -> None:
        """Change the webhook settings in the device's own web interface."""
        for key, value in fields.items():
            setattr(self, key, value)
        self.rev = ""

    def _url_ok(self, value: Any) -> bool:
        return (
            isinstance(value, str)
            and value.startswith(("http://", "https://"))
            and len(value) <= self.URL_MAX
        )

    def _rev_ok(self, value: Any) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= self.REV_MAX
            and set(value) <= self._REV_CHARS
        )

    def _strict_settings(self, body: Any) -> dict[str, Any] | None:
        """Validate a registration object as the firmware does with a ``rev``."""
        if not isinstance(body, dict):
            return None
        urls = body.get("urls", [body.get("url")])
        interval = body.get("interval")
        enabled = body.get("enabled", True)
        valid = (
            self._rev_ok(body.get("rev"))
            and self._url_ok(body.get("url"))
            and isinstance(urls, list)
            and 0 < len(urls) <= 2
            and urls[0] == body["url"]
            and all(self._url_ok(item) for item in urls)
            and isinstance(interval, int)
            and not isinstance(interval, bool)
            and 1 <= interval <= 3600
            and isinstance(enabled, bool)
        )
        if not valid:
            return None
        return {
            "url": body["url"],
            "urls": list(urls),
            "enabled": enabled,
            "interval": interval,
            "rev": body["rev"],
        }

    def _store(self, settings: dict[str, Any]) -> None:
        self.url = settings["url"]
        self.urls = settings["urls"]
        self.enabled = settings["enabled"]
        self.interval = settings["interval"]
        self.rev = settings["rev"]

    def shape(self) -> dict[str, Any]:
        """The body of ``GET /api/webhook`` (and of a registration's answer)."""
        body: dict[str, Any] = {
            "url": self.url,
            "enabled": self.enabled,
            "interval": self.interval,
            "manual_override": self.pinned,
        }
        if len(self.urls) > 1:
            body["urls"] = list(self.urls)
        if self.reads_replies:
            body["rev"] = self.reported_rev() or ""
        return body

    def register(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Take a ``POST /api/webhook`` body: (status, answer body)."""
        if not isinstance(body, dict):
            return 400, {"error": "json"}
        self.registrations.append(dict(body))
        if self.pinned and "manual_override" not in body:
            return 200, self.shape()
        first = not self.url
        if self.reads_replies and "rev" in body:
            settings = self._strict_settings(body)
            if settings is None:
                return 400, {"error": "invalid"}
            self._store(settings)
            return (201 if first else 200), self.shape()
        if not self._url_ok(body.get("url")):
            return 400, {"error": "url"}
        urls = body.get("urls")
        interval = body.get("interval")
        self._store(
            {
                "url": body["url"],
                "urls": list(urls) if isinstance(urls, list) else [body["url"]],
                "enabled": body.get("enabled", True) is True,
                # Out of range or missing: the firmware keeps what it has.
                "interval": interval
                if isinstance(interval, int) and 1 <= interval <= 3600
                else self.interval,
                "rev": "",
            },
        )
        return (201 if first else 200), self.shape()

    def take_reply(self, status: int, body: bytes) -> str:
        """Treat the reply to a push as the firmware does; returns the verdict.

        ``none`` is a reply without settings (no body, not a 200, no
        ``webhook`` object) or a firmware that does not read replies.
        Every other verdict is kept in :attr:`reply_verdicts`.
        """
        if self.reads_replies and status == 410:
            verdict = self._judge_gone(body)
            if verdict != "none":
                self.reply_verdicts.append(verdict)
            return verdict
        if not self.reads_replies or status != 200 or not body:
            return "none"
        verdict = self._judge_reply(body)
        if verdict != "none":
            self.reply_verdicts.append(verdict)
        return verdict

    def _judge_gone(self, body: bytes) -> str:
        """A 410: Home Assistant removed this device (firmware 6.0.0-beta.11).

        Only with Home Assistant's marker in the body; the webhook is
        cleared as ``DELETE /api/webhook`` clears it.
        """
        try:
            parsed = json.loads(body) if len(body) <= self.REPLY_MAX else None
        except ValueError:
            parsed = None
        webhook = parsed.get("webhook") if isinstance(parsed, dict) else None
        if not isinstance(webhook, dict) or webhook.get("removed") is not True:
            return "none"
        if self.pinned:
            return "pinned"
        if not self.trusts_replies:
            return "untrusted"
        self.url = ""
        self.urls = []
        self.enabled = False
        self.rev = ""
        return "removed"

    def _judge_reply(self, body: bytes) -> str:
        if len(body) > self.REPLY_MAX:
            return "too_long"
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return "not_json"
        if "webhook" not in parsed:
            return "none"
        return self._apply_reply(parsed["webhook"])

    def _apply_reply(self, webhook: Any) -> str:
        if self.pinned:
            return "pinned"
        if not self.trusts_replies:
            return "untrusted"
        settings = self._strict_settings(webhook)
        if settings is None:
            return "invalid"
        if settings["rev"] == self.rev:
            return "same"
        self._store(settings)
        self.replies_applied += 1
        return "applied"


class SimulatedDeviceApi:
    """The device-side HTTP API, wired into the aiohttp mocker.

    Answers the integration's real ``MeatPiApiClient`` requests. All state is
    live: change :attr:`mode`, :attr:`api_level`, or the payload overrides at
    any point in a test and the next request sees the new behavior.

    Modes:
      ok         normal behavior for the configured api_level
      offline    every request raises a connection error (device asleep/away)
      timeout    every request times out
      forbidden  HTTP 403 (the V6 network-trust lockdown)
      error      HTTP 500 on every request
      garbage    HTTP 200 with a non-JSON body
      html       HTTP 200 with an HTML page (very old firmware's catch-all UI)
    """

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        base_urls: list[str],
        *,
        api_level: int = 6,
        components: tuple[str, ...] = V6_COMPONENTS_FULL,
        fw_version: str = "6.0.1",
        mode: str = "ok",
        firmware: SimulatedWebhookFirmware | None = None,
    ) -> None:
        # The device's webhook settings: /api/webhook is served from them.
        self.firmware = firmware
        self.api_level = api_level
        self.components = list(components)
        self.fw_version = fw_version
        self.mode = mode
        # Optional raw payload overrides (hostile-shape injection). _UNSET
        # (not None) is the "no override" marker so tests can inject a
        # literal JSON null.
        self.status_payload: Any = _UNSET
        self.settings_payload: Any = _UNSET
        self.calls: list[tuple[str, str]] = []
        # Data-logger simulation: rows served by the cursor export route
        # (when export_supported) and/or rotated files served over /api/fs.
        self.export_supported = False
        self.export_rows: list[dict[str, Any]] = []
        self.export_payload_override: str | None = None
        # "firmware" = the shape the real route serves (epoch ms, the
        # `autopid.` prefix, `_file` and `_cursor` lines, a <epoch>:<offset>
        # cursor); "proposal" = the 2026-07 proposal (seconds, bare names,
        # an X-Next-Since header) the integration still accepts.
        self.export_shape = "firmware"
        self.export_file_epoch = 1793900000
        self.export_calls: list[str | None] = []  # the `since` of each call
        self.export_unsupported_format = False  # a sqlite/binary params stream
        # the GPS track route: None = a firmware without it (404); a list of
        # documents served page after page (the last one says more=false)
        self.gpx_documents: list[str] | None = None
        self.gpx_calls: list[dict[str, str]] = []
        self.logger_dir = "/sd/logs"
        self.log_files: dict[str, str] = {}
        self.active_log_file: str | None = None
        # Hosts that no longer answer (e.g. the car left home: the LAN
        # address dies while the VPN address keeps working).
        self.unreachable_hosts: set[str] = set()
        # An odd answer on /api/webhook for one method ("GET", "DELETE"):
        # the keyword arguments of the mock response served instead of
        # the firmware's own answer.
        self.webhook_answers: dict[str, dict[str, Any]] = {}
        self._aioclient_mock = aioclient_mock

        for base in base_urls:
            self.add_base(base)

    def add_base(self, base: str) -> None:
        """Register the device API routes on an additional base URL."""
        base_url = URL(base)
        for method, path in (
            ("get", "/api/status"),
            ("get", "/api/settings"),
            ("post", "/api/restart"),
            ("post", "/api/rtc/sync"),
            ("get", "/api/webhook"),
            ("post", "/api/webhook"),
            ("delete", "/api/webhook"),
            ("get", "/api/logger"),
            ("get", "/api/logger/export"),
            ("get", "/api/logger/gpx"),
            ("get", "/api/fs/list"),
            ("get", "/api/fs/download"),
        ):
            getattr(self._aioclient_mock, method)(
                str(base_url.with_path(path)), side_effect=self._respond,
            )

    # -- request counters --------------------------------------------------

    def count(self, method: str, path: str) -> int:
        return self.calls.count((method.upper(), path))

    @property
    def restart_calls(self) -> int:
        return self.count("POST", "/api/restart")

    @property
    def sync_time_calls(self) -> int:
        return self.count("POST", "/api/rtc/sync")

    @property
    def probe_calls(self) -> int:
        return self.count("GET", "/api/status")

    # -- behavior ------------------------------------------------------------

    async def _respond(
        self, method: str, url: URL, data: Any,
    ) -> AiohttpClientMockResponse:
        # Dead addresses never reach the device: not counted as calls.
        if url.host in self.unreachable_hosts:
            raise aiohttp.ClientConnectionError(
                f"simulated: {url.host} unreachable",
            )
        self.calls.append((method.upper(), url.path))

        if self.mode == "offline":
            raise aiohttp.ClientConnectionError("simulated: device offline")
        if self.mode == "timeout":
            raise TimeoutError("simulated: request timed out")
        if self.mode == "forbidden":
            return self._json(
                method, url, {"error": "configuration disabled on this network"},
                status=403,
            )
        if self.mode == "error":
            return self._json(
                method, url, {"error": "simulated internal error"}, status=500,
            )
        if self.mode == "garbage":
            return AiohttpClientMockResponse(method, url, text="<<<not json>>>")
        if self.mode == "html":
            return AiohttpClientMockResponse(
                method, url, text="<html>device ui</html>",
                headers={"Content-Type": "text/html"},
            )

        if self.firmware is not None and url.path == "/api/webhook":
            return self._webhook(self.firmware, method, url, data)
        return self._route(method, url)

    def _webhook(
        self,
        firmware: SimulatedWebhookFirmware,
        method: str,
        url: URL,
        data: Any,
    ) -> AiohttpClientMockResponse:
        """Serve /api/webhook from the simulated firmware's settings."""
        verb = method.upper()
        if verb in self.webhook_answers:
            return AiohttpClientMockResponse(
                method, url, **self.webhook_answers[verb],
            )
        if verb == "POST":
            status, body = firmware.register(data)
            return self._json(method, url, body, status=status)
        if verb == "DELETE":
            firmware.changed_on_device(url="", urls=[], enabled=False)
            return AiohttpClientMockResponse(method, url, status=204)
        return self._json(method, url, firmware.shape())

    def _route(self, method: str, url: URL) -> AiohttpClientMockResponse:
        path = url.path
        if path == "/api/status":
            if self.status_payload is not _UNSET:
                return self._json(method, url, self.status_payload)
            if self.api_level >= 6:
                return self._json(
                    method,
                    url,
                    {
                        "bits": {"sta_connected": True, "ap_enabled": False},
                        "version": self.fw_version,
                        "uptime": "0d 01:02:03",
                        "boot_count": 4,
                    },
                )
            # Legacy firmware answers /api/status with the flat status object
            # (no "bits"), a positive "not V6" signal.
            return self._json(
                method, url, {"batt_voltage": "12.6V", "ecu_status": "online"},
            )
        if path == "/api/settings":
            if self.settings_payload is not _UNSET:
                return self._json(method, url, self.settings_payload)
            if self.api_level >= 6:
                return self._json(
                    method,
                    url,
                    {
                        "components": [
                            {"name": name, "version": 1}
                            for name in self.components
                        ],
                    },
                )
            return self._json(method, url, {"error": "not found"}, status=404)
        if path == "/api/logger":
            return self._json(
                method,
                url,
                {
                    "enabled": True,
                    "running": True,
                    "paused": False,
                    "file": self.active_log_file,
                    "dir": self.logger_dir,
                },
            )
        if path == "/api/logger/export":
            return self._export(method, url)
        if path == "/api/logger/gpx":
            if self.gpx_documents is None:
                return self._json(method, url, {"error": "not found"}, status=404)
            self.gpx_calls.append(dict(url.query))
            index = min(len(self.gpx_calls) - 1, len(self.gpx_documents) - 1)
            return AiohttpClientMockResponse(method, url, text=self.gpx_documents[index])
        if path == "/api/fs/list":
            if url.query.get("path") != self.logger_dir:
                return self._json(method, url, {"error": "invalid path"}, status=400)
            return self._json(
                method,
                url,
                {
                    "path": self.logger_dir,
                    "entries": [
                        {"name": name, "dir": False, "size": len(content)}
                        for name, content in sorted(self.log_files.items())
                    ],
                },
            )
        if path == "/api/fs/download":
            requested = url.query.get("path", "")
            name = requested.rsplit("/", 1)[-1]
            if name == self.active_log_file:
                # The active file is write-locked on the real device.
                return self._json(method, url, {"error": "file locked"}, status=423)
            if name in self.log_files:
                return AiohttpClientMockResponse(
                    method, url, text=self.log_files[name],
                )
            return self._json(method, url, {"error": "not found"}, status=404)
        if path in ("/api/restart", "/api/rtc/sync") and self.api_level < 6:
            # Legacy firmware has no control routes.
            return self._json(method, url, {"error": "not found"}, status=404)
        if path == "/api/restart":
            return self._json(method, url, {"ok": True})
        if path == "/api/rtc/sync":
            return self._json(method, url, {"time": "2026-07-10T00:00:00Z"})
        if path == "/api/webhook":
            if method.upper() == "DELETE":
                return AiohttpClientMockResponse(method, url, status=204)
            return self._json(method, url, {"url": "", "enabled": True})
        return self._json(method, url, {"error": "unknown route"}, status=404)

    def _export(self, method: str, url: URL) -> AiohttpClientMockResponse:
        """Serve the cursor-based NDJSON export route (firmware ask #8)."""
        if not self.export_supported:
            return self._json(method, url, {"error": "not found"}, status=404)
        if self.export_unsupported_format:
            return AiohttpClientMockResponse(
                method, url, text="params stream is not jsonl or csv", status=400,
            )
        if self.export_payload_override is not None:
            return AiohttpClientMockResponse(
                method, url, text=self.export_payload_override,
            )
        raw_since = url.query.get("since")
        self.export_calls.append(raw_since)
        try:
            limit = max(1, int(url.query.get("limit", "1000")))
        except ValueError:
            limit = 1000
        ordered = sorted(self.export_rows, key=lambda row: row.get("ts", 0))

        if self.export_shape == "proposal":
            try:
                since = float(raw_since or "0")
            except ValueError:
                since = 0.0
            matching = [row for row in ordered if row.get("ts", 0) > since]
            page = matching[:limit]
            body = "\n".join(json.dumps(row) for row in page)
            headers = {}
            if page:
                headers["X-Next-Since"] = str(page[-1].get("ts", since))
            return AiohttpClientMockResponse(method, url, text=body, headers=headers)

        # The firmware's cursor is "<file-epoch>:<byte offset>"; the
        # simulator keeps one file and uses the row index as the offset. A
        # plain epoch (seconds or ms) means the rows from that moment on.
        start = 0
        if raw_since and ":" in raw_since:
            try:
                start = int(raw_since.split(":", 1)[1])
            except ValueError:
                start = 0
        elif raw_since:
            try:
                moment = float(raw_since)
            except ValueError:
                moment = 0.0
            if moment > 100_000_000_000:
                moment /= 1000.0
            start = next(
                (i for i, row in enumerate(ordered) if row.get("ts", 0) >= moment),
                len(ordered),
            )
        page = ordered[start:start + limit]
        lines = [json.dumps({"_file": f"dl_{self.export_file_epoch}.jsonl"})]
        for row in page:
            ts = row.get("ts", 0)
            lines.append(
                json.dumps(
                    {
                        "ts": int(round(float(ts) * 1000)),
                        "param": f"autopid.{row.get('name')}",
                        "value": row.get("value"),
                    },
                ),
            )
        end = start + len(page)
        lines.append(
            json.dumps(
                {"_cursor": f"{self.export_file_epoch}:{end}", "more": end < len(ordered)},
            ),
        )
        return AiohttpClientMockResponse(method, url, text="\n".join(lines) + "\n")

    @staticmethod
    def _json(
        method: str, url: URL, payload: Any, *, status: int = 200,
    ) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, json=payload, status=status)


class MeatPiDeviceSimulator:
    """Simulate a WiCAN device talking to the integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        hass_client_factory: Any,
        *,
        title: str = "WiCAN Sim",
        webhook_id: str = "sim_webhook_id",
        device_id: str | None = "wican_sim_001",
        mac: str = "AA:BB:CC:00:11:22",
        fw_version: str = "v4.49",
        hw_version: str = "WiCAN-Pro",
        hostname: str = "wican_sim.local",
        ip: str = "192.168.1.123",
        status_fields: dict[str, Any] | None = None,
        omit_obd_fields: bool = False,
        reads_replies: bool = False,
    ) -> None:
        self.hass = hass
        self._client_factory = hass_client_factory
        self._client: Any = None
        self.title = title
        self.webhook_id = webhook_id
        self.device_id = device_id
        self.mac = mac
        self.fw_version = fw_version
        self.hw_version = hw_version
        self.hostname = hostname
        self.ip = ip
        self.status_fields = dict(status_fields or {})
        self.omit_obd_fields = omit_obd_fields
        self.entry: MockConfigEntry | None = None
        self.api: SimulatedDeviceApi | None = None
        # The webhook settings this device holds. ``reads_replies`` makes it
        # a firmware that reports a settings revision and reads push replies.
        self.firmware = SimulatedWebhookFirmware(reads_replies=reads_replies)
        # What the firmware made of the reply to the last push.
        self.last_reply = "none"
        self._preset_api_kwargs: dict[str, Any] = {}

    @classmethod
    def from_preset(
        cls,
        hass: HomeAssistant,
        hass_client_factory: Any,
        preset: str,
        **overrides: Any,
    ) -> MeatPiDeviceSimulator:
        """Create a simulator for one of the DEVICE_PRESETS product types."""
        config = dict(DEVICE_PRESETS[preset])
        api_kwargs = dict(config.pop("api"))
        config.update(overrides)
        sim = cls(hass, hass_client_factory, **config)
        sim._preset_api_kwargs = api_kwargs
        return sim

    def attach_api(
        self,
        aioclient_mock: AiohttpClientMocker,
        **overrides: Any,
    ) -> SimulatedDeviceApi:
        """Wire up the device-side HTTP API on this device's addresses.

        Must be called *before* :meth:`async_setup` so the integration's real
        setup-time capability probe finds the routes registered.
        """
        kwargs: dict[str, Any] = {
            "fw_version": self.fw_version,
            "firmware": self.firmware,
        }
        kwargs.update(self._preset_api_kwargs)
        kwargs.update(overrides)
        self.api = SimulatedDeviceApi(
            aioclient_mock,
            [f"http://{self.ip}", f"http://{self.hostname}"],
            **kwargs,
        )
        return self.api

    # -- lifecycle -------------------------------------------------------

    async def async_setup(
        self,
        *,
        store_device_id: bool = True,
        register_result: bool = True,
        options: dict[str, Any] | None = None,
        device_type: str | None = None,
        real_registration: bool = False,
        minor_version: int = 3,
    ) -> MockConfigEntry:
        """Create the config entry and set up the integration.

        ``minor_version`` is the schema the entry was written at: an older
        one simulates an entry an earlier release made, which the
        integration migrates as it sets it up.

        ``device_type`` simulates an entry created from a discovery that
        carried a device_type TXT record (new firmware); without it the
        integration infers the type from the hardware version.

        ``real_registration`` lets the integration's own registration run
        against the simulated device API (:meth:`attach_api` first) instead
        of a stub that returns ``register_result``.
        """
        data: dict[str, Any] = {
            CONF_WEBHOOK_ID: self.webhook_id,
            "mdns": f"http://{self.hostname}",
            "host": f"http://{self.ip}",
            "mac": self.mac,
            "fw_version": self.fw_version,
            "hw_version": self.hw_version,
            # The test client connects from localhost; pre-seeding the captured
            # request IP keeps ordinary pushes from looking like a connection
            # change (which would fire a real re-registration attempt).
            "ip": "127.0.0.1",
        }
        if device_type is not None:
            data["device_type"] = device_type
        if store_device_id and self.device_id:
            data["device_id"] = self.device_id

        self.entry = MockConfigEntry(
            domain=DOMAIN,
            title=self.title,
            data=data,
            options=options or {CONF_POST_INTERVAL: 15},
            unique_id=self.mac.replace(":", "").lower(),
            version=1,
            minor_version=minor_version,
        )
        self.entry.add_to_hass(self.hass)

        if real_registration:
            assert await self.hass.config_entries.async_setup(self.entry.entry_id)
            await self.hass.async_block_till_done()
            return self.entry

        with patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=register_result,
        ):
            assert await self.hass.config_entries.async_setup(self.entry.entry_id)
            await self.hass.async_block_till_done()
        return self.entry

    async def async_reload(self) -> None:
        """Reload the config entry (device keeps pushing afterwards)."""
        assert self.entry is not None
        with patch(
            "custom_components.wican._async_register_webhook_on_device",
            return_value=True,
        ):
            assert await self.hass.config_entries.async_reload(self.entry.entry_id)
            await self.hass.async_block_till_done()

    @property
    def coordinator(self) -> Any:
        assert self.entry is not None
        return self.entry.runtime_data.coordinator

    # -- device -> HA push ----------------------------------------------

    async def push(
        self,
        payload: Any = None,
        *,
        raw: str | bytes | None = None,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> Any:
        """POST a webhook payload to HA as the device would."""
        if self._client is None:
            self._client = await self._client_factory()
        url = f"/api/webhook/{self.webhook_id}"
        if raw is not None:
            all_headers = {"Content-Type": content_type, **(headers or {})}
            resp = await self._client.post(url, data=raw, headers=all_headers)
        elif headers is not None:
            resp = await self._client.post(url, json=payload, headers=headers)
        else:
            resp = await self._client.post(url, json=payload)
        if self.firmware.reads_replies:
            # The firmware reads the reply to its own push.
            self.last_reply = self.firmware.take_reply(resp.status, await resp.read())
        return resp

    async def push_and_settle(self, payload: Any) -> Any:
        """Push a payload and let all resulting tasks run."""
        resp = await self.push(payload)
        await self.hass.async_block_till_done()
        await self.coordinator.async_refresh()
        await self.hass.async_block_till_done()
        return resp

    def go_stale(self) -> None:
        """Simulate the device having stopped pushing for a long time."""
        self.coordinator._last_push = dt_util.utcnow() - timedelta(hours=6)

    def reboot(
        self,
        *,
        fw_version: str | None = None,
        hw_version: str | None = None,
    ) -> None:
        """Simulate a device reboot, optionally with new firmware.

        The next :meth:`status` payload reports the new versions, exactly as a
        real device does after an OTA update.
        """
        if fw_version is not None:
            self.fw_version = fw_version
        if hw_version is not None:
            self.hw_version = hw_version

    def seed_log_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        via: str = "export",
        file_format: str = "jsonl",
        filename: str | None = None,
        active: bool = False,
    ) -> None:
        """Seed offline-drive rows into the simulated device's data logger.

        ``via="export"`` serves them through the cursor export route;
        ``via="file"`` writes a rotated log file (jsonl or csv) served over
        the /api/fs routes, optionally marked as the write-locked active
        file.
        """
        assert self.api is not None, "attach_api() must be called first"
        if via == "export":
            self.api.export_supported = True
            self.api.export_rows.extend(rows)
            return
        first_ts = int(rows[0]["ts"]) if rows else 0
        extension = "csv" if file_format == "csv" else "jsonl"
        name = filename or f"dl_{first_ts}.{extension}"
        if file_format == "csv":
            lines = ["ts,name,value"]
            lines.extend(
                f"{row['ts']},{row['name']},{row['value']}" for row in rows
            )
            content = "\n".join(lines)
        else:
            content = "\n".join(json.dumps(row) for row in rows)
        self.api.log_files[name] = content
        if active:
            self.api.active_log_file = name

    def set_vpn(self, vpn_ip: str) -> None:
        """Make the device's API reachable on its VPN tunnel address too."""
        assert self.api is not None, "attach_api() must be called first"
        self.api.add_base(f"http://{vpn_ip}")

    def leave_home(self) -> None:
        """Simulate the car driving away: LAN addresses stop answering."""
        assert self.api is not None, "attach_api() must be called first"
        self.api.unreachable_hosts.update({self.ip, self.hostname})

    def return_home(self) -> None:
        """Simulate the car back on the home network."""
        assert self.api is not None, "attach_api() must be called first"
        self.api.unreachable_hosts.difference_update({self.ip, self.hostname})

    def ota_to_v6(
        self,
        fw_version: str = "6.0.1",
        components: tuple[str, ...] = V6_COMPONENTS_FULL,
    ) -> None:
        """Simulate an OTA update to V6 firmware (device + its HTTP API).

        The next status push reports the new firmware version, which makes
        the integration re-probe and discover the new control surface.
        """
        self.reboot(fw_version=fw_version)
        if self.api is not None:
            self.api.api_level = 6
            self.api.fw_version = fw_version
            self.api.components = list(components)

    # -- payload builders -----------------------------------------------

    def status(self, **fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "fw_version": self.fw_version,
            "hw_version": self.hw_version,
            "batt_voltage": "12.6V",
            "wifi_mode": "Station",
            "vpn_status": "Not Connected",
            "uptime": "01:23:45",
        }
        if not self.omit_obd_fields:
            base.update(
                {
                    "ecu_status": "online",
                    "obd_chip_status": "ok",
                    "ble_status": "Disabled",
                },
            )
        base.update(self.status_fields)
        if self.device_id:
            base["device_id"] = self.device_id
        reported_rev = self.firmware.reported_rev()
        if reported_rev is not None:
            base["webhook_rev"] = reported_rev
        base.update(fields)
        return {"status": base}

    def pids(
        self,
        values: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"autopid_data": values, "config": config or {}}

    def gps(self, lat: float, lon: float, **extra: Any) -> dict[str, Any]:
        return {"gps": {"latitude": lat, "longitude": lon, **extra}}
