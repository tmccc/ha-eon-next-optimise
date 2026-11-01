"""Optional EV modules preserve unknowns, explicit dates and provider forecasts."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import dt as dt_util

from custom_components.eon_next_optimise import binary_sensor, ev_sensor, sensor
from custom_components.eon_next_optimise.const import MAX_AGES

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
VEHICLE = {"entity_id": "PRIVATE_VEHICLE", "make": "Tesla", "model": "Model 3"}


def period(start, soc=60, minutes=5, **extra):
    return {
        "valid_from": start.isoformat(),
        "valid_to": (start + timedelta(minutes=minutes)).isoformat(),
        "state_of_charge": soc,
        "price": 23.5,
        "source": "GRID",
        "quality": "forecast",
        **extra,
    }


def modules(now=NOW):
    meta = {"query_success": True, "reported": True, "fetched_at": now.isoformat()}
    return {
        "timeline": {
            **meta,
            "intervals": [
                period(now + timedelta(minutes=5), 60),
                period(now + timedelta(minutes=10), 65),
            ],
            "truncated": False,
        },
        "ready_by": {
            **meta,
            "configuration_reported": True,
            "target_soc": 80,
            "target_time": (now + timedelta(hours=2)).isoformat(),
            "active": False,
            "recurring": True,
            "cancelled_at": now.isoformat(),
        },
        "history": {
            **meta,
            "nominal_capacity_wh": 80000,
            "nominal_economy_km_per_kwh": 6.5,
            "days": [
                {"date": "2026-10-28", "home_charge_kwh": 12, "grid_cost": 8},
                {
                    "date": "2026-10-29",
                    "home_charge_kwh": 1.25,
                    "away_charge_kwh": 0,
                    "grid_charge_kwh": 0.75,
                    "solar_charge_kwh": 0.5,
                    "battery_charge_kwh": 0,
                    "grid_cost": 0.21,
                    "grid_average_price": 28,
                },
            ],
        },
    }


@pytest.fixture
def sources(monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock.now)

    def section(name, rows):
        return SimpleNamespace(
            entry=SimpleNamespace(entry_id="PRIVATE_ENTRY"),
            site_id="PRIVATE_SITE",
            section=name,
            data={"vehicles": rows, "vehicle_count": len(rows)},
            last_update_success=True,
            last_success=NOW,
            max_age=MAX_AGES[name],
            async_add_listener=Mock(return_value=Mock()),
            async_request_refresh=AsyncMock(),
        )

    fleet = section("ev", [dict(VEHICLE)])
    data = modules()
    plan = section(
        "ev_plan", [{**VEHICLE, "timeline": data["timeline"], "ready_by": data["ready_by"]}]
    )
    history = section("ev_history", [{**VEHICLE, "history": data["history"]}])
    plan.fleet_coordinator = history.fleet_coordinator = fleet
    return SimpleNamespace(clock=clock, fleet=fleet, plan=plan, history=history, modules=data)


def target(sources, timestamp=False):
    return ev_sensor.OptimiseEVReadyBySensor(
        sources.plan, "Optimise site 1", VEHICLE, 1, timestamp=timestamp
    )


def forecast(sources):
    return ev_sensor.OptimiseEVForecastSensor(sources.plan, "Optimise site 1", VEHICLE, 1)


def history(sources, key="history_home_charge"):
    spec = next(item for item in ev_sensor.EV_HISTORY_SENSORS if item.key == key)
    return ev_sensor.OptimiseEVHistorySensor(sources.history, "Optimise site 1", VEHICLE, 1, spec)


def test_ready_by_target_and_boolean_flags_are_direct_and_not_inferred(sources):
    assert target(sources).native_value == 80
    assert target(sources).state_class is None
    instant = target(sources, timestamp=True)
    assert instant.device_class == SensorDeviceClass.TIMESTAMP
    assert instant.native_value == NOW + timedelta(hours=2)
    active = binary_sensor.OptimiseEVReadyByConfigSensor(
        sources.plan, "Optimise site 1", VEHICLE, 1, "active"
    )
    recurring = binary_sensor.OptimiseEVReadyByConfigSensor(
        sources.plan, "Optimise site 1", VEHICLE, 1, "recurring"
    )
    assert active.available and active.is_on is False
    assert recurring.available and recurring.is_on is True
    sources.modules["ready_by"]["active"] = None
    assert not active.available and active.is_on is None  # Target/cancellation imply nothing.
    sources.modules["ready_by"]["target_time"] = None
    assert not instant.available and instant.native_value is None


@pytest.mark.parametrize("failure", ["query", "unreported", "time", "missing_vehicle"])
def test_optional_module_failure_isolated_and_never_reuses_other_current_data(sources, failure):
    entity = forecast(sources)
    assert entity.native_value == 60
    if failure == "query":
        sources.modules["timeline"]["query_success"] = False
    elif failure == "unreported":
        sources.modules["timeline"]["reported"] = False
    elif failure == "time":
        sources.modules["timeline"]["fetched_at"] = (NOW - MAX_AGES["ev_plan"]).isoformat()
    else:
        sources.plan.data["vehicles"] = []
    assert not entity.available and entity.native_value is None
    assert entity.extra_state_attributes["forecast"] == []
    assert entity.extra_state_attributes["forecast_period_count"] is None
    assert history(sources).available
    if failure != "missing_vehicle":
        assert target(sources).available


def test_next_forecast_with_out_of_range_raw_soc_stays_unknown_without_skipping_point(sources):
    first = sources.modules["timeline"]["intervals"][0]
    first.update(state_of_charge=None, raw_state_of_charge=100.08902189553524)
    entity = forecast(sources)
    assert not entity.available
    assert entity.native_value is None
    attrs = entity.extra_state_attributes
    assert attrs["forecast_period_count"] == 2
    assert attrs["forecast"][0]["state_of_charge"] is None
    assert attrs["forecast"][0]["price_p_per_kwh"] == 23.5
    assert attrs["forecast"][1]["state_of_charge"] == 65
    assert "raw_state_of_charge" not in str(attrs)
    assert target(sources).available


@pytest.mark.parametrize("failure", ["stale", "failed", "unknown", "removed"])
def test_optional_entities_require_current_live_fleet_membership(sources, failure):
    entity = history(sources)
    if failure == "stale":
        sources.fleet.last_success -= sources.fleet.max_age
    elif failure == "failed":
        sources.fleet.last_update_success = False
    elif failure == "unknown":
        sources.fleet.data["vehicle_count"] = None
    else:
        sources.fleet.data.update(vehicles=[], vehicle_count=0)
    assert not entity.available and entity.native_value is None
    assert not target(sources).available
    assert not forecast(sources).available


def test_history_latest_reported_day_is_shared_not_today_or_last_nonnull(sources):
    home = history(sources)
    assert home.native_value == 1.25 and home.native_unit_of_measurement == "kWh"
    assert home.state_class is None
    assert home.extra_state_attributes == {
        "reported_date": "2026-10-29",
        "period_kind": "latest_reported_day",
    }
    assert "today" not in home.entity_description.name.lower()
    assert history(sources, "history_grid_cost").native_value == 0.21
    assert history(sources, "history_grid_cost").native_unit_of_measurement == "GBP"
    assert history(sources, "history_grid_average_price").native_value == 28
    assert history(sources, "history_nominal_capacity").native_value == 80000
    assert history(sources, "history_nominal_capacity").native_unit_of_measurement == "Wh"
    assert history(sources, "history_nominal_economy").native_unit_of_measurement == "km/kWh"
    assert all(
        history(sources, spec.key).state_class is None for spec in ev_sensor.EV_HISTORY_SENSORS
    )
    sources.modules["history"]["days"][-1]["home_charge_kwh"] = None
    assert not home.available and home.native_value is None  # Do not fall back to older12kWh.
    assert home.extra_state_attributes["reported_date"] == "2026-10-29"


def test_forecast_bounded_allowlisted_unknown_count_and_no_measurement_statistics(sources):
    entity = forecast(sources)
    sources.modules["timeline"].update(
        intervals=[
            period(NOW + timedelta(minutes=5 * index), index % 100, PRIVATE="PRIVATE_TOKEN")
            for index in range(-2, 650)
        ],
        truncated=True,
    )
    attrs = entity.extra_state_attributes
    assert len(attrs["forecast"]) == attrs["forecast_period_count"] == 100
    assert attrs["forecast_truncated"] is True
    assert attrs["forecast_is_measurement"] is False
    assert entity.state_class is None
    assert all(datetime.fromisoformat(row["valid_to"]) > NOW for row in attrs["forecast"])
    assert all(
        datetime.fromisoformat(row["valid_to"]) <= NOW + timedelta(hours=48)
        for row in attrs["forecast"]
    )
    assert "PRIVATE" not in str(attrs)
    sources.modules["timeline"]["intervals"] = []
    assert not entity.available
    assert entity.extra_state_attributes["forecast_period_count"] == 0  # Known empty is real.
    sources.modules["timeline"]["reported"] = False
    assert entity.extra_state_attributes["forecast_period_count"] is None


async def test_forecast_boundary_updates_locally_without_provider_query(hass, sources, monkeypatch):
    sources.fleet.max_age = sources.plan.max_age = timedelta(hours=2)
    entity = forecast(sources)
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    scheduled = []

    def track(_hass, action, point):
        scheduled.append((action, point))
        return Mock()

    monkeypatch.setattr(sensor, "async_track_point_in_utc_time", track)
    await entity.async_added_to_hass()
    assert entity.native_value == 60
    assert scheduled[-1][1] == NOW + timedelta(minutes=5)
    sources.clock.now = scheduled[-1][1]
    scheduled[-1][0](sources.clock.now)
    assert entity.native_value == 65
    entity.async_write_ha_state.assert_called_once()
    sources.plan.async_request_refresh.assert_not_awaited()
    entity._cancel_timer()


def test_optional_state_attributes_and_registry_ids_never_expose_provider_identity(sources):
    for entity in (target(sources), forecast(sources), history(sources)):
        assert "PRIVATE_VEHICLE" not in entity.unique_id
        assert "PRIVATE_VEHICLE" not in str(entity.device_info)
        assert "PRIVATE" not in str(entity.extra_state_attributes)
        assert ":ev:" in entity.unique_id
        assert entity.unique_id.count(":") == 4
        assert not any(
            hasattr(entity, action)
            for action in ("async_turn_on", "async_set_native_value", "async_press")
        )
