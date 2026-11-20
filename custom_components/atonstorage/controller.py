"""AtonStorage controller"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.util import dt as dt_util

from .const import CONF_BATTERY_RESERVE, DEFAULT_BATTERY_RESERVE

_BASEURL = "https://www.atonstorage.com/atonTC/"
_LOGIN_ENDPOINT = _BASEURL + "index.php"
_MONITOR_ENDPOINT = _BASEURL + "get_monitor.php?sn={serial_number}"
# sn is what the portal's own page sends; without it plants with no EV also get
# the empty EV series, about a quarter more bytes for the same totals.
_ENERGY_ENDPOINT = (
    _BASEURL
    + "get_energy.php?idImpianto={id}&anno={year}&mese={month}&giorno={day}"
    + "&intervallo=d&sn={serial_number}"
)
_SET_REQUEST_ENDPOINT = (
    _BASEURL
    + "set_request.php?request=MONITOR&intervallo={interval}&sn={serial_number}"
)
_ALARM_DESC_ENDPOINT = _BASEURL + "getAlarmDesc.php?sn={serial_number}"
# _ENDPOINT = "https://www.atonstorage.com/atonTC/get_monitor.php?sn={serialNumber}&_={timestamp}"
# https://www.atonstorage.com/atonTC/set_request.php?sn={serialNumber}&request=MONITOR&intervallo=15&_={timestamp}
# https://www.atonstorage.com/atonTC/getAlarmDesc.php?sn={serialNumber}&_={timestamp}
# https://www.atonstorage.com/atonTC/hasExternalEV.php?id_impianto=151762966&_={timestamp}
# https://www.atonstorage.com/atonTC/get_monitorToday.php?&sn={serialNumber}&_={timestamp}
# https://www.atonstorage.com/atonTC/get_energy.php?anno=2022&mese=11&giorno=9&idImpianto=151762966&intervallo=d&potNom=3500&batNom=3500&sn={serialNumber}&_={timestamp}
# https://www.atonstorage.com/atonTC/get_vbib.php?anno=2022&mese=11&sn={serialNumber}&_={timestamp}
# https://www.atonstorage.com/atonTC/get_allarmi_oggi.php?sn={serialNumber}&idImpianto=151762966&tipoUtente=1&_={timestamp}
# https://www.atonstorage.com/atonTC/checkTShift.php?sn={serialNumber}&_={timestamp}
# https://www.atonstorage.com/atonTC/getTShift.php?sn={serialNumber}&_={timestamp}

# Per request timeout. A full refresh performs three of these calls, so it has
# to stay well below the coordinator's own timeout.
REQUEST_TIMEOUT = 30

# get_energy.php answers HTTP 429 when called again within about two seconds
# and only recomputes its totals every 20 minutes, so it is polled far less
# often than the monitor, whose instant values refresh every scan.
ENERGY_INTERVAL = timedelta(minutes=5)

# Seconds without news from the plant after which the portal itself shows it
# as offline; past this the instant values are no longer current.
STALE_AFTER = 4200

# Numeric only: the template served before login carries "var idImpianto = ;".
_PLANT_ID_RE = re.compile(r"var idImpianto\s*=\s*(\d+)\s*;")
# The page served after login carries the plant serial.
_SERIAL_RE = re.compile(r'var sn\s*=\s*"([^"]+)"')
# Wrong credentials land back on the login form.
_LOGIN_FORM = 'name="password"'
_UNAUTHORIZED = b"Unauthorized"

_LOGGER = logging.getLogger(__name__)


class Controller:
    """Define a generic AtonStorage sensor."""

    def __init__(self, hass: HomeAssistant, user, password, serial_number, opts):
        """Initialize."""

        if not user or not password:
            raise UsernameAndPasswordRequiredError

        self._hass = hass

        self._user = user
        self._password = password
        # Empty means: use the serial the portal shows after login.
        self._serial_number = (serial_number or "").strip() or None
        self._plant_id = None
        self._opts = opts
        self._logged_in = False

        # A private client, so the PHP session cookie is not shared with every
        # other httpx based integration, nor with a second AtonStorage account.
        # Its cookie jar holds the session. Closed again by async_close();
        # auto_cleanup is off because it would register one more stop listener
        # on every reload of the config entry.
        self._async_client = create_async_httpx_client(
            hass, auto_cleanup=False, follow_redirects=True
        )

        # data dicts
        self.monitor_data = None
        self.energy_data = None

        # get_energy is fetched every ENERGY_INTERVAL, not every scan.
        self._energy_day: date | None = None
        self._energy_fetched: datetime | None = None
        # Highest value of each daily counter seen today, see _keep_max().
        self._daily_max: dict[str, float] = {}

        # The 128 alarm descriptions are static, so they are fetched once
        # and kept; a failure here must never break a refresh.
        self._alarm_descriptions: list[str] = []
        self._alarm_descriptions_tried = False

    async def async_close(self) -> None:
        """Close the http client owned by this controller."""
        # Home Assistant swaps aclose() on the instance for a stub that only logs
        # a warning, to protect the clients it shares. This one is private, so
        # call the class method to really release its connections.
        await httpx.AsyncClient.aclose(self._async_client)

    async def login(self) -> bool:
        """Login to Aton server. False means the portal rejected the credentials."""

        # A stale PHPSESSID in the jar would otherwise be sent along.
        self._async_client.cookies.clear()
        self._logged_in = False
        try:
            await self._async_client.get(_LOGIN_ENDPOINT, timeout=REQUEST_TIMEOUT)
            login = await self._async_client.post(
                _LOGIN_ENDPOINT,
                timeout=REQUEST_TIMEOUT,
                # Pass a dict and let httpx url-encode it: a hand built body
                # breaks on passwords containing &, =, + or non ascii characters.
                data={"username": self._user, "password": self._password},
            )
        except httpx.HTTPError as err:
            raise AtonStorageConnectionError(f"Login request failed: {err}") from err

        if login.is_error:
            raise AtonStorageConnectionError(f"Login returned HTTP {login.status_code}")

        # The portal hands out a session cookie even when the credentials are
        # wrong, so the only reliable success marker is the plant id embedded in
        # the page that comes back.
        result = _PLANT_ID_RE.search(login.text)
        if result is None:
            # Rejected credentials redirect back to the login form. Any other
            # page (maintenance, an outage, a new step on the portal) says
            # nothing about the password: treating it as a rejection started a
            # reauth flow, which stops polling until the password is typed again.
            if login.url.path.endswith("/login.php") and _LOGIN_FORM in login.text:
                _LOGGER.debug("The portal rejected the credentials")
                return False
            raise AtonStorageConnectionError(
                f"Login landed on {login.url.path} without a plant id"
            )

        self._plant_id = result.group(1)
        if self._serial_number is None:
            serial = _SERIAL_RE.search(login.text)
            if serial is None:
                raise InvalidSerialNumberError(
                    "No serial number configured and none on the portal page"
                )
            self._serial_number = serial.group(1)
        self._logged_in = True
        _LOGGER.info("Logged in, plant id %s", self._plant_id)

        return True

    async def _async_get(self, url: str, description: str) -> httpx.Response:
        """Perform an authenticated GET, raising on anything that is not usable."""

        try:
            response = await self._async_client.get(url, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as err:
            raise AtonStorageConnectionError(f"{description} failed: {err}") from err

        # Note the b prefix: response.content is bytes, comparing it to a str
        # silently never matched.
        if (
            response.status_code in (401, 403)
            or response.content.strip() == _UNAUTHORIZED
        ):
            self._logged_in = False
            raise AtonStorageSessionExpiredError(f"{description} was refused")

        if response.is_error:
            raise AtonStorageConnectionError(
                f"{description} returned HTTP {response.status_code}"
            )

        if not response.content:
            raise AtonStorageConnectionError(f"{description} returned an empty body")

        return response

    async def _async_get_json(self, url: str, description: str) -> Any:
        """Perform an authenticated GET and decode the JSON payload."""

        response = await self._async_get(url, description)

        try:
            data = json.loads(response.content)
        except ValueError as err:
            # A body that is not JSON is almost always the HTML login page, i.e.
            # the PHP session expired. Drop the session so the caller can log in
            # again, instead of silently serving yesterday's values forever.
            self._logged_in = False
            _LOGGER.debug("Erroneous JSON for %s: %s", description, response.content)
            raise AtonStorageSessionExpiredError(
                f"{description} did not return JSON"
            ) from err

        _LOGGER.debug("Data fetched from resource: %s", response.content)

        return data

    async def refresh(self) -> None:
        """Refresh data from server, logging in again if the session expired."""

        for attempt in (1, 2):
            if not self._logged_in:
                if not await self.login():
                    raise InvalidUsernameOrPasswordError

            try:
                await self._async_refresh_once()
            except AtonStorageSessionExpiredError as err:
                if attempt == 2:
                    raise AtonStorageConnectionError(str(err)) from err
                _LOGGER.info("AtonStorage session expired, logging in again")
                continue

            return

    async def _async_refresh_once(self) -> None:
        """Run one full fetch cycle with the session currently held."""

        try:
            await self._async_get(
                _SET_REQUEST_ENDPOINT.format(
                    serial_number=self._serial_number,
                    interval=self._opts.get("interval", 15),
                ),
                "set_request",
            )
        except AtonStorageSessionExpiredError:
            raise
        except AtonStorageConnectionError as err:
            # It only asks the plant to keep streaming; the monitor may well be
            # current anyway, so this alone is no reason to lose a refresh.
            _LOGGER.debug("set_request failed: %s", err)

        monitor_data = await self._async_get_json(
            _MONITOR_ENDPOINT.format(serial_number=self._serial_number),
            "get_monitor",
        )
        # A wrong serial logs in fine but gets no plant back.
        if not isinstance(monitor_data, dict) or not monitor_data.get("serialNumber"):
            raise InvalidSerialNumberError(
                f"get_monitor returned no plant data for {self._serial_number}"
            )
        self.monitor_data = monitor_data

        await self._async_refresh_energy()

        # Optional extra, deliberately last: it can neither hold up the data
        # above nor leave a partial refresh behind if the portal misbehaves.
        if not self._alarm_descriptions_tried:
            await self._async_load_alarm_descriptions()

    def _energy_due(self, today: date) -> bool:
        """Whether get_energy has to be fetched again."""
        if self.energy_data is None or self._energy_day != today:
            return True
        return dt_util.utcnow() - self._energy_fetched >= ENERGY_INTERVAL

    async def _async_refresh_energy(self) -> None:
        """Fetch today's energy totals when due. Never fails the refresh."""

        # Read the clock once, in Home Assistant's configured timezone: three
        # separate now() calls can straddle midnight and build a date whose
        # year/month/day come from different days.
        now = dt_util.now()
        if not self._energy_due(now.date()):
            return

        try:
            energy_data = await self._async_get_json(
                _ENERGY_ENDPOINT.format(
                    id=self._plant_id,
                    year=now.year,
                    month=now.month,
                    day=now.day,
                    serial_number=self._serial_number,
                ),
                "get_energy",
            )
        except AtonStorageSessionExpiredError:
            raise
        except AtonStorageConnectionError as err:
            # Rate limited (HTTP 429) or briefly unavailable. Failing the whole
            # refresh made every entity unavailable; the totals only move every
            # 20 minutes, so keep the last ones and try again on the next scan.
            _LOGGER.debug("Keeping the previous energy totals: %s", err)
            return

        if not isinstance(energy_data, dict):
            _LOGGER.debug("get_energy returned no totals, keeping the previous ones")
            return

        if self._energy_day != now.date():
            self._daily_max = {}
        self.energy_data = energy_data
        self._energy_day = now.date()
        self._energy_fetched = dt_util.utcnow()

    async def _async_load_alarm_descriptions(self) -> None:
        """Fetch the alarm name table once. Best effort, never fatal."""
        self._alarm_descriptions_tried = True
        try:
            data = await self._async_get_json(
                _ALARM_DESC_ENDPOINT.format(serial_number=self._serial_number),
                "getAlarmDesc",
            )
        except Exception as err:  # noqa: BLE001
            # _async_get_json drops the session when a reply is not JSON.
            # The monitor call just succeeded with this one, so an optional
            # extra must not force a needless re-login.
            self._logged_in = True
            _LOGGER.debug("Could not fetch the alarm descriptions: %s", err)
            return

        if isinstance(data, list):
            self._alarm_descriptions = [str(item) for item in data]
            _LOGGER.debug("Loaded %d alarm descriptions", len(self._alarm_descriptions))

    @property
    def active_alarms(self) -> list[str]:
        """Names of the alarms currently raised.

        monitor_data["allarmi"] is a flag per alarm, positionally matching
        the getAlarmDesc table. Every flag was zero on the plant this was
        built against, so the mapping is inferred from the equal lengths.
        """
        raw = (self.monitor_data or {}).get("allarmi")
        if not isinstance(raw, list):
            return []

        names = []
        for index, value in enumerate(raw):
            try:
                if int(value) == 0:
                    continue
            except (TypeError, ValueError):
                continue
            if index < len(self._alarm_descriptions):
                names.append(self._alarm_descriptions[index])
            else:
                names.append(f"alarm {index + 1}")
        return names

    @property
    def active_alarm_count(self) -> int:
        return len(self.active_alarms)

    def get_raw_data(self, key: str):
        """Return a raw value from either payload, or None when unavailable."""

        if self.monitor_data and key in self.monitor_data:
            return self.monitor_data[key]

        if self.energy_data and key in self.energy_data:
            return self.energy_data[key]

        # Debug and not warning: a plant without an EV lacks a whole set of keys
        # and would log on every single poll.
        _LOGGER.debug("Key %s not found in monitor_data or energy_data", key)
        return None

    def _status_bit(self, mask: int) -> bool:
        """Return a single bit of the status bitfield."""
        return int(self.monitor_data.get("status") or 0) & mask == mask

    @property
    def grid_to_house(self) -> bool:
        return self._status_bit(1)

    @property
    def solar_to_battery(self) -> bool:
        return self._status_bit(2)

    @property
    def solar_to_grid(self) -> bool:
        return self._status_bit(4)

    @property
    def battery_to_house(self) -> bool:
        return self._status_bit(8)

    @property
    def solar_to_house(self) -> bool:
        return self._status_bit(16)

    @property
    def grid_to_battery(self) -> bool:
        return self._status_bit(32)

    @property
    def battery_to_grid(self) -> bool:
        return self._status_bit(64)

    @property
    def serial_number(self) -> str:
        return self.monitor_data["serialNumber"]

    @property
    def last_update(self) -> str:
        return self.monitor_data["data"]

    @property
    def status(self) -> str:
        return self.monitor_data["status"]

    @property
    def status_man(self) -> str:
        return self.monitor_data["statusMan"]

    @property
    def instant_solar_power(self) -> int:
        return int(self.monitor_data["pSolare"])

    @property
    def instant_user_power(self) -> int:
        return int(self.monitor_data["pUtenze"])

    @property
    def instant_user_power_real(self) -> int:
        return int(self.monitor_data["pUtenzeReal"])

    @property
    def instant_battery_power(self) -> int:
        return int(self.monitor_data["pBatteria"])

    @property
    def instant_grid_input_power(self) -> int:
        return int(self.monitor_data["pReteIn"])

    @property
    def instant_grid_output_power(self) -> int:
        return int(self.monitor_data["pReteOut"])

    @property
    def instant_grid_power(self) -> int:
        return int(self.monitor_data["pRete"])

    @property
    def instant_grid_power_real(self) -> int:
        return int(self.monitor_data["pReteReal"])

    @property
    def status_of_charge(self) -> float:
        return float(self.monitor_data["soc"])

    @property
    def run_mode(self) -> int:
        return int(self.monitor_data["runMode"])

    @property
    def string1_current(self) -> float:
        return float(self.monitor_data["string1I"])

    @property
    def string1_voltage(self) -> float:
        return float(self.monitor_data["string1V"])

    @property
    def string2_current(self) -> float:
        return float(self.monitor_data["string2I"])

    @property
    def string2_voltage(self) -> float:
        return float(self.monitor_data["string2V"])

    @property
    def user_current(self) -> float:
        return float(self.monitor_data["utenzeI"])

    @property
    def user_voltage(self) -> float:
        return float(self.monitor_data["utenzeV"])

    @property
    def battery_voltage(self) -> float:
        return float(self.monitor_data["vb"])

    @property
    def battery_current(self) -> float:
        return float(self.monitor_data["ib"])

    @property
    def fw_Scheda(self) -> str:
        return self.monitor_data["fwScheda"]

    @property
    def rel_inverter(self) -> str:
        return self.monitor_data["relInverter"]

    @property
    def rel_manager(self) -> str:
        return self.monitor_data["relManager"]

    @property
    def rel_charger(self) -> str:
        return self.monitor_data["relCharger"]

    @property
    def rel_bios(self) -> str:
        return self.monitor_data["relBIOS"]

    @property
    def battery_charged(self) -> int:
        return int(self.monitor_data["ahCaricati"])

    @property
    def battery_discharged(self) -> int:
        return int(self.monitor_data["ahScaricati"])

    def _keep_max(self, key: str, value: float) -> float:
        """Never let a daily counter run backwards before midnight.

        TOTAL_INCREASING reads any drop of more than 10% as a reset and books
        the next reading as new energy, so a single bad reply from the portal
        would add up to a whole day to the long-term statistics. The memory is
        cleared when the first totals of a new day arrive.
        """
        best = self._daily_max.get(key)
        if best is not None and value < best:
            return best
        self._daily_max[key] = value
        return value

    def _daily_total(self, key: str) -> float | None:
        """A get_energy daily total in kWh, or None when unavailable."""
        try:
            value = float(self.energy_data[key])
        except (TypeError, KeyError, ValueError):
            return None
        return self._keep_max(key, value)

    def _battery_flows(self) -> tuple[float, float] | None:
        """Energy into and out of the battery today, in kWh.

        tot_pBatteria is not battery charge: it equals tot_pUtenze minus
        tot_pReteIn, the energy the plant supplied to the house, and the portal
        labels it self-consumed energy. The vb and ib series hold the average
        battery voltage and current of each slot, so their product integrates
        to the energy that went in (positive) and came out (negative). Checked
        against full days on two plants: it matches the portal's cumulative
        battery series to the cent, and Ah counters times voltage.
        """
        data = self.energy_data if isinstance(self.energy_data, dict) else {}
        voltages, currents = data.get("vb"), data.get("ib")
        if not isinstance(voltages, list) or not isinstance(currents, list):
            return None
        if not voltages:
            return None
        # 72 slots of 20 minutes; slots still in the future read zero.
        hours = 24 / len(voltages)
        charged = discharged = 0.0
        for voltage, current in zip(voltages, currents):
            try:
                energy = float(voltage[1]) * float(current[1]) * hours / 1000
            except (TypeError, ValueError, IndexError):
                continue
            if energy > 0:
                charged += energy
            else:
                discharged -= energy
        return round(charged, 3), round(discharged, 3)

    @property
    def battery_energy_charged(self) -> float | None:
        flows = self._battery_flows()
        if flows is None:
            return None
        return self._keep_max("battery_charged", flows[0])

    @property
    def battery_energy_discharged(self) -> float | None:
        flows = self._battery_flows()
        if flows is None:
            return None
        return self._keep_max("battery_discharged", flows[1])

    @property
    def max_selled_power(self) -> int:
        return self.monitor_data["pMaxVenduta"]

    @property
    def max_pannel_power(self) -> int:
        return self.monitor_data["pMaxPannelli"]

    @property
    def max_battery_power(self) -> int:
        return self.monitor_data["pMaxBatteria"]

    @property
    def max_bought_power(self) -> int:
        return self.monitor_data["pMaxComprata"]

    @property
    def sold_energy(self) -> float | None:
        return self._daily_total("tot_pReteOut")

    @property
    def bought_energy(self) -> float | None:
        return self._daily_total("tot_pReteIn")

    @property
    def pannel_energy(self) -> int:
        return self.monitor_data["ePannelli"]

    @property
    def self_consumed_energy(self) -> float | None:
        """Energy the plant supplied to the house itself, solar plus battery.

        tot_pBatteria equals tot_pUtenze - tot_pReteIn to the cent. It is not
        battery charge energy, see _battery_flows().
        """
        return self._daily_total("tot_pBatteria")

    @property
    def consumed_energy(self) -> float | None:
        """Daily household consumption, as reported by the portal.

        Previously computed as bought + discharged, which ignored the solar
        consumed directly by the house and under-reported by roughly a factor
        of three (3.35 against a real 9.99 kWh on 2026-08-29).
        """
        return self._daily_total("tot_pUtenze")

    @property
    def self_sufficiency(self) -> float | None:
        """Share of the consumption the plant covered itself, in percent.

        The portal's own formula. 100 - bought / consumed went negative
        whenever the battery was charged from the grid.
        """
        own, bought = self.self_consumed_energy, self.bought_energy
        if own is None or bought is None:
            return None
        if own + bought <= 0:
            return 100.0
        return round(min(100.0, max(0.0, own * 100 / (own + bought))), 2)

    @property
    def usable_soc(self) -> int | None:
        """State of charge as the Aton app shows it, net of the hidden reserve.

        The app shows 0% at the reserve and 100% at full: a plant at 22% with
        the default 20% reserve shows 3%.
        """
        try:
            soc = float(self.monitor_data["soc"])
        except (TypeError, KeyError, ValueError):
            return None
        reserve = self._opts.get(CONF_BATTERY_RESERVE, DEFAULT_BATTERY_RESERVE)
        usable = min(100.0, max(0.0, (soc - reserve) * 100 / (100 - reserve)))
        # Half up like the app: 2.5 is shown as 3, where round() would give 2.
        return math.floor(usable + 0.5)

    @property
    def is_stale(self) -> bool:
        """The plant has been silent past the portal's own offline threshold."""
        try:
            return int((self.monitor_data or {}).get("DiffDate")) > STALE_AFTER
        except (TypeError, ValueError):
            return False

    @property
    def has_wallbox(self) -> bool:
        """An Axis.T is connected.

        Without one every EV field reads -1; the portal shows its EV panel only
        when stato_EV is above -1.
        """
        try:
            return int((self.monitor_data or {}).get("stato_EV")) > -1
        except (TypeError, ValueError):
            return False

    # "ingressi1": "0",
    # "ingressi2": "160",
    # "ingressi3": "0",
    # "ingressi4": "0",
    # "ingressi5": "0",
    # "ingressi6": "0",
    # "ingressi7": "0",
    # "ingressi8": "0",
    # "uscite1": "0",
    # "uscite2": "10",
    # "uscite3": "0",
    # "uscite4": "0",
    # "uscite5": "0",
    # "uscite6": "0",
    # "iac1": "0",
    # "iac2": "0",
    # "iac3": "0",
    # "allarmi1": "0",
    # "allarmi2": "0",
    # "allarmi3": "0",
    # "allarmi4": "0",
    # "allarmi5": "0",
    # "allarmi6": "0",
    # "allarmi7": "0",
    # "allarmi8": "0",
    # "allarmi9": "0",
    # "allarmi10": "0",
    # "allarmi11": "0",
    # "allarmi12": "32",
    # "allarmi13": "0",
    # "allarmi14": "0",
    # "allarmi15": "0",
    # "allarmi16": "0",

    @property
    def grid_voltage(self) -> float:
        return self.monitor_data["gridV"]

    @property
    def grid_frequency(self) -> float:
        return self.monitor_data["gridHz"]

    @property
    def grid_power(self) -> float:
        return self.monitor_data["pGrid"]

    # "string1IIN": "0",
    # "string1VIN": "0",
    # "string2IIN": "0",
    # "string2VIN": "0",

    @property
    def temperature(self) -> float:
        return self.monitor_data["temperatura"]

    @property
    def temperature2(self) -> float:
        return self.monitor_data["temperatura2"]

    # "dataAllarme": "07/11/2022 07:11:28",

    @property
    def update_delay(self) -> int:
        return self.monitor_data["DiffDate"]

    # "DiffDate": "829",
    # "timestampScheda": "07/11/2022 11:13:13",

    @property
    def vb_scheda(self) -> str:
        return self.monitor_data.get("vbScheda")

    # "flagProgrammazione": "128",
    # "flagProgrammazione3": "72",
    # "wifi": "1",
    # "exportLimit": "0",

    # "pL1": "0",
    # "pL2": "0",
    # "pL3": "0",
    # "pReteL1": "0",
    # "pReteL2": "0",
    # "pReteL3": "0",

    @property
    def ev_num(self) -> int:
        return int(self.monitor_data["num_EV"])

    @property
    def ev_status_of_charge(self) -> float:
        return float(self.monitor_data["SoC_EV"])

    @property
    def ev_status(self) -> int:
        return int(self.monitor_data["stato_EV"])

    # var firstNumber = (parseInt(_data.stato_EV)&0xf0)>>4;
    # var secondNumber = parseInt(_data.stato_EV)&0x0f;

    @property
    def _ev_status_high(self) -> int:
        """High nibble of stato_EV.

        Mind the parentheses: in Python >> binds tighter than &, so writing
        "x & 0xF0 >> 4" silently means "x & 0x0F" instead.
        """
        return (int(self.monitor_data.get("stato_EV") or 0) & 0xF0) >> 4

    @property
    def _ev_status_low(self) -> int:
        """Low nibble of stato_EV."""
        return int(self.monitor_data.get("stato_EV") or 0) & 0x0F

    # Decoded as the portal's own page does for an Axis.T: high nibble 0 is
    # off, 1 is on for low nibble 1-3 and paused for 4, 2 and 3 are charging,
    # 4 and 5 are an alarm. A pause is neither on nor off.

    @property
    def ev_status_off(self) -> bool:
        return self._ev_status_high == 0 or (
            self._ev_status_high == 1 and self._ev_status_low not in (1, 2, 3, 4)
        )

    @property
    def ev_status_on(self) -> bool:
        return self._ev_status_high == 1 and self._ev_status_low in (1, 2, 3)

    @property
    def ev_status_charge(self) -> bool:
        return self._ev_status_high in (2, 3)

    @property
    def ev_status_warning(self) -> bool:
        return self._ev_status_high in (4, 5)

    @property
    def ev_setp(self) -> float:
        return float(self.monitor_data["setp_EV"])  # in A

    @property
    def ev_power(self) -> int:
        return int(self.monitor_data["potenza_EV"])  # carica in W

    @property
    def ev_kmh(self) -> float:
        return float(self.monitor_data["kmh"])  # evCaricakmh km/h

    @property
    def ev_e_ciclo_(self) -> float:
        return float(self.monitor_data["e_ciclo_EV"])  # evScaricakWh

    @property
    def ev_km(self) -> float:
        return float(self.monitor_data["km"])  # evScaricakm km

    @property
    def ev_perc_carica(self) -> float:
        return float(self.monitor_data["perc_carica"])  # evCaricakmh %

    # "paese": "IT",
    # "scena": "0",
    # "qeps": "1",
    # "allertaMeteoAuto": "0",

    @property
    def battery_count(self) -> int:
        return self.monitor_data["numBatterie"]


class AtonStorageConnectionError(Exception):
    """Unable to start fetching data."""


class AtonStorageSessionExpiredError(AtonStorageConnectionError):
    """The server refused the session cookie, a new login is needed."""


class UsernameAndPasswordRequiredError(Exception):
    """Error username and password required."""


class InvalidUsernameOrPasswordError(Exception):
    """Error invalid username or password."""


class InvalidSerialNumberError(Exception):
    """The portal returned no plant data for the serial number."""
