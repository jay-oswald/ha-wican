"""Data models for the MeatPi integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .api import LEGACY_CAPABILITIES, DeviceCapabilities
from .devices import DEVICE_PROFILES, DEVICE_TYPE_WICAN

if TYPE_CHECKING:
    from collections.abc import Callable

    from .api import MeatPiApiClient
    from .coordinator import MeatPiDataUpdateCoordinator
    from .devices import MeatPiDeviceProfile
    from .github_releases import GitHubReleasesCoordinator
    from .history import HistorySyncResult


@dataclass
class PushReplyState:
    """Settings in the push reply: what the device reported, what was sent."""

    # The revision in the device's last push (None: it reported none) and
    # the revision of the settings Home Assistant wants it to have (None:
    # no settings could be built).
    reported_rev: str | None = None
    wanted_rev: str | None = None
    # The (reported, wanted) pair the last reply body answered and when
    # (monotonic seconds): one unchanged difference is not answered with a
    # body on every push.
    answered: tuple[str, str] | None = None
    answered_at: float = 0.0
    # Diagnostics: how many reply bodies went out, and when the last one did.
    bodies_sent: int = 0
    last_body_at: str | None = None


@dataclass
class MeatPiRuntimeData:
    """Runtime data for a MeatPi device config entry."""

    coordinator: MeatPiDataUpdateCoordinator
    github_coordinator: GitHubReleasesCoordinator
    webhook_id: str
    post_interval: int
    device_host: str | None = None
    device_ip: str | None = None
    # Device-reported VPN tunnel address (WireGuard/Tailscale), used as the
    # backup control endpoint when the local address is unreachable.
    device_vpn_ip: str | None = None
    cached_resolved_ip: str | None = None
    cache_timestamp: float = 0.0
    # Webhook-registration coalescing (see _async_request_webhook_registration)
    registration_running: bool = False
    registration_pending: bool = False
    # Whether a run that waits was asked for by something other than a push
    # (a start, a changed option): such a run reports its failure.
    registration_loud: bool = False
    # Whether the last registration could not reach the device, and when a
    # push last triggered a new attempt (monotonic seconds; 0.0 = never).
    registration_failed: bool = False
    last_registration_retry: float = 0.0
    # Settings in the push reply (see PushReplyState).
    push_reply: PushReplyState = field(default_factory=PushReplyState)
    # Device-type profile (brand framework; defaults to the WiCAN profile,
    # which matches every install that predates device types).
    device_profile: MeatPiDeviceProfile = field(
        default=DEVICE_PROFILES[DEVICE_TYPE_WICAN],
    )
    # Control channel: HTTP API client + probed capabilities. Legacy
    # (pre-V6) devices keep the defaults and get no control entities.
    api: MeatPiApiClient | None = None
    capabilities: DeviceCapabilities = LEGACY_CAPABILITIES
    # Capability-probe coalescing (see _async_request_capability_probe)
    probe_running: bool = False
    probe_pending: bool = False
    # Whether a probe has ever reached the device this runtime. While False,
    # incoming telemetry pushes retry the probe (rate-limited) so a device
    # that was asleep at setup still gains its control entities later.
    probe_successful: bool = False
    last_probe_retry: float = 0.0
    # History backfill (see history.py): run coalescing + rate limiting,
    # and the last run's counters for diagnostics.
    history_sync_running: bool = False
    history_sync_pending: bool = False
    last_history_sync: float = 0.0
    last_history_result: HistorySyncResult | None = None
    # A follow-up run the last sync asked for (the hour still running, a
    # device with more than one run could take): its cancel handle and
    # how many ran in a row (bounded).
    history_followup: Callable[[], None] | None = None
    history_followups: int = 0
    history_followup_due: float | None = None  # epoch seconds, diagnostics
