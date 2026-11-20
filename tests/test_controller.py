"""Controller logic on canned payloads, without Home Assistant or network."""

from __future__ import annotations

import pytest
from custom_components.atonstorage.const import CONF_BATTERY_RESERVE
from custom_components.atonstorage.controller import (
    _PLANT_ID_RE,
    _SERIAL_RE,
    Controller,
)
from fake_portal import (
    CHARGED_KWH,
    DISCHARGED_KWH,
    HOME_PAGE,
    PLANT_ID,
    SERIAL,
    TEMPLATE_PAGE,
    energy_payload,
    monitor_payload,
)


def make(monitor=None, energy=None, **opts):
    controller = Controller.__new__(Controller)
    controller.monitor_data = monitor_payload() if monitor is None else monitor
    controller.energy_data = energy_payload() if energy is None else energy
    controller._opts = opts
    controller._daily_max = {}
    controller._alarm_descriptions = []
    return controller


def test_plant_id_and_serial_from_the_home_page():
    assert _PLANT_ID_RE.search(HOME_PAGE).group(1) == PLANT_ID
    assert _SERIAL_RE.search(HOME_PAGE).group(1) == SERIAL


def test_empty_template_has_no_plant_id():
    """index.php serves this before login: it must not pass for a login."""
    assert _PLANT_ID_RE.search(TEMPLATE_PAGE) is None
    assert _SERIAL_RE.search(TEMPLATE_PAGE) is None


def test_battery_flows():
    controller = make()
    assert controller.battery_energy_charged == CHARGED_KWH
    assert controller.battery_energy_discharged == DISCHARGED_KWH
    # What the old sensor reported as charge.
    assert controller.self_consumed_energy == 3.57


def test_battery_flows_without_series():
    controller = make(energy={"tot_pBatteria": "3.57"})
    assert controller.battery_energy_charged is None


def test_daily_total_keeps_the_maximum():
    controller = make()
    assert controller.bought_energy == 0.49
    controller.energy_data = energy_payload(tot_pReteIn="0.20")
    assert controller.bought_energy == 0.49
    controller._daily_max = {}
    assert controller.bought_energy == 0.20


@pytest.mark.parametrize(
    "own,bought,expected",
    [("3.57", "0.49", 87.93), ("0", "12", 0.0), ("0", "0", 100.0), ("5", "0", 100.0)],
)
def test_self_sufficiency(own, bought, expected):
    controller = make(energy=energy_payload(tot_pBatteria=own, tot_pReteIn=bought))
    assert controller.self_sufficiency == expected


@pytest.mark.parametrize(
    "soc,expected",
    [("22", 3), ("20", 0), ("18", 0), ("47", 34), ("100", 100), ("", None)],
)
def test_usable_soc_matches_the_app(soc, expected):
    """App reading given by a user: 3% while the plant reported 22%."""
    assert make(monitor_payload(soc=soc)).usable_soc == expected


def test_usable_soc_custom_reserve():
    controller = make(monitor_payload(soc="47"), **{CONF_BATTERY_RESERVE: 10})
    assert controller.usable_soc == 41


@pytest.mark.parametrize(
    "value,off,on,charge,warning",
    [
        (0x00, True, False, False, False),
        (0x11, False, True, False, False),
        (0x12, False, True, False, False),
        (0x13, False, True, False, False),
        (0x14, False, False, False, False),
        (0x15, True, False, False, False),
        (0x23, False, False, True, False),
        (0x31, False, False, True, False),
        (0x40, False, False, False, True),
        (0x51, False, False, False, True),
    ],
)
def test_ev_status_as_the_portal_shows_it(value, off, on, charge, warning):
    controller = make(monitor_payload(stato_EV=str(value)))
    assert controller.has_wallbox
    assert (
        controller.ev_status_off,
        controller.ev_status_on,
        controller.ev_status_charge,
        controller.ev_status_warning,
    ) == (off, on, charge, warning)


def test_no_wallbox():
    assert not make(monitor_payload(stato_EV="-1")).has_wallbox
    assert not make(monitor_payload(stato_EV=None)).has_wallbox


@pytest.mark.parametrize(
    "diff,stale", [("44", False), ("4200", False), ("4201", True), (None, False)]
)
def test_stale(diff, stale):
    assert make(monitor_payload(DiffDate=diff)).is_stale is stale
