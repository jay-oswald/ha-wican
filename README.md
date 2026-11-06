[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

# MeatPi for Home Assistant

This is the official Home Assistant integration for
[MeatPi](https://www.meatpi.com/) devices — **MeatPi is the brand; WiCAN is a
device**. Today it supports the WiCAN family of OBD-II CAN-bus adapters
([firmware](https://github.com/meatpiHQ/wican-fw)); it is built as a generic
multi-device integration so upcoming MeatPi products (such as **ESPNetlink**,
an LTE + GPS gateway) plug into the same integration.

WiCAN reads data from your vehicle and pushes it to Home Assistant over your
local network. The integration exposes that data as sensors (battery voltage,
and any OBD-II PIDs you have configured on the device), tracks connection
status, follows the vehicle's GPS location where supported, updates the device
firmware, and — on firmware V6+ — offers control buttons backed by the
device's HTTP API.

This integration is distributed via HACS and is not part of the default Home
Assistant integrations. (Its internal Home Assistant domain remains `wican`
for compatibility with existing installations; everything user-facing is
MeatPi.)

The documentation for the hardware itself (WiCAN OBD / WiCAN USB / WiCAN-Pro),
including how to wire it up, configure protocols, and set up per-vehicle PIDs,
lives in the official [WiCAN device documentation](https://meatpihq.github.io/wican-fw/).

## Supported devices

| Device | Supported | Notes |
|---|---|---|
| WiCAN OBD-II | ✅ | Connects to the vehicle's OBD-II port. |
| WiCAN-Pro | ✅ | Adds support for multiple webhook URLs on firmware `v4.49+` (local HTTP + external HTTPS); firmware V6 adds the control HTTP API. |
| WiCAN USB | ✅ | Same integration; requires network connectivity to Home Assistant. |
| ESPNetlink | 🔜 | LTE + GPS gateway; profile and product sensors (LTE signal/operator, GPS tracking) are already in place, awaiting the product firmware. |
| Future MeatPi devices | ✅ | Any device implementing the MeatPi device contract works immediately; full product support (model name, product sensors) arrives via the remote **device catalog** without an integration update (see `notes/device-contract/DEVICE_CATALOG.md`). |

WiCAN devices must be running firmware with the **AutoPID** protocol enabled
and be reachable from Home Assistant on the local network. Very old firmware
without a MAC address / device ID in its mDNS advertisement is still supported
but falls back to a hostname-based identifier. Devices on pre-V6 firmware keep
the full telemetry feature set; V6 additionally enables control entities.

## Supported functionality

The integration is a **local push** integration: the WiCAN device sends data to
a Home Assistant webhook on an interval you configure. It creates one device per
WiCAN adapter with the following entities:

| Platform | Entity | Description |
|---|---|---|
| `sensor` | Battery Voltage | Vehicle battery voltage reported by the device. |
| `sensor` | WiFi Mode, VPN Status, Uptime | Diagnostic sensors about the device itself. |
| `sensor` | Dynamic PID sensors | One sensor per OBD-II PID configured on the device (e.g. speed, coolant temperature, state of charge). Created automatically as data arrives. High-volume per-cell battery voltages are created **disabled by default**. |
| `binary_sensor` | ECU Online | Whether the vehicle ECU is currently responding. |
| `binary_sensor` | Bluetooth Enabled | Diagnostic BLE status. |
| `device_tracker` | Location | GPS location of the device/vehicle, where GPS data is available. |
| `update` | Firmware | Shows available WiCAN firmware from GitHub and installs it over the air. |
| `button` | Restart, Sync time | Control buttons backed by the device HTTP API. Created automatically on firmware V6+ devices (Sync time requires the RTC component); not shown on older firmware. |
| `button` | Refresh integration definitions | On every device: downloads the latest PID parameter definitions and device catalog from GitHub, then reloads the integration to apply them. Use it when a newly added parameter should show up immediately. It refreshes downloaded definition data only — it does not communicate with the device. |

Dynamic PID sensors are named from the raw OBD-II keys provided by the device,
because those keys are only known at runtime (see
[Known limitations](#known-limitations)).

## How data updates work

WiCAN does not poll the vehicle from Home Assistant. Instead, the device
**pushes** data to a Home Assistant webhook:

1. During setup the integration registers a webhook URL on the device.
2. The device POSTs its status and PID data to that URL on the configured
   **Post Interval** (default 15 seconds).
3. Entities update immediately when each push arrives.

If the device stops pushing (for example the car is parked and the adapter is in
sleep mode), the entities are marked **unavailable** after several missed
intervals, and become available again on the next push. A repair issue is raised
if Home Assistant cannot register its webhook on the device at all.

Two behaviors help devices on the road:

- **Compressed pushes:** the webhook accepts `Content-Encoding: gzip` bodies,
  so firmware can cut LTE data use substantially once it compresses payloads.
- **VPN backup endpoint:** if the device reports a connected WireGuard or
  Tailscale tunnel (`vpn_ip` in its status), Home Assistant uses that address
  as a backup when the local address stops answering — control buttons and
  history sync keep working while the car is away, provided Home Assistant
  can route into the VPN network.

## Installation

### Prerequisites
- A WiCAN device on the same network as Home Assistant, powered and reachable.
- The device protocol set to **AutoPID** (see the device documentation).
- [HACS](https://hacs.xyz/) installed in Home Assistant.

### Install via HACS
1. Add this repository to HACS as a custom repository
   ([guide](https://www.hacs.xyz/docs/faq/custom_repositories/)):
   - Repository URL: `https://github.com/meatpiHQ/ha-wican`
   - Type: `Integration`
2. Download the MeatPi (WiCAN) integration in HACS.
3. Restart Home Assistant.
4. Continue with **Configuration** below.

### Install via My Home Assistant
1. Use this link:
   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meatpiHQ&repository=ha-wican&category=integration)
2. Restart Home Assistant.
3. Continue with **Configuration** below.

## Configuration

WiCAN devices are usually **discovered automatically** over mDNS/Zeroconf — you
will see a discovered device prompt in *Settings → Devices & Services*. Use
manual setup only if discovery did not find your device.

### Installation parameters (manual setup)
When adding the integration manually you provide:

| Field | Required | Description |
|---|---|---|
| **Hostname or IP address** (`mdns`) | Yes | The device's mDNS hostname (e.g. `wican_xxxxxxxxxxxx.local`) or its IP address. |
| **Alternative IP** (`host`) | No | An optional fallback IP address to try if the hostname cannot be resolved. |

If you have multiple WiCAN devices, repeat the setup for each one.

### Configuration parameters (options)
After setup, use the integration's **Configure** button:

| Option | Default | Range | Description |
|---|---|---|---|
| **Post Interval** | 15 s | 1–3600 s | How often the device pushes data to Home Assistant. Lower values mean more frequent updates and more network traffic. |
| **Backfill history from the device log** | on | on/off | When the device reconnects after being away, import the AutoPID data it logged to its SD card into each sensor's long-term statistics at the recorded times (see [History backfill](#history-backfill-offline-drives)). |

You can also change the device address later with the **Reconfigure** option
without deleting and re-adding the integration.

### Webhook URL behavior
- WiCAN devices use a single local HTTP webhook URL.
- WiCAN-Pro devices on firmware `v4.49+` can receive multiple webhook URLs.
- When available, Home Assistant sends WiCAN-Pro devices a local HTTP webhook URL
  first and an external HTTPS webhook URL second (such as Nabu Casa or a reverse
  proxy).
- If no local HTTP Home Assistant URL is available, WiCAN-Pro `v4.49+` can fall
  back to a single external HTTPS webhook URL.

## Store and forward (history catch-up for offline drives)

A vehicle spends most of its life away from your WiFi. With **Store and
forward** switched on in the device's web interface (Automate > Home
Assistant; it sets the data logger to JSON lines with every AutoPID sample,
and it is unavailable without a usable SD card), the device keeps recording
its vehicle parameters while it is away. When it reconnects, this
integration notices the gap, pulls the recorded rows from the device's own
read cursor (`GET /api/logger/export`), and imports them into each PID
sensor's **long-term statistics** at the hours they were recorded, filling
the gap in the long-term graphs. While it reads, the device holds off its
sleep countdown a minute at a time (ten minutes at most), so the pull
finishes before the car goes to sleep.

What to expect:

- Backfilled data appears at **hourly resolution** (mean/min/max per
  hour). This matches Home Assistant's permanent history layer: even
  live-recorded data is reduced to hourly statistics after the recorder
  retention window (default 10 days).
- The fine-grained history detail view and logbook stay empty for the
  offline period (Home Assistant has no supported way to backfill raw
  states) and automations are never retro-triggered.
- The hour still running is imported once it has ended (a follow-up run
  past the top of the next hour); a drive too long for one run continues
  a minute later. The recorded hours replace the live statistics of the
  same hour by default, because the log is the complete record of it and
  the live pushes are samples of it (the option **Keep the live statistics
  of hours the device also recorded** restores the old rule). Re-runs are
  idempotent.
- Requirements: a firmware with the export route (V6; the 2026-11 build
  adds the sleep hold and the export counters), the params stream in
  `jsonl` or `csv` (Store and forward sets it; a `sqlite` or `binary`
  log is reported in the diagnostics and imports nothing), an SD card, and
  a valid device clock (SNTP or the RTC).

The sync runs automatically when the device reconnects after a gap and
shortly after Home Assistant starts; the **Catch up history** button on
the device page runs it on request; both can be turned off per device in
the integration options. The diagnostics download carries the last run
(`history_sync`: pages, rows fetched, used, deferred, hours and sensors
imported, the cursor, the follow-up, the device's logger state).

### The GPS track (GPX)

**The map card.** The integration ships a Lovelace card that draws a
device's recorded track on Home Assistant's own map (no resource entry
needed, the integration registers it):

```yaml
type: custom:wican-gpx-card
device_id: <the WiCAN device>   # or url: /local/wican/<file>.gpx
hours: 24                       # how far back (1..720)
title: GPS track
height: 320                     # px
```

With `device_id` the card calls `wican.save_gpx` when it is shown and
draws the file it saved (one path per track segment, a new segment
after ten minutes without a fix); with `url` it draws a file already
under `www/wican/`. The line under the map names the points and the
span, or says why there is nothing (no SD card log, a log not in JSON
lines or CSV, no fix in the window).

A position has no statistics and Home Assistant cannot write a past
location, so the GPS fix the device records (the `gps_*` parameters,
when a GPS receiver or an ESPNetLink is attached) is not backfilled onto
the Location tracker. Instead the service **`wican.save_gpx`** (fields:
the device, hours back, default 24) fetches the recorded fix from the
device as a GPX 1.1 track (`GET /api/logger/gpx`) and saves it under
`<config>/www/wican/`, where it is served as `/local/wican/<file>.gpx`;
the service response carries the path, the URL and the point count. The
device's Logger page offers the same download.

## Use cases

- **Battery health monitoring** — track resting battery voltage and get alerted
  before a flat battery leaves you stranded.
- **EV charging & range** — monitor state of charge, charging power, and range
  PIDs (on vehicles that expose them) to automate charging notifications.
- **Trip / location logging** — use the GPS `device_tracker` to record where the
  vehicle is and trigger zone-based automations (e.g. "car arrived home").
- **Engine diagnostics** — surface coolant temperature, RPM, fuel level and other
  PIDs on a dashboard.

## Automation examples

Notify when the battery voltage drops (possible parasitic drain):

```yaml
automation:
  - alias: "WiCAN low battery voltage"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.wican_battery_voltage
        below: 12.2
        for: "00:10:00"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "Vehicle battery low"
          message: "Battery voltage is {{ states('sensor.wican_battery_voltage') }} V"
```

Announce when the vehicle arrives home (GPS device tracker enters the Home zone):

```yaml
automation:
  - alias: "WiCAN arrived home"
    triggers:
      - trigger: zone
        entity_id: device_tracker.wican_location
        zone: zone.home
        event: enter
    actions:
      - action: notify.family
        data:
          message: "The car just arrived home."
```

## Known limitations

- **Dynamic PID sensor names** are taken from the raw OBD-II keys the device
  sends and therefore cannot be pre-translated. Rename them in Home Assistant if
  you prefer friendlier names.
- **Units are defined on the device.** To change a PID's unit of measurement,
  update it on the WiCAN device (Automate tab) and reload the integration; it
  cannot be changed from Home Assistant alone.
- **One device per config entry.** Each WiCAN adapter is added separately.
- **Webhook reachability is required.** The device must be able to reach Home
  Assistant's webhook URL; if it cannot, entities will not update and a repair
  issue is raised.
- Changing the vehicle/PID configuration on the device may add or remove PIDs;
  stale entities can be removed manually (see Troubleshooting).

## Troubleshooting

### Cannot add a device via IP address or mDNS/hostname
The WiCAN device might not be reachable, or its protocol is not set to AutoPID.
1. Confirm the device is reachable from your web browser. If not, make sure it is
   not in sleep mode ([Sleep Mode docs](https://meatpihq.github.io/wican-fw/config/sleep-mode)).
2. Confirm the device protocol is set to **AutoPID** in its settings.

### The device is added, but all entities show "Unavailable"
Home Assistant was restarted or the integration reloaded while the device was
unavailable (car away, sleep mode). Make the device available again (e.g. turn on
the ignition) and reload the integration.

### Firmware update fails
1. Confirm Home Assistant can reach the device on the local network.
2. Ensure the device is awake and not mid-update.
3. Retry from the Firmware update entity; check the logs for the specific error.

### Entities are not updated after changing the car configuration on the device
The integration creates entities from the device's PID configuration. After
changing it, either:
- delete the individual entities that no longer exist, or
- delete the WiCAN device in Home Assistant and add it again.

### The unit of measurement of an entity cannot be changed in Home Assistant
Units come from the device configuration. Open the device in a browser (e.g. via
the *Visit* link on the device page), go to the **Automate** tab, update the PID
`unit`, and press *Submit changes* (see the device
[Automate docs](https://meatpihq.github.io/wican-fw/config/automate/usage)). Then
reload the integration in Home Assistant.

## Removing the integration

This integration follows standard Home Assistant removal:

1. Go to *Settings → Devices & Services*.
2. Select the **MeatPi** integration, open the device's menu, and choose
   **Delete**.
3. Repeat for any additional devices.

Removing the config entry unregisters the Home Assistant webhook and removes all
entities. Optionally, open the WiCAN device's web UI and disable its webhook
(Settings → Services → Automation → Webhooks) so it stops sending data.

## For developers

The integration is a generic MeatPi device framework: device types are
declarative profiles, telemetry is webhook push on every firmware, and
control/discovery uses the device HTTP API (firmware V6+) when present.

- Architecture and design decisions: [notes/MEATPI_INTEGRATION_PLAN.md](notes/MEATPI_INTEGRATION_PLAN.md)
- Remote device catalog (new products without releases): [notes/device-contract/DEVICE_CATALOG.md](notes/device-contract/DEVICE_CATALOG.md)
- Adding a new MeatPi device (integration + firmware contract): [notes/device-contract/ADDING_A_DEVICE.md](notes/device-contract/ADDING_A_DEVICE.md)
- Requested firmware API changes: [notes/device-contract/FIRMWARE_API_FEEDBACK.md](notes/device-contract/FIRMWARE_API_FEEDBACK.md)
- Device HTTP API (firmware V6): [notes/device-contract/HTTP_API.md](notes/device-contract/HTTP_API.md)
