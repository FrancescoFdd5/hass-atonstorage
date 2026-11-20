# AtonStorage Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/wilds/hass-atonstorage.svg)](https://GitHub.com/wilds/hass-atonstorage/releases/)
![Installation Count](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.atonstorage.total)

This integration polls your AtonStorage inverter through the AtonStorage
cloud portal and splits the values it fetches into separate Home Assistant
sensors and binary sensors: battery state, instantaneous power flows, daily
energy counters, grid and string measurements, EV wallbox data (when present)
and alarm status.

## Requirements

- Home Assistant **2025.8.0** or newer
- Internet access: the integration is cloud-polling (`atonstorage.com`), the
  inverter does not need to be reachable from your LAN

## Installation

1. Install using [HACS](https://github.com/custom-components/hacs)
   (*Apps & integrations* → search for **AtonStorage**), or install manually by
   copying the `custom_components/atonstorage` folder into
   `<config_dir>/custom_components`
2. Restart Home Assistant
3. In the Home Assistant UI, navigate to `Settings` → `Devices & Services` and
   click **Add integration**. Select **AtonStorage** and fill in:
   - **Username** — your AtonStorage portal username
   - **Password** — your AtonStorage portal password
   - **Serial number** — optional: leave it empty to use the plant of
     your account. Otherwise enter the plant serial shown in the
     AtonStorage app or portal, not the 16-digit number on the inverter
     label
   - **Scan interval (seconds)** — how often to poll the portal (10–3600,
     default 30)
   - **Sensors to create** — which sensors to expose; tick **BINARY SENSORS**
     to add the binary sensors

If the portal rejects the stored password at any point, Home Assistant asks
you to enter it again (re-authentication flow) instead of failing silently.

## Options

After setup you can change the scan interval, the sensor selection and the
battery reserve from `Settings` → `Devices & Services` → *AtonStorage* →
**Options**. Changes are applied immediately — no restart needed.

- **Battery reserve** (default 20%) — the share of the battery the Aton app
  hides to protect it: the app shows 0% when the plant reports about 20%. Only
  the *Battery level usable* sensor uses it; *Battery level* keeps the real
  value.

Instant values (power, voltages, battery level, ...) are read on every scan.
The daily energy totals of the portal change only every 20 minutes, so they
are fetched every 5 minutes; if the portal refuses a request (it rate limits
them), the previous totals are kept and nothing goes unavailable.

## Sensors

All sensors are grouped under a single device named after the username.
The available categories:

| Category | Sensors |
| --- | --- |
| Battery | level, usable level (as in the Aton app), voltage, current, charged/discharged current, charged/discharged energy, BMS / inverter / Ah state of charge |
| Instant power | solar, user, battery, grid (plus grid and battery input/output split, and unfiltered grid and user power) |
| Daily energy | bought, sold, solar, consumed, self-consumed |
| Grid & utilities | voltage, frequency, current, utilities voltage/current |
| Solar strings | voltage and current for string 1 and 2 |
| Temperature | inverter temperature, temperature 2 |
| Derived | self sufficiency (%) |
| EV (wallbox plants only) | EV num, battery level, charge current, charge power, km/h, charged energy, km, charged percentage |
| Per phase (three-phase plants) | phase 1/2/3 power, grid phase 1/2/3 power |
| Alarms & diagnostics | active alarm count (with alarm names), last alarm, last update, board time, firmware versions, export limit |

Notes:

- EV sensors only report data when the plant has a wallbox; on other plants
  they show *unknown*
- Per-phase sensors read 0 on single-phase plants
- Daily counters reset to 0 at midnight, as reported by the plant, and never
  go backwards during the day, so a bad reply cannot inflate the statistics
- Battery charged/discharged energy is the energy that went into and out of
  the battery, from its voltage and current. Up to 1.0.10 they reported the
  portal's `tot_pBatteria`/`tot_pBatteriaB`, which are the energy the plant
  supplied to the house, not battery charge. The entities are the same, so the
  history continues; update right after midnight to avoid a one-off jump in
  that day's statistics
- When the plant has not reported for over 70 minutes (the portal's own
  offline threshold), instant values become *unavailable* instead of showing
  the last reading as current; daily counters keep their value
- *Instant grid power real* and *Instant user power real* are the unfiltered
  readings: the plain ones read 0 W on small flows. Positive grid power is
  export

## Binary sensors

Enabled by ticking **BINARY SENSORS** during setup or in the options:

- Energy flows: grid→house, solar→battery, solar→grid, battery→house,
  solar→house, grid→battery, battery→grid
- EV status: off, on, charging, warning
- Connectivity: Wi-Fi
- Problems: alarm active

## Development

```sh
pip install -r requirements-dev.txt
pytest                      # runs Home Assistant against an offline fake portal
pre-commit run --all-files  # black + flake8
```

## License

[MIT](LICENSE)
