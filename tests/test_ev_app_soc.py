"""App gauge SOC retains its timeline provenance and never replaces live SOC."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eon_next_optimise import ev_sensor, sensor
from custom_components.eon_next_optimise.const import (
    CONF_REFRESH_TOKEN,
    CONF_SITE_IDS,
    DOMAIN,
    MAX_AGES,
)

NOW = datetime(2026, 10, 31, 1, 35, tzinfo=timezone.utc)
VEHICLE = {"entity_id": "synthetic-private-vehicle", "make": "Tesla", "model": "Model 3"}


def interval(start=NOW, end=None, soc=69.7304, quality="current"):
    return {
        "valid_from": start.isoformat(),
        "valid_to": (end or start + timedelta(minutes=5)).isoformat(),
        "state_of_charge": soc,
        "quality": quality,
        "unrequested_private_field": "must-not-be-published",
    }


@pytest.fixture
def sources(monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock.now)

    def section(name, rows):
        return SimpleNamespace(
            entry=SimpleNamespace(entry_id="synthetic-entry"),
            site_id="synthetic-site",
            section=name,
            data={"vehicles": rows, "vehicle_count": len(rows)},
            last_update_success=True,
            last_success=NOW,
            max_age=MAX_AGES[name],
            async_add_listener=Mock(return_value=Mock()),
            async_request_refresh=AsyncMock(),
        )

    fleet = section("ev", [{**VEHICLE, "state_of_charge": 70}])
    timeline = {
        "query_success": True,
        "reported": True,
        "fetched_at": NOW.isoformat(),
        "intervals": [interval()],
    }
    plan = section("ev_plan", [{**VEHICLE, "timeline": timeline}])
    plan.fleet_coordinator = fleet
    entity = ev_sensor.OptimiseEVAppSocSensor(plan, "Optimise site 1", VEHICLE, 1)
    return SimpleNamespace(clock=clock, fleet=fleet, plan=plan, timeline=timeline, entity=entity)


def test_app_gauge_floor_preserves_raw_source_attributes_and_live_soc(sources):
    entity = sources.entity
    live = ev_sensor.OptimiseEVSensor(
        sources.fleet, "Optimise site 1", VEHICLE, 1, ev_sensor.EV_SENSORS[0]
    )
    assert live.native_value == 70
    assert live.unique_id.endswith(":soc")
    assert entity.native_value == 69
    assert entity.device_class == SensorDeviceClass.BATTERY
    assert entity.native_unit_of_measurement == "%"
    assert entity.state_class is None
    assert entity.entity_description.name == "Battery level shown in app"
    assert entity.unique_id.endswith(":app_soc")
    assert entity.unique_id != live.unique_id
    assert entity.extra_state_attributes == {
        "source": "ev_timeline",
        "raw_state_of_charge": 69.7304,
        "valid_from": NOW.isoformat(),
        "valid_to": (NOW + timedelta(minutes=5)).isoformat(),
        "quality": "current",
        "forecast_is_measurement": False,
        "display_rounding": "floor",
    }
    assert "private" not in str(entity.extra_state_attributes)
    assert "synthetic-private-vehicle" not in entity.unique_id
    assert sources.timeline["intervals"][0]["state_of_charge"] == 69.7304
    assert live.native_value == 70  # The companion never edits telemetry or source rows.


@pytest.mark.parametrize(
    "value,expected", [(0, 0), (100, 100), (69.9999, 69), (-0.2, 0), (100.2, 100)]
)
def test_app_gauge_matches_floor_then_clamp_including_zero(sources, value, expected):
    sources.timeline["intervals"][0]["state_of_charge"] = value
    assert sources.entity.available
    assert sources.entity.native_value == expected


@pytest.mark.parametrize(
    "value", [None, True, "69.7304", float("nan"), float("inf"), -float("inf")]
)
def test_missing_and_nonfinite_soc_never_becomes_zero_or_exposes_stale_attributes(sources, value):
    sources.timeline["intervals"][0]["state_of_charge"] = value
    assert not sources.entity.available
    assert sources.entity.native_value is None
    assert sources.entity.extra_state_attributes is None


@pytest.mark.parametrize(
    "failure",
    [
        "module_stale",
        "module_future",
        "module_failed",
        "unreported",
        "plan_stale",
        "plan_failed",
        "fleet_stale",
        "fleet_failed",
        "fleet_unknown",
        "vehicle_removed",
        "response_vehicle_missing",
    ],
)
def test_app_gauge_requires_fresh_module_section_and_current_fleet(sources, failure):
    assert sources.entity.native_value == 69
    if failure == "module_stale":
        sources.timeline["fetched_at"] = (NOW - sources.plan.max_age).isoformat()
    elif failure == "module_future":
        sources.timeline["fetched_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif failure == "module_failed":
        sources.timeline["query_success"] = False
    elif failure == "unreported":
        sources.timeline["reported"] = False
    elif failure == "plan_stale":
        sources.plan.last_success -= sources.plan.max_age
    elif failure == "plan_failed":
        sources.plan.last_update_success = False
    elif failure == "fleet_stale":
        sources.fleet.last_success -= sources.fleet.max_age
    elif failure == "fleet_failed":
        sources.fleet.last_update_success = False
    elif failure == "fleet_unknown":
        sources.fleet.data["vehicle_count"] = None
    elif failure == "vehicle_removed":
        sources.fleet.data.update(vehicles=[], vehicle_count=0)
    else:
        sources.plan.data.update(vehicles=[], vehicle_count=0)
    assert not sources.entity.available
    assert sources.entity.native_value is None
    assert sources.entity.extra_state_attributes is None


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [interval(start=NOW + timedelta(minutes=1))],
        [interval(start=NOW - timedelta(minutes=10))],
        [interval(quality="history")],
        [interval(quality=None)],
        [interval(), interval(soc=65)],
        [interval(), {**interval(), "valid_to": "invalid"}],
    ],
)
def test_no_future_forecast_or_gap_fallback_and_ambiguous_current_is_unknown(sources, rows):
    sources.timeline["intervals"] = rows
    if len(rows) == 2 and rows[1].get("valid_to") == "invalid":
        # Invalid rows are ignored; the one explicit, valid current interval remains.
        assert sources.entity.native_value == 69
    else:
        assert not sources.entity.available
        assert sources.entity.native_value is None
        assert sources.entity.extra_state_attributes is None


def test_half_open_bounds_use_forecast_only_once_it_covers_now(sources):
    sources.fleet.max_age = sources.plan.max_age = timedelta(hours=2)
    end = NOW + timedelta(minutes=5)
    sources.timeline["intervals"] = [interval(), interval(start=end, soc=80, quality="forecast")]
    assert sources.entity.native_value == 69  # Inclusive start.
    sources.clock.now = end - timedelta(microseconds=1)
    assert sources.entity.native_value == 69
    sources.clock.now = end
    assert sources.entity.native_value == 80  # The next row now covers the playhead.
    assert sources.entity.extra_state_attributes["quality"] == "forecast"
    sources.clock.now = end + timedelta(minutes=1)
    assert sources.entity.native_value == 80
    sources.clock.now = end + timedelta(minutes=5)
    assert sources.entity.native_value is None  # Exclusive end; never stretch a row.
    assert sources.entity.extra_state_attributes is None
    sources.plan.async_request_refresh.assert_not_awaited()


def test_gap_is_not_filled_by_nearest_old_or_future_forecast_row(sources):
    sources.fleet.max_age = sources.plan.max_age = timedelta(hours=2)
    sources.timeline["intervals"] = [
        interval(),
        interval(start=NOW + timedelta(minutes=10), soc=80, quality="forecast"),
    ]
    sources.clock.now = NOW + timedelta(minutes=5)
    assert sources.entity.native_value is None
    assert sources.entity.extra_state_attributes is None
    sources.clock.now = NOW + timedelta(minutes=10)
    assert sources.entity.native_value == 80
    assert sources.entity.extra_state_attributes["quality"] == "forecast"


async def test_current_interval_end_updates_locally_and_drops_source_attributes(
    hass, sources, monkeypatch
):
    sources.fleet.max_age = sources.plan.max_age = timedelta(hours=2)
    entity = sources.entity
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    scheduled = []

    def track(_hass, action, point):
        scheduled.append((action, point))
        return Mock()

    monkeypatch.setattr(sensor, "async_track_point_in_utc_time", track)
    await entity.async_added_to_hass()
    assert scheduled[-1][1] == NOW + timedelta(minutes=5)
    sources.clock.now = scheduled[-1][1]
    scheduled[-1][0](sources.clock.now)
    assert entity.native_value is None
    assert entity.extra_state_attributes is None
    entity.async_write_ha_state.assert_called_once()
    sources.plan.async_request_refresh.assert_not_awaited()
    entity._cancel_timer()


async def test_real_ha_has_separate_live70_and_app69_and_discovers_companion_once(
    hass, mock_client, section_data
):
    now = dt_util.utcnow()
    section_data["ev"].update(vehicles=[{**VEHICLE, "state_of_charge": 70}], vehicle_count=1)
    section_data["ev_plan"].update(
        vehicles=[
            {
                **VEHICLE,
                "timeline": {
                    "query_success": True,
                    "reported": True,
                    "fetched_at": now.isoformat(),
                    "intervals": [interval(start=now - timedelta(seconds=1))],
                },
            }
        ],
        vehicle_count=1,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "synthetic@example.test",
            CONF_REFRESH_TOKEN: mock_client.refresh_token,
            CONF_SITE_IDS: ["site_alpha"],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)

    def companions():
        return [
            item
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if item.unique_id.endswith(":app_soc")
        ]

    live = next(
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.unique_id.endswith(":soc")
    )
    assert len(companions()) == 1
    app = companions()[0]
    expected_live_id = (
        f"{entry.entry_id}:site_alpha:ev:{ev_sensor.vehicle_digest(VEHICLE['entity_id'])}:soc"
    )
    assert live.unique_id == expected_live_id
    assert hass.states.get(live.entity_id).state == "70"
    state = hass.states.get(app.entity_id)
    assert state.state == "69"
    assert state.attributes["raw_state_of_charge"] == 69.7304
    assert state.attributes["source"] == "ev_timeline"
    assert state.attributes["forecast_is_measurement"] is False
    assert "state_class" not in state.attributes
    assert state.attributes["device_class"] == "battery"
    assert "synthetic-private-vehicle" not in str(state.as_dict())
    await entry.runtime_data.sites["site_alpha"].ev.async_refresh()
    await hass.async_block_till_done()
    assert len(companions()) == 1
    assert companions()[0].unique_id == app.unique_id
    assert hass.states.get(live.entity_id).state == "70"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
