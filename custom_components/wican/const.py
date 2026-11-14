"""Constants for the MeatPi integration."""

# The Home Assistant domain is a permanent identifier: every existing config
# entry, entity-registry row, and device identifier is keyed by it, so it
# stays "wican" even though the integration is branded MeatPi (see
# notes/MEATPI_INTEGRATION_PLAN.md, decision D1).
DOMAIN = "wican"

MANUFACTURER = "MeatPi"

# Configuration
CONF_DEVICE_TYPE = "device_type"
CONF_POST_INTERVAL = "post_interval"
DEFAULT_POST_INTERVAL = 15  # seconds
MIN_POST_INTERVAL = 1
MAX_POST_INTERVAL = 3600

# Webhook Registration
WEBHOOK_REGISTRATION_TIMEOUT = 10  # seconds
WEBHOOK_RETRY_DELAY_BASE = 2  # seconds for exponential backoff
WEBHOOK_MAX_RETRIES = 3
# A firmware that does not read push replies whose registration failed is
# registered again when it pushes, at most this often.
REGISTRATION_RETRY_INTERVAL = 300  # seconds

# Settings in the push reply. A firmware that reads the reply to its own
# push reports the revision of the webhook settings it holds in every push
# (status.webhook_rev). When that is not the revision Home Assistant wants
# the device to have, the reply carries the settings: the only way to hand
# them to a car that pushes but cannot be reached (asleep at start, on LTE
# without a VPN).
WEBHOOK_REV_KEY = "webhook_rev"
WEBHOOK_REV_LENGTH = 8  # hex characters of the revision Home Assistant sends
WEBHOOK_REV_MAX_LENGTH = 16  # what the device stores and reports
# Entry data: the device reported a revision in its last push, so it reads
# replies and a registration may carry "rev".
CONF_READS_PUSH_REPLY = "reads_push_reply"
# The device decides by itself whether it trusts a reply, and Home Assistant
# cannot see the verdict: the body for one unchanged difference is repeated
# at most this often.
PUSH_REPLY_REPEAT_INTERVAL = 300  # seconds
PUSH_REPLY_MAX_BYTES = 2048  # the device ignores a longer reply body

# Minimum supported firmware per release stream (device contract v2).
# Older firmware keeps telemetry working but gets a persistent repair
# issue telling the user a firmware update is required; legacy device
# accommodations below these versions are no longer maintained.
MIN_FIRMWARE_BY_STREAM = {
    "pro": (6, 0),
    "obd": (5, 0),
    "usb": (5, 0),
}

# IP Caching
IP_CACHE_DURATION = 300  # 5 minutes in seconds

# mDNS Resolution
MDNS_RESOLUTION_TIMEOUT = 5  # seconds

# Device HTTP API (firmware V6+). Telemetry stays webhook-push on every
# firmware; these routes are the control/discovery surface of V6 devices.
API_STATUS_PATH = "/api/status"
API_SETTINGS_PATH = "/api/settings"
API_RESTART_PATH = "/api/restart"
API_RTC_SYNC_PATH = "/api/rtc/sync"
API_PROBE_TIMEOUT = 10  # seconds per probe request
API_REQUEST_TIMEOUT = 35  # seconds; /api/rtc/sync may block up to ~32 s

# Bounds on device API responses. A healthy device is far below these; a
# glitching or hostile one must not be able to balloon memory or the
# capability set.
MAX_API_RESPONSE_BYTES = 262144  # 256 KiB
MAX_API_COMPONENTS = 128
MAX_API_COMPONENT_NAME_LENGTH = 64

# When the capability probe has never succeeded (device asleep/unreachable at
# setup), retry it when telemetry pushes arrive — but at most this often.
PROBE_RETRY_INTERVAL = 300  # seconds

# History backfill (SD-card data logger → HA long-term statistics).
# See notes/HISTORICAL_DATA_SYNC.md for the design.
CONF_HISTORY_SYNC = "history_sync"
DEFAULT_HISTORY_SYNC = True
# Hours the device recorded replace the live statistics of the same hour by
# default (the log is the complete record; the live pushes are samples of
# it, and the drive's last hour would otherwise keep only the minutes after
# the car got home). True = the old rule: an hour Home Assistant already
# has is never touched.
CONF_HISTORY_PREFER_LIVE = "history_prefer_live"
DEFAULT_HISTORY_PREFER_LIVE = False
COMPONENT_DATA_LOGGER = "data_logger"
LOGGER_STATUS_PATH = "/api/logger"
LOGGER_EXPORT_PATH = "/api/logger/export"
FS_LIST_PATH = "/api/fs/list"
FS_DOWNLOAD_PATH = "/api/fs/download"
HISTORY_STORAGE_VERSION = 1
# Bounds: a healthy device is far below these; a glitching one must not be
# able to stall the event loop or balloon memory/statistics.
HISTORY_MAX_AGE_DAYS = 30  # never import rows older than this
HISTORY_FUTURE_SKEW = 300  # seconds of clock skew tolerated into the future
HISTORY_MAX_ROWS_PER_SYNC = 200_000
HISTORY_MAX_FILE_BYTES = 8 * 1024 * 1024
HISTORY_MAX_FILES_PER_SYNC = 48
HISTORY_MAX_PIDS_PER_SYNC = 200
HISTORY_EXPORT_PAGE_ROWS = 2000  # rows requested per export page (the firmware's cap)
HISTORY_EXPORT_MAX_PAGES = 200
HISTORY_EXPORT_PAGE_BYTES = 2 * 1024 * 1024  # size cap per export page
# The firmware stamps its rows in epoch milliseconds (issue #984); a stamp
# past this is read as milliseconds, anything below as seconds.
HISTORY_TS_MS_THRESHOLD = 100_000_000_000
# Follow-up runs: rows of the hour still running are imported this many
# seconds past the top of the next hour (the device's last batch of the
# hour lands up to its flush interval later); a device with more than one
# run could take is read again after this delay, at most this many times
# in a row.
HISTORY_FOLLOWUP_HOUR_MARGIN = 90
HISTORY_FOLLOWUP_MORE_DELAY = 60
HISTORY_MAX_FOLLOWUPS = 20

# The GPS track (2026-11-06): the device serves the recorded fix as a GPX
# document (GET /api/logger/gpx); the save_gpx service writes it under
# <config>/www/wican/ so it can be fetched as /local/wican/<file>.
LOGGER_GPX_PATH = "/api/logger/gpx"
# the device's clock journal (firmware issue #984): every set or step of its
# clock with the log files that were open; rows written before a step carry
# the old clock and are re-based with it (bounded on the device to ~96 KB)
CLOCK_JOURNAL_PATH = "/sd/logs/clock.jsonl"
# the GPS track card (a Lovelace custom card shipped with the integration,
# served from custom_components/wican/www and registered as a frontend
# resource at setup, so no manual resource entry is needed)
FRONTEND_STATIC_URL = "/wican-static"
GPX_CARD_FILE = "wican-gpx-card.js"
DATA_FRONTEND_REGISTERED = "wican_frontend_registered"
CLOCK_JOURNAL_MAX_BYTES = 256 * 1024
CLOCK_JOURNAL_BOOT_SLACK_MS = 5000
GPX_PAGE_BYTES = 4 * 1024 * 1024
GPX_PAGE_POINTS = 4000
GPX_MAX_PAGES = 10
GPX_DIR = "wican"
SERVICE_SAVE_GPX = "save_gpx"
ATTR_HOURS = "hours"
DEFAULT_GPX_HOURS = 24
MAX_GPX_HOURS = 24 * 30
HISTORY_SYNC_MIN_INTERVAL = 300  # seconds between sync runs per entry

# Remote device catalog: declarative device-type definitions pulled from
# GitHub (like params.json), so a new MeatPi product gets first-class
# support without an integration release. Design: notes/device-contract/DEVICE_CATALOG.md.
# Living document: https://github.com/meatpiHQ/meatpi-devices
DEVICE_CATALOG_URL = (
    "https://raw.githubusercontent.com/meatpiHQ/meatpi-devices/main/"
    "device_catalog.json"
)
CATALOG_SCHEMA_VERSION = 1
CATALOG_STORAGE_KEY = f"{DOMAIN}.device_catalog"
CATALOG_STORAGE_VERSION = 1
CATALOG_REFRESH_INTERVAL = 86400  # seconds between remote fetch attempts
CATALOG_FETCH_TIMEOUT = 15  # seconds
CATALOG_MAX_BYTES = 262144  # 256 KiB fetch cap
# Bounds on catalog content — a compromised or corrupt catalog must not be
# able to balloon the registry or entity set.
CATALOG_MAX_DEVICE_TYPES = 64
CATALOG_MAX_KEYWORDS = 8
CATALOG_MAX_KEYWORD_LENGTH = 32
CATALOG_MAX_MODEL_LENGTH = 64
CATALOG_MAX_SENSORS = 32
CATALOG_MAX_FIELD_LENGTH = 128

# PID parameter definitions (params.json). Fetched from the wican-fw
# vehicle-profiles pipeline on every entry setup/reload (the "reload the
# integration to get your new parameter" support flow), coalesced across
# concurrent setups, persisted in .storage; the bundled file is the
# offline fallback.
PARAMS_STORAGE_KEY = f"{DOMAIN}.params"
PARAMS_STORAGE_VERSION = 1
PARAMS_FETCH_DEDUPE_WINDOW = 30  # seconds; folds concurrent setups into one fetch
PARAMS_MAX_BYTES = 1048576  # 1 MiB fetch cap (file is ~55 KiB today)
PARAMS_MAX_ENTRIES = 5000
PARAMS_MAX_KEY_LENGTH = 128
PARAMS_MAX_FIELD_LENGTH = 256

# Webhook pushes may arrive gzip-compressed (LTE data saving). aiohttp
# inflates transparently but only bounds the *compressed* size, so the
# handler reads at most this many decompressed bytes — a compression bomb
# stops inflating at the cap instead of ballooning memory. A healthy
# payload is a few KiB.
MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024

# Cap on distinct top-level payload keys retained by the coordinator: a
# healthy device sends a handful (status, autopid_data, config, gps, ...);
# a buggy firmware emitting rotating keys must not grow memory forever.
MAX_COORDINATOR_KEYS = 32

# Device API levels (capability probe result)
API_LEVEL_LEGACY = 0  # webhook-only firmware (pre-V6)
API_LEVEL_V6 = 6  # full /api/* HTTP surface

# Dispatcher signal fired when a capability probe finishes; suffixed with
# the entry_id so platforms of other entries never react.
SIGNAL_CAPABILITIES_UPDATED = f"{DOMAIN}_capabilities_updated"

# GPS / Location Tracking
GPS_ACCURACY_THRESHOLD = 200  # meters - filter out low accuracy GPS fixes
MIN_GPS_LATITUDE = -90.0
MAX_GPS_LATITUDE = 90.0
MIN_GPS_LONGITUDE = -180.0
MAX_GPS_LONGITUDE = 180.0

# GitHub API
GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
GITHUB_OWNER = "meatpiHQ"
GITHUB_REPO = "wican-fw"

# Firmware Update
FIRMWARE_DOWNLOAD_TIMEOUT = 120  # 2 minutes to download from GitHub
FIRMWARE_UPLOAD_TIMEOUT = 180  # 3 minutes to upload to device
GITHUB_API_TIMEOUT = 30  # seconds for GitHub API requests
FIRMWARE_UPDATE_REBOOT_DELAY = 2  # seconds to wait before refreshing after update
OTA_V6_ENDPOINT = "/api/ota/upload"  # contract v2: raw octet-stream body
OTA_LEGACY_ENDPOINT = "/upload/ota.bin"  # migration bridge for pre-v5 firmware
OTA_FORM_FIELD = "ota_file"

# Update Coordinator
GITHUB_RELEASES_UPDATE_INTERVAL = 3600  # 1 hour

# WiCAN Data Coordinator (push-based fallback polling)
WICAN_DATA_UPDATE_INTERVAL = 300  # seconds (5 minutes)

# Device availability: mark entities unavailable when the device has not pushed
# data within max(post_interval * DEVICE_STALE_FACTOR, MIN_DEVICE_STALE_SECONDS).
DEVICE_STALE_FACTOR = 5
MIN_DEVICE_STALE_SECONDS = 120

# What the entities that carry a device reading (status and PID sensors, the
# location) show while the device is away. False: they go unavailable with
# the device. True: they keep their last received value, and the Connected
# and Last seen sensors say how current it is. Entities that act on the
# device or describe its live links follow the device either way.
CONF_KEEP_LAST_VALUES = "keep_last_values"
DEFAULT_KEEP_LAST_VALUES = False

# Key of the Last seen sensor. The coordinator reads that sensor's restored
# state at startup, so both sides must agree on its unique id.
LAST_SEEN_KEY = "last_seen"

# Bounds on device-reported data. A healthy device never exceeds these; a
# misbehaving one (corrupted memory, hostile caller) must not be able to grow
# the entity registry or the config entry without limit.
MAX_DYNAMIC_PID_SENSORS = 1000
MAX_PID_KEY_LENGTH = 128
MAX_PID_CONFIG_FIELD_LENGTH = 64

# A PID's value is computed on the device (B3*100/255): three decimals keep
# every reading a car gives and spare the state 13 digits of arithmetic.
PID_VALUE_DECIMALS = 3
MAX_DEVICE_INFO_FIELD_LENGTH = 255
