"""An offline AtonStorage portal, plugged into the integration's real httpx client.

It mimics what the real portal was seen doing: index.php redirects to login.php
before login, wrong credentials land back on the login form, the session lives
in a PHPSESSID cookie, a request without a valid session gets 401
"Unauthorized", and get_energy.php answers 429 when called again too soon.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
from custom_components.atonstorage.const import AVAILABLE_SENSORS, DOMAIN
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_MONITORED_VARIABLES,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

SERIAL = "R12AB34567S"
PLANT_ID = "123456789"
USERNAME = "user"
PASSWORD = "secret&pass="

LOGIN_PAGE = (
    '<form class="login-form" action="index.php" method="post">'
    '<input name="username"/><input type="password" name="password"/></form>'
)
# index.php before login: a 302 whose body is the empty page template.
TEMPLATE_PAGE = 'var sn = "";\nvar idImpianto = ;'
HOME_PAGE = (
    f'<script>var sn = "{SERIAL}";\nvar idImpianto = {PLANT_ID};\n'
    "var tipoUtente = 1;</script>"
)
MAINTENANCE_PAGE = "<html><h1>Sito in manutenzione</h1></html>"

# Battery series of get_energy: 24 slots discharging at 50 V, -6 A (0.1 kWh
# each), then 10 slots charging at 52 V, 15 A (0.26 kWh each), then the zero
# padding of slots still to come.
CHARGED_KWH = 2.6
DISCHARGED_KWH = 2.4


def _series(values: list) -> list:
    rows = []
    for slot in range(72):
        hour, minute = divmod(slot * 20, 60)
        value = values[slot] if slot < len(values) else "0.00"
        rows.append([f"{hour:02d}:{minute:02d}", value])
    return rows


def monitor_payload(**overrides) -> dict:
    data = {
        "serialNumber": SERIAL,
        "data": "30/10/2026 11:21:01",
        "status": "18",
        "statusMan": "22",
        "pSolare": "2363",
        "pUtenze": "449",
        "pUtenzeReal": "408",
        "pBatteria": "1914",
        "pRete": "0",
        "pReteReal": "41",
        "soc": "47",
        "socInv": "47",
        "socBms": "44",
        "socAh": "42",
        "vb": "51",
        "ib": "33.8",
        "gridV": "237.6",
        "gridHz": "49.99",
        "ePannelli": "4056",
        "eBatteria": "3589",
        "ahCaricati": "51",
        "ahScaricati": "65",
        "fwScheda": "11.90",
        "DiffDate": "44",
        "timestampScheda": "30/10/2026 11:20:37",
        "dataAllarme": "28/10/2026 23:03:34",
        "wifi": "1",
        "numBatterie": "2",
        "stato_EV": "-1",
        "num_EV": "-1",
        "setp_EV": "-1",
        "potenza_EV": "-1",
        "e_ciclo_EV": "-1",
        "SoC_EV": "0",
        "kmh": "0",
        "km": "0",
        "perc_carica": "0",
        "allarmi": [0] * 128,
    }
    data.update(overrides)
    return data


def energy_payload(**overrides) -> dict:
    data = {
        "tot_pSolare": "3.71",
        "tot_pUtenze": "4.05",
        "tot_pBatteria": "3.57",
        "tot_pBatteriaB": "2.50",
        "tot_pReteIn": "0.49",
        "tot_pReteOut": "0.09",
        "vb": _series(["50"] * 24 + ["52"] * 10),
        "ib": _series(["-6"] * 24 + ["15"] * 10),
    }
    data.update(overrides)
    return data


class FakePortal:
    """Callable for httpx.MockTransport. Tweak the attributes to misbehave."""

    def __init__(self) -> None:
        self.password = PASSWORD
        self.calls: list[str] = []
        self.monitor = monitor_payload()
        self.energy = energy_payload()
        # Raw bodies, when set they replace the payloads above.
        self.monitor_body: str | None = None
        self.energy_body: str | None = None
        # Page returned by a login POST, whatever the credentials.
        self.login_page: str | None = None
        self.energy_status = 200
        # Refuse get_energy with 429 within this many seconds of the last one.
        self.energy_window = 0.0
        self._last_energy = float("-inf")
        self._sessions: set[str] = set()

    def count(self, name: str) -> int:
        return self.calls.count(name)

    def expire_sessions(self) -> None:
        self._sessions.clear()

    def _session_ok(self, request: httpx.Request) -> bool:
        cookie = request.headers.get("cookie", "")
        return any(f"PHPSESSID={token}" in cookie for token in self._sessions)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(name)

        if name == "index.php" and request.method == "GET":
            return httpx.Response(
                302,
                text=TEMPLATE_PAGE,
                headers={"location": "login.php", "set-cookie": "PHPSESSID=anon"},
            )
        if name == "login.php":
            return httpx.Response(200, text=LOGIN_PAGE)
        if name == "index.php":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if self.login_page is not None:
                return httpx.Response(200, text=self.login_page)
            if (
                form.get("username") != USERNAME
                or form.get("password") != self.password
            ):
                return httpx.Response(302, headers={"location": "login.php"})
            token = f"s{len(self._sessions) + len(self.calls)}"
            self._sessions.add(token)
            return httpx.Response(
                200, text=HOME_PAGE, headers={"set-cookie": f"PHPSESSID={token}"}
            )

        if not self._session_ok(request):
            return httpx.Response(401, text="Unauthorized")
        if name == "set_request.php":
            # Not empty: the integration has always rejected an empty body here,
            # so the real portal must answer with something.
            return httpx.Response(200, text="ok")
        if name == "get_monitor.php":
            body = self.monitor_body
            return httpx.Response(200, text=body or json.dumps(self.monitor))
        if name == "get_energy.php":
            now = time.monotonic()
            if now - self._last_energy < self.energy_window:
                return httpx.Response(429, text="Too Many Requests")
            self._last_energy = now
            if self.energy_status != 200:
                return httpx.Response(self.energy_status, text="Too Many Requests")
            return httpx.Response(200, text=self.energy_body or json.dumps(self.energy))
        if name == "getAlarmDesc.php":
            return httpx.Response(
                200, json=[f"{i + 1:03d} - ALARM" for i in range(128)]
            )
        return httpx.Response(404, text="Not Found")


def make_entry(hass, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL.lower(),
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_ID: SERIAL,
            CONF_SCAN_INTERVAL: 30,
            CONF_MONITORED_VARIABLES: AVAILABLE_SENSORS,
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry
