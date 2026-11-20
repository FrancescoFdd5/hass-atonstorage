"""The integration running in Home Assistant against the fake portal."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from unittest.mock import patch

from custom_components.atonstorage.const import (
    AVAILABLE_SENSORS,
    CONF_BATTERY_RESERVE,
    DOMAIN,
)
from fake_portal import (
    CHARGED_KWH,
    DISCHARGED_KWH,
    MAINTENANCE_PAGE,
    PASSWORD,
    SERIAL,
    USERNAME,
    make_entry,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_MONITORED_VARIABLES,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

POWER = "sensor.user_instant_solar_power"
CHARGED = "sensor.user_battery_charged_energy"
DISCHARGED = "sensor.user_battery_discharged_energy"
BOUGHT = "sensor.user_daily_bought_energy"


async def setup(hass, **data):
    entry = make_entry(hass, **data)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def advance(hass, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def state(hass, entity_id: str) -> str:
    return hass.states.get(entity_id).state


def unavailable(hass) -> list[str]:
    return [
        s.entity_id for s in hass.states.async_all() if s.state == STATE_UNAVAILABLE
    ]


async def test_setup_refreshes_once(hass, mock_portal, freezer):
    """update_before_add used to trigger two more full refreshes."""
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    await advance(hass, freezer, 12)
    assert mock_portal.count("get_monitor.php") == 1
    assert mock_portal.count("get_energy.php") == 1


async def test_energy_polled_every_five_minutes(hass, mock_portal, freezer):
    await setup(hass)
    for _ in range(9):
        await advance(hass, freezer, 30)
    assert mock_portal.count("get_monitor.php") == 10
    assert mock_portal.count("get_energy.php") == 1
    await advance(hass, freezer, 30)
    assert mock_portal.count("get_energy.php") == 2


async def test_rate_limit_does_not_make_entities_unavailable(
    hass, mock_portal, freezer
):
    """A 429 from get_energy used to fail the refresh, and every entity with it."""
    mock_portal.energy_window = 3600
    await setup(hass)
    assert not unavailable(hass)

    mock_portal.energy["tot_pReteIn"] = "0.75"
    await advance(hass, freezer, 301)
    assert not unavailable(hass)
    # The refused fetch keeps the previous totals.
    assert state(hass, BOUGHT) == "0.49"


async def test_first_energy_fetch_failing_still_sets_up(hass, mock_portal, freezer):
    mock_portal.energy_status = 429
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, POWER) == "2363"
    assert state(hass, BOUGHT) == STATE_UNKNOWN

    # Retried on the next scan, not after five minutes.
    mock_portal.energy_status = 200
    await advance(hass, freezer, 30)
    assert state(hass, BOUGHT) == "0.49"


async def test_add_integration_under_rate_limit(hass, mock_portal):
    """The flow's check and the setup right after it fetch energy twice."""
    mock_portal.energy_window = 3600
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_DEVICE_ID: SERIAL,
            CONF_SCAN_INTERVAL: 30,
            CONF_MONITORED_VARIABLES: AVAILABLE_SENSORS,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == "create_entry"
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.state is ConfigEntryState.LOADED


async def test_battery_energy_from_voltage_and_current(hass, mock_portal):
    """tot_pBatteria (3.57 here) is self-consumption, not battery charge."""
    await setup(hass)
    assert float(state(hass, CHARGED)) == CHARGED_KWH
    assert float(state(hass, DISCHARGED)) == DISCHARGED_KWH
    assert state(hass, "sensor.user_daily_self_consumed_energy") == "3.589"


async def test_daily_counter_never_runs_backwards(hass, mock_portal, freezer):
    await setup(hass)
    mock_portal.energy["tot_pReteIn"] = "0.10"
    await advance(hass, freezer, 301)
    assert state(hass, BOUGHT) == "0.49"
    mock_portal.energy["tot_pReteIn"] = "0.60"
    await advance(hass, freezer, 301)
    assert state(hass, BOUGHT) == "0.6"


async def test_midnight_reset_goes_through(hass, mock_portal, freezer):
    # Two minutes before midnight in Home Assistant's own time zone.
    freezer.move_to(dt_util.now().replace(hour=23, minute=58, second=0))
    await setup(hass)
    assert state(hass, BOUGHT) == "0.49"
    mock_portal.energy["tot_pReteIn"] = "0.01"
    await advance(hass, freezer, 180)
    assert state(hass, BOUGHT) == "0.01"


async def test_maintenance_page_keeps_polling(hass, mock_portal, freezer):
    """Used to start a reauth flow, which stopped polling for good."""
    await setup(hass)
    mock_portal.expire_sessions()
    mock_portal.login_page = MAINTENANCE_PAGE
    await advance(hass, freezer, 30)
    assert not hass.config_entries.flow.async_progress()

    mock_portal.login_page = None
    before = mock_portal.count("get_monitor.php")
    await advance(hass, freezer, 30)
    assert mock_portal.count("get_monitor.php") > before
    assert not unavailable(hass)


async def test_wrong_password_starts_reauth(hass, mock_portal, freezer):
    await setup(hass)
    mock_portal.expire_sessions()
    mock_portal.password = "changed"
    await advance(hass, freezer, 30)
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_session_expiry_logs_in_again(hass, mock_portal, freezer):
    await setup(hass)
    mock_portal.expire_sessions()
    mock_portal.monitor["pSolare"] = "1000"
    await advance(hass, freezer, 30)
    assert state(hass, POWER) == "1000"
    assert mock_portal.count("index.php") == 4


async def _reauth(hass, entry, password):
    reloads = []
    real = hass.config_entries.async_reload

    async def counting(entry_id):
        reloads.append(entry_id)
        return await real(entry_id)

    with patch.object(hass.config_entries, "async_reload", counting):
        entry.async_start_reauth(hass)
        await hass.async_block_till_done()
        (flow,) = hass.config_entries.flow.async_progress()
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_PASSWORD: password}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    return result, reloads


async def test_reauth_reloads_once(hass, mock_portal):
    entry = await setup(hass)
    mock_portal.password = "new"
    result, reloads = await _reauth(hass, entry, "new")
    assert result["reason"] == "reauth_successful"
    assert len(reloads) == 1
    assert entry.data[CONF_PASSWORD] == "new"


async def test_reauth_with_same_password_still_reloads(hass, mock_portal):
    """Nothing changes, so no update listener fires: polling must still resume."""
    entry = await setup(hass)
    result, reloads = await _reauth(hass, entry, PASSWORD)
    assert result["reason"] == "reauth_successful"
    assert len(reloads) == 1


async def test_unload_really_closes_the_client(hass, mock_portal, caplog):
    entry = await setup(hass)
    client = hass.data[DOMAIN][entry.entry_id]["controller"]._async_client
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert client.is_closed
    assert "closes the Home Assistant httpx client" not in caplog.text


async def test_no_per_request_cookies(hass, mock_portal, recwarn):
    await setup(hass)
    assert not [w for w in recwarn if "per-request cookies" in str(w.message)]


async def test_stale_plant_hides_instant_values(hass, mock_portal, freezer):
    await setup(hass)
    mock_portal.monitor["DiffDate"] = "86400"
    await advance(hass, freezer, 30)
    assert state(hass, POWER) == STATE_UNAVAILABLE
    assert state(hass, "binary_sensor.user_solar_to_house") == STATE_UNAVAILABLE
    # Daily counters keep their value.
    assert state(hass, "sensor.user_daily_solar_energy") == "4.056"


async def test_no_wallbox_reports_unknown(hass, mock_portal):
    """Without a wallbox every EV field reads -1, e.g. a -1 W charge power."""
    await setup(hass)
    assert state(hass, "sensor.user_ev_charge") == STATE_UNKNOWN
    assert state(hass, "sensor.user_ev_setp") == STATE_UNKNOWN
    assert state(hass, "binary_sensor.user_ev_off") == STATE_UNKNOWN


async def test_wallbox_values(hass, mock_portal):
    mock_portal.monitor.update(stato_EV="35", potenza_EV="3500", num_EV="1")
    await setup(hass)
    assert state(hass, "sensor.user_ev_charge") == "3500"
    assert state(hass, "binary_sensor.user_ev_charging") == "on"


async def test_bad_fields_become_unknown(hass, mock_portal, freezer, caplog):
    await setup(hass)
    caplog.clear()
    mock_portal.monitor.update(soc="", gridV="n/a", data="", pRete="12.5")
    with caplog.at_level(logging.WARNING):
        await advance(hass, freezer, 30)
    assert state(hass, "sensor.user_battery_level") == STATE_UNKNOWN
    assert state(hass, "sensor.user_grid_voltage") == STATE_UNKNOWN
    assert state(hass, "sensor.user_last_update") == STATE_UNKNOWN
    assert state(hass, POWER) == "2363"
    # Used to raise in every affected entity on each poll.
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and (
            record.name.startswith("custom_components.atonstorage")
            or "updating listener" in record.getMessage()
        )
    ]


async def test_self_sufficiency_never_negative(hass, mock_portal):
    mock_portal.energy.update(tot_pReteIn="12", tot_pUtenze="9", tot_pBatteria="0")
    await setup(hass)
    assert state(hass, "sensor.user_self_sufficiency") == "0.0"


async def test_new_sensors(hass, mock_portal):
    await setup(hass)
    assert state(hass, "sensor.user_battery_level_usable") == "34"
    assert state(hass, "sensor.user_instant_grid_power_real") == "41"
    assert state(hass, "sensor.user_instant_user_power_real") == "408"


async def test_battery_reserve_option(hass, mock_portal):
    entry = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 30,
            CONF_MONITORED_VARIABLES: AVAILABLE_SENSORS,
            CONF_BATTERY_RESERVE: 30,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    # (47 - 30) / 70 = 24.3%
    assert state(hass, "sensor.user_battery_level_usable") == "24"


async def _user_flow(hass, **fields):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_SCAN_INTERVAL: 30,
            CONF_MONITORED_VARIABLES: AVAILABLE_SENSORS,
            **fields,
        },
    )


async def test_flow_reads_the_serial_from_the_portal(hass, mock_portal):
    result = await _user_flow(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] == "create_entry"
    assert result["data"][CONF_DEVICE_ID] == SERIAL
    assert result["result"].unique_id == SERIAL.lower()


async def test_flow_explains_a_wrong_serial(hass, mock_portal):
    mock_portal.monitor_body = "null"
    result = await _user_flow(hass, **{CONF_DEVICE_ID: "1234567890123456"})
    assert result["errors"] == {CONF_DEVICE_ID: "invalid_serial"}


async def test_flow_wrong_password(hass, mock_portal):
    result = await _user_flow(hass, **{CONF_PASSWORD: "nope"})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_flow_maintenance_is_not_a_wrong_password(hass, mock_portal):
    mock_portal.login_page = MAINTENANCE_PAGE
    result = await _user_flow(hass)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_energy_url_carries_the_serial(hass, mock_portal):
    seen = []
    real = mock_portal.__class__.__call__

    def spy(self, request):
        if request.url.path.endswith("get_energy.php"):
            seen.append(dict(request.url.params))
        return real(self, request)

    with patch.object(mock_portal.__class__, "__call__", spy):
        await setup(hass)
    assert seen and seen[0]["sn"] == SERIAL


def test_payloads_are_json():
    """Guard against a typo in the fixtures silently changing every test."""
    from fake_portal import energy_payload, monitor_payload

    json.dumps(monitor_payload())
    json.dumps(energy_payload())
