# AGENTS.md

Home Assistant custom integration (`custom_components/atonstorage`) for AtonStorage
solar+battery inverters. Cloud-polls the atonstorage.com PHP portal over httpx.
Single package, no build step, no frontend.

## Commands

- Tests: `pytest` (deps pinned in `requirements-dev.txt`; install with
  `pip install -r requirements-dev.txt`). `pytest-homeassistant-custom-component`
  pins the Home Assistant version the suite runs on. `pytest.ini` sets
  `testpaths=tests`, `pythonpath=. tests` and `asyncio_mode=auto`. Tests run
  offline: `tests/fake_portal.py` mimics the portal behind `httpx.MockTransport`
  (login redirects, session cookie, 401, 429) and the real controller talks to
  it; `test_sensor_entity.py` and `test_controller.py` feed canned payloads to a
  Controller built without `__init__`.
- The test plugin does not run on Windows as is (it imports `fcntl` and
  `resource`); use Linux, WSL or CI.
- Lint: `pre-commit run --all-files` (black + flake8). flake8 config:
  max-line-length 127, only `E9,F63,F7,F82` selected.
- Version bump: `version` in `custom_components/atonstorage/manifest.json` plus a
  `vX.Y.Z` git tag. `hacs.json` holds the *minimum HA version* (2025.8.0), not the
  release version.

## Architecture

- `controller.py` — `Controller`: every scan calls `set_request` →
  `get_monitor`; `get_energy` only every `ENERGY_INTERVAL` (5 min) and at the
  first scan of a new day; a one-time optional alarm table. `set_request` and
  `get_energy` failures (other than an expired session) never fail the refresh:
  the previous energy totals are kept. Owns one private httpx client per config
  entry, its cookie jar holds the session; re-logs in when the session expires.
  Close it with `httpx.AsyncClient.aclose(client)`: HA replaces the instance's
  `aclose` with a stub that only warns.
- `__init__.py` — config entry setup + `AtonStorageUpdateCoordinator` (pass
  `config_entry` explicitly). `UPDATE_TIMEOUT` (90 s) must stay above
  3 × `REQUEST_TIMEOUT` (30 s).
- `sensor.py` / `binary_sensor.py` — entity descriptions; each uses either
  `value_conversion_function` (raw payload key via `get_raw_data`) or
  `value_calc_function` (computed from the controller).
- `config_flow.py` — setup, reauth, and options flows (scan interval, sensor
  selection, battery reserve); changing options reloads the whole entry through
  the update listener. The serial is optional: when empty the one on the
  portal page is used and stored in the entry.
- Entities are selected by **display name** via `CONF_MONITORED_VARIABLES`
  (`AVAILABLE_SENSORS` in `const.py`); binary sensors are gated by the
  `"BINARY SENSORS"` option.
- Device identity: identifier `(DOMAIN, "AtonStorage " + username)`, device name =
  username, `sw_version` = `fw_Scheda`. New entities must reuse this.

## Portal / domain quirks

- Payloads are Italian (`pUtenze`, `ahCaricati`, `temperatura`, ...).
- Login success = numeric `var idImpianto = <digits>;` in the page after the
  POST, which also carries the serial in `var sn = "..."`. Wrong credentials
  redirect back to `/atonTC/login.php` with its password form: only that means
  "rejected" (reauth). Any other page without a plant ID (maintenance, outage)
  is a connection error, so polling goes on. Before login `index.php` is a 302
  whose body still contains `var idImpianto = ;`. A request without a valid
  session gets HTTP 401 `Unauthorized`; a non-JSON response also means the
  session expired → re-login.
- `get_energy.php` is rate limited: HTTP 429 when called again within about
  two seconds. Its `tot_*` only move every 20 minutes (about 3 minutes after
  each slot), by a whole slot at once.
- `tot_pBatteria` is NOT battery charge: it equals `tot_pUtenze - tot_pReteIn`
  (self-consumption, the portal's "Energia Auto Consumata"), same as the
  monitor's `eBatteria`. Battery charge/discharge come from the `vb` and `ib`
  slot series (`vb * ib`, positive in, negative out). The keys `tot_pBatteria`
  and `tot_pBatteriaB` stay as entity keys for the unique IDs.
- `status` is a bitfield (1=grid→house, 2=solar→battery, 4=solar→grid, 8=battery→house,
  16=solar→house, 32=grid→battery, 64=battery→grid). `allarmi` is positional
  against the 128-entry `getAlarmDesc` table.
- Daily counters (`tot_*`) are reset to 0 by the plant at midnight: declare them
  `TOTAL_INCREASING`, never `TOTAL` (no `last_reset` is published). The
  get_energy based ones go through `Controller._keep_max()` so they never drop
  before midnight.
- EV fields read -1 on plants without a wallbox (`stato_EV == -1`); EV
  entities set `requires_wallbox` and report unknown. They may also be absent.
- `energy_data` can be up to `ENERGY_INTERVAL` older than `monitor_data`, or
  older still while the portal refuses it; it is never replaced by a non-dict.
- `DiffDate` > `STALE_AFTER` (4200 s, the portal's offline threshold) makes
  MEASUREMENT sensors and the `instant` binary sensors unavailable.
- The energy endpoint date must be read once via `homeassistant.util.dt.now()`
  (HA timezone); separate `now()` calls can straddle midnight.

## Conventions

- Do not rename, remove, or re-unit existing entities — it would split their
  long-term statistics.
- Adding a sensor: add its display name to `AVAILABLE_SENSORS` (`const.py`) and
  matching keys to `strings.json` + `translations/en.json`. Translations use
  **literal strings** — `%key%` references are only resolved for core
  integrations.
- HA internal APIs are unstable: `IntegrationSensor.__init__` gained a required
  `hass` in 2025.8 and lost it again in 2026.8 (core PR #177596, not on the
  official breaking-changes list). When touching `homeassistant.components.*`
  internals, check the installed HA version.
- From 2026.11 HA aliases `httpx` to `httpx2` and `voluptuous` to `probatio`
  in its package `__init__`; plain `import httpx` / `import voluptuous` keep
  working.
- Untracked scratch files in the package dir (`controller copy.py`,
  `Untitled-1.p_y`) are not part of the integration — ignore them.
- The `lint` branch is a diverged experiment (removes `binary_sensor.py` and the
  tests); don't base work on it.
