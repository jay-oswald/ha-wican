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
| `binary_sensor` | Connected | Whether the device is currently sending data. Always available: it turns off when the device goes quiet (asleep, out of Wi-Fi range) and back on with the next push. |
| `sensor` | Last seen | When the device last sent data. Always available, so you can tell how old the other readings are. |
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
intervals, and become available again on the next push. Both are logged once at
info level, not as errors. A repair issue is raised when Home Assistant cannot
reach the device to register its webhook, the device is not sending data either,
and it does not already hold the webhook settings it would be given (a new
device, a changed Post Interval or Home Assistant address); it closes by itself
with the next push. A device that holds its settings and sleeps through Home
Assistant's start is logged at info level only: it pushes when it wakes. So is
a device an earlier version of this integration registered, asleep at the first
start after the upgrade: it holds that version's settings, pushes as it did
before, and is given this version's when it next pushes.

If you would rather keep seeing the last readings while the device is away, turn
on **Keep the last values while the device is offline** for that device (see
[Configuration parameters](#configuration-parameters-options)):

| While the device is away | Option off (default) | Option on |
|---|---|---|
| Battery Voltage, the other status sensors, PID sensors | Unavailable | Last received value |
| Location | Unavailable | Last known location |
| ECU Online, Bluetooth Enabled | Unavailable | Unavailable |
| Restart, Sync time | Unavailable | Unavailable |
| Connected | Off | Off |
| Last seen | Time of the last push | Time of the last push |

The same applies after a restart of Home Assistant: a device that went quiet
before or during the restart starts out as away, not as freshly connected.

With the option on, a held value is the last one received, not a current one. A
car that drives out of Wi-Fi range keeps showing the speed and RPM of that
moment until it is back, so use **Connected** or **Last seen** in automations
and dashboards that need to know whether a reading is live.

Two behaviors help devices on the road:

- **Compressed pushes:** the webhook accepts `Content-Encoding: gzip` bodies,
  so firmware can cut LTE data use substantially once it compresses payloads.
- **VPN backup endpoint:** if the device reports a connected WireGuard or
  Tailscale tunnel (`vpn_ip` in its status), Home Assistant uses that address
  as a backup when the local address stops answering — control buttons and
  history sync keep working while the car is away, provided Home Assistant
  can route into the VPN network.

### When Home Assistant cannot reach the device

A car that is asleep when Home Assistant starts, or one on LTE without a VPN,
cannot be reached by Home Assistant, but it reaches Home Assistant with every
push. The webhook addresses and the **Post Interval** then travel the other way:

- **Firmware that reads push replies** (WiCAN-Pro `6.0.0-beta.11` and newer):
  Home Assistant puts the settings into its reply to the device's own push. A
  changed Post Interval arrives with the next push, wherever the car is.
- **Older firmware:** Home Assistant registers again when the device pushes, at
  most once every five minutes, so the settings arrive once the device can be
  reached.

A device takes settings from a reply only where the connection can be trusted:
over HTTPS to a host name, or to a private address on a network that is not
marked untrusted on the device. Otherwise it keeps what it has until it is home
again. A device whose webhook is pinned on the device itself (**Manual URL
override** on its *Home Assistant* card) keeps its own settings, and Home
Assistant sends it none.

Settings changed on the device itself (its web interface) are put back to the
ones from Home Assistant with the next push, unless the webhook is pinned.

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
| **Show the recorded drive on the History page too** | on | on/off | On: the values the device recorded while it was away are also written as states of that time, so the History page and each sensor's graph show the drive (see [Store and forward](#store-and-forward-history-catch-up-for-offline-drives)). It works on the Home Assistant versions it was tested with and switches itself off on others; the statistics are imported either way. Off: statistics only. |
| **History detail: seconds between two points of one sensor** | 1 s | 0 to 3600 s | How dense the recorded drive is drawn on the History page: a sensor gets a point when its value changed, and not sooner than this many seconds after its last point. 0 = every change the device recorded. A smaller number costs database space and load time, a larger one can skip a short peak (see [History detail](#history-detail-how-dense-the-drive-is-drawn)). The statistics are not affected. |
| **Keep the last values while the device is offline** | off | on/off | Off: the sensors and the location show as unavailable once the device stops sending data. On: they keep their last received value, and the Connected and Last seen sensors tell how current it is (see [How data updates work](#how-data-updates-work)). Set per device; takes effect immediately and nothing is sent to the device, so it can be changed while the car is asleep. |

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

#### Home Assistant with SSL on its own web server
With `ssl_certificate` and `ssl_key` under `http:` in `configuration.yaml`, Home
Assistant answers over HTTPS only, so every webhook URL is an https one:

- The first URL is the **Local network URL** (*Settings > System > Network*) when
  it is an https address, otherwise Home Assistant's own address by IP
  (`https://<ip>:8123`). The second URL is the **Internet URL**, when it differs.
- A WiCAN-Pro pushes over HTTPS. It checks the certificate against its built-in
  list of public certificate authorities (Let's Encrypt and others); for an
  address by IP it does not compare the name on the certificate. A Local network
  URL by name has to be a name the certificate carries.
- A self-signed or private certificate has to be made known to the device: upload
  its CA certificate as a certificate set (*System > Certificates* on the device)
  and select that set on the device's *Home Assistant* card (*Automate*).
- WiCAN OBD-II and WiCAN USB cannot push over HTTPS. For them, keep Home
  Assistant's own web server on plain http and let a reverse proxy serve HTTPS
  (for example the NGINX Home Assistant SSL proxy add-on). Otherwise a repair
  issue tells you that the device cannot deliver its data.

## Store and forward (history catch-up for offline drives)

A vehicle spends most of its life away from your WiFi. With **Store and
forward** switched on in the device's web interface (Automate > Home
Assistant; it sets the data logger to JSON lines with every AutoPID sample,
and it is unavailable without a usable SD card), the device keeps recording
its vehicle parameters while it is away. When it reconnects, this
integration notices the gap and pulls the recorded rows from the device's
own read cursor (`GET /api/logger/export`), within seconds of the first
push. It does two things with them:

- it imports them into each sensor's **long-term statistics** at the hours
  they were recorded (the long-term graphs, a statistics graph card, the
  energy and statistics views), and
- it writes the values of the time the device was away as **states** of
  that time, so the **History** page and each sensor's own graph show the
  drive.

While it reads, the device holds off its sleep countdown a minute at a
time (ten minutes at most), so the pull finishes before the car goes to
sleep.

What to expect:

- **Statistics** appear at **hourly resolution** (mean/min/max per hour).
  This matches Home Assistant's permanent history layer: even
  live-recorded data is reduced to hourly statistics after the recorder
  retention window (default 10 days).
- **History** shows the drive as soon as the car is home: a point whenever
  a value changed, at most one a second per sensor unless the
  option **History detail** says otherwise (0 = every change, see
  [History detail](#history-detail-how-dense-the-drive-is-drawn)), in the unit
  the sensor is shown in. Home Assistant has no official way to write a
  state of the past, so the integration writes into the recorder's own
  table. It does that only on the Home Assistant versions it was tested
  with (recorder database version 53: Home Assistant 2026.2, 2026.10 and
  2026.11);
  on another version it writes no state, logs one line saying so, and the
  statistics are imported as before. A newer Home Assistant is approved
  through the device catalog once it has been tested, without an update
  of the integration. The option **Show the recorded drive on the History
  page too** turns it off. The logbook stays empty for the away time and
  automations are never triggered by a value of the past.
- **Units.** The device logs in its own unit (°C, km/h, kPa). The
  integration converts every value into the unit Home Assistant keeps
  that sensor in (°F, mph, psi on a US system) before it writes it. A
  sensor whose two units Home Assistant cannot convert between is not
  filled at all (the diagnostics name it). Versions before 2.0.0-beta2
  wrote the logged number as it came: hours imported by them into a
  sensor shown in another unit are wrong by the conversion. After the
  update the integration reads the device's log again from its start,
  once, and imports the hours it still holds again in the right unit
  (only for a device that has such a sensor; a long log takes several
  runs and may continue at the next drive). Hours the device no longer
  holds stay as they are: delete them under Developer tools > Statistics
  if they matter.
- **The battery voltage.** A firmware from 6.0.0-beta.15 on records the
  device's own battery voltage with the drive; it fills the Battery
  Voltage sensor the same way.
- **The last hour of a drive.** Home Assistant writes each sensor's row of
  an hour itself, ten seconds after that hour has ended, and loses the
  hour of every sensor when a row is there already. So an hour is
  imported only once it has ended: the hour the car came home in follows
  90 seconds after the next full hour, by itself. The integration keeps
  the figures of that hour from the pull at the return, so it is imported
  then even when the device has gone to sleep meanwhile, and imported
  again from the device's own rows at its next wake. A drive too long for
  one run continues a minute later. The recorded hours replace the live
  statistics of the same hour by default, because the log is the complete
  record of it and the live pushes are samples of it (the option **Keep
  the live statistics of hours the device also recorded** restores the old
  rule). Re-runs are idempotent.
- With **Keep the last values while the device is offline** on, leave **Keep
  the live statistics of hours the device also recorded** off (the default).
  A held value keeps producing statistics while the device is away, so every
  hour of a drive already has live statistics by the time the device is back,
  and the catch-up would skip them all.
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
imported, the hours imported from the kept copy, the cursor, the
follow-up, the device's logger state, and under `states` what the History
fill did: whether it is active on this Home Assistant, the recorder
database version, the states written, the spacing asked for and used, or
the reason it wrote none).

#### History detail: how dense the drive is drawn

The option **History detail: seconds between two points of one sensor**
sets how many of the recorded values become points on the History page. A
sensor gets a point when its recorded value changed, and not sooner than
the set number of seconds after its last point.

- **0 is the minimum: every change the device recorded.** There is nothing
  finer to show: the device records a value each time it reads it from the
  car (Store and forward records every reading; the logger's own setting
  **Record parameters** can be set to changes only), and Home Assistant
  itself keeps no second state of a value that did not change. A point
  carries the moment the device read the value, to the millisecond. How
  often a value is read is set by the device's poll table, not by this
  option. On the test bench a table of 30 parameters gave each of them a
  reading every 7.8 seconds; a table of 4 parameters polled every 100 ms
  gave each a reading every 0.14 seconds, and History then held a point
  for every change, some of them a fraction of a second apart.
- **1 (the default)** is the same as 0 for every value the device reads
  once a second or less often, which is every usual poll table, and holds
  a value that is read several times a second to one point a second.
- **The statistics are never thinned.** The hourly mean, min and max count
  every recorded value, whatever is set here. With a spacing above 0 the
  highest point of the History line can therefore be lower than the max of
  the statistics of that hour: the peak lay between two points.

What a choice costs, measured on a Raspberry Pi with Home Assistant's
default SQLite database (2026-11-22): a point takes about 110 bytes of the
database, the recorder writes about 8,500 points a second, and when the
History page asks for a sensor with 50,000 points Home Assistant answers
in 0.2 to 0.5 seconds with 2 MB for the browser to draw (it drew them).
The points of a drive are purged with the rest of the recorder's states
(after 10 days unless `purge_keep_days` says otherwise), so the cost does
not add up over the months.

| Set to | A sensor that changes every second, per hour of driving | What you see |
|---|---|---|
| 0 | 3,600 points, 0.4 MB | every change; the History line and the statistics agree |
| 1 (the default) | 3,600 points, 0.4 MB | the same for this sensor; a value read several times a second is thinned to one point a second |
| 5 | 720 points, 0.08 MB | a peak shorter than 5 s can be missing |
| 10 | 360 points, 0.04 MB | a peak shorter than 10 s can be missing |
| 60 | 60 points, 0.007 MB | the course of the drive, no detail |

A sensor that is read every 8 seconds has 450 readings an hour at most, so
for it every setting up to 8 is the same as 0.

Two simulated drives on the test bench, all sensors together, per hour of
driving:

| The device's table | Readings logged | Points at 0 | at 1 | at 5 | at 10 | at 60 |
|---|---|---|---|---|---|---|
| 30 parameters, each read every 7.8 s | 14,300 | 3,400 (0.38 MB) | 3,400 | 3,400 | 2,350 (0.26 MB) | 810 |
| 4 parameters, each read every 0.14 s | 104,800 | 7,200 (0.79 MB) | 4,600 (0.51 MB) | 1,670 | 1,210 (0.13 MB) | 315 |

The simulated values of the second drive changed twice a second at most.
A real signal read seven times a second can be another value at every
reading: at 0 that table then gives up to 104,800 points an hour (11.5 MB),
and the limit of one catch-up below is reached after half an hour of
driving. The default of 1 holds that table to 14,400 points an hour
(1.6 MB); 0 is for a table like that only when you want the full detail
of short drives.

Two limits hold whatever is set:

- **One catch-up writes 50,000 points at most** (all sensors together; the
  recorder is busy for about 6 seconds with that many on a Raspberry Pi,
  and Home Assistant's own states wait that long). A catch-up that would
  write more widens its spacing for that one run (1 s, then 2 s, 4 s and
  so on) until it fits, instead of cutting the drive off in time, and says
  so in the log ("written with a spacing of ..."). The diagnostics carry
  the spacing asked for (`step`), the one used (`step_used`) and the
  number of points the asked spacing came to (`states_at_step`).
- **A change applies to the drives read after it.** A drive that is in
  History already is not written again with another spacing.

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
  cannot be changed from Home Assistant alone. A unit the device spells its own
  way is shown in Home Assistant's spelling (`degC` as `°C`, `volts` as `V`,
  `grams/sec` as `g/s`). A device class that Home Assistant does not accept
  with the PID's unit is left out and the unit is kept.
- **PID values keep three decimals.** The device computes a value from the
  bytes of the car's answer; `25.0980392156863` is shown as `25.098`. The
  position of a GPS fix keeps every digit.
- **One device per config entry.** Each WiCAN adapter is added separately.
- **Webhook reachability is required.** The device must be able to reach Home
  Assistant's webhook URL; if it cannot, entities will not update and a repair
  issue is raised.
- **WiCAN OBD-II and WiCAN USB need a plain http address.** They cannot push to
  a Home Assistant whose own web server has SSL on (see
  [Webhook URL behavior](#webhook-url-behavior)).
- Changing the vehicle/PID configuration on the device may add or remove PIDs;
  stale entities can be removed manually (see Troubleshooting).

## Troubleshooting

### Cannot add a device via IP address or mDNS/hostname
The WiCAN device might not be reachable, or its protocol is not set to AutoPID.
1. Confirm the device is reachable from your web browser. If not, make sure it is
   not in sleep mode ([Sleep Mode docs](https://meatpihq.github.io/wican-fw/config/sleep-mode)).
2. Confirm the device protocol is set to **AutoPID** in its settings.

### The device is added, but all entities show "Unavailable"
The device is not sending data (car away, sleep mode); **Last seen** shows when
it last did. The entities come back by themselves with the next push, for
example when the ignition is turned on. If they stay unavailable while the
device is awake, reload the integration so that it registers its webhook on the
device again.

To see the last readings instead of "Unavailable" while the device is away, turn
on **Keep the last values while the device is offline** in the integration's
**Configure** dialog.

If Home Assistant's own web server has SSL on, also check the device's *Home
Assistant* card (*Automate*). A `failed` status there usually means that the
device does not trust Home Assistant's certificate, or that the Local network URL
is a name the certificate does not carry; the device log then shows
`mbedtls_ssl_handshake returned -0x2700`. See
[Webhook URL behavior](#webhook-url-behavior).

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
entities. The device is told to stop sending data:

- A device on firmware V6 that Home Assistant can reach at that moment has its
  webhook cleared, when it still points to this entry.
- A device that is asleep or away then is told by the answer to its next post.
  The webhook of a removed entry answers `410 Gone`, and firmware 6.0.0-beta.11
  or later clears its webhook on that answer. If no MeatPi device is left in
  Home Assistant and Home Assistant was restarted since, this starts again when
  the device is discovered on the network.

These keep sending data:

- a device on firmware 4.x, or on a V6 beta before 6.0.0-beta.11 that could not
  be reached (its own status shows the posts as failed from then on),
- a device whose webhook is pinned on the device (Manual URL override).

For these, open the WiCAN device's web UI and disable its webhook (Settings →
Services → Automation → Webhooks).

## For developers

The integration is a generic MeatPi device framework: device types are
declarative profiles, telemetry is webhook push on every firmware, and
control/discovery uses the device HTTP API (firmware V6+) when present.

- Architecture and design decisions: [notes/MEATPI_INTEGRATION_PLAN.md](notes/MEATPI_INTEGRATION_PLAN.md)
- Remote device catalog (new products without releases): [notes/device-contract/DEVICE_CATALOG.md](notes/device-contract/DEVICE_CATALOG.md)
- Adding a new MeatPi device (integration + firmware contract): [notes/device-contract/ADDING_A_DEVICE.md](notes/device-contract/ADDING_A_DEVICE.md)
- Requested firmware API changes: [notes/device-contract/FIRMWARE_API_FEEDBACK.md](notes/device-contract/FIRMWARE_API_FEEDBACK.md)
- Device HTTP API (firmware V6): [notes/device-contract/HTTP_API.md](notes/device-contract/HTTP_API.md)
