"""Entity isolation, exact interval transitions, missing data and privacy."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from custom_components.eon_next_optimise import binary_sensor, diagnostics, sensor
from custom_components.eon_next_optimise.const import DOMAIN

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    result = SimpleNamespace(now=NOW)
    monkeypatch.setattr(sensor.dt_util, "utcnow", lambda: result.now)
    return result


@pytest.fixture
def make_coordinator(clock):
    def make(section, data=None, *, site_id="private-site-id", entry_id="private-entry-id"):
        return SimpleNamespace(
            entry=SimpleNamespace(entry_id=entry_id),
            site_id=site_id,
            section=section,
            data=data if data is not None else {},
            max_age=sensor.DEFAULT_MAX_AGES[section],
            last_success=clock.now,
            last_update_success=True,
            last_exception=None,
            async_add_listener=Mock(return_value=Mock()),
            async_request_refresh=AsyncMock(),
        )

    return make


def period(start, value=10.0, *, duration=timedelta(minutes=30), **extra):
    return {
        "valid_from": start.isoformat(),
        "valid_to": (start + duration).isoformat(),
        "value": value,
        "raw_value": value,
        "quality": "forecast",
        "estimate": True,
        **extra,
    }


def value_spec(key):
    return next(item for item in (*sensor.LIVE_SENSORS, *sensor.STATUS_SENSORS) if item.key == key)


@pytest.mark.parametrize("value", [None, True, "0", float("inf"), float("nan")])
def test_unknown_measurements_are_not_zero(make_coordinator, value):
    coordinator = make_coordinator("live", {"stateOfChargePercentage": value})
    entity = sensor.OptimiseValueSensor(coordinator, "Optimise site 1", value_spec("battery_soc"))
    assert entity.available is False
    assert entity.native_value is None


def test_zero_and_false_are_real_values(make_coordinator):
    coordinator = make_coordinator("live", {"stateOfChargePercentage": 0})
    entity = sensor.OptimiseValueSensor(coordinator, "Optimise site 1", value_spec("battery_soc"))
    assert entity.available
    assert entity.native_value == 0
    status = make_coordinator("status", {"battery_enrolled": False})
    binary = binary_sensor.OptimiseBinarySensor(
        status, "Optimise site 1", binary_sensor.BINARY_SENSORS[0]
    )
    assert binary.available
    assert binary.is_on is False
    status.data["battery_enrolled"] = None
    assert not binary.available
    assert binary.is_on is None


def test_failure_and_freshness_are_section_local(make_coordinator, clock):
    live = make_coordinator("live", {"stateOfChargePercentage": 55})
    prices = make_coordinator("prices", {"import": [period(NOW, 0)]})
    power_entity = sensor.OptimiseValueSensor(live, "Optimise site 1", value_spec("battery_soc"))
    price_entity = sensor.OptimisePriceSensor(prices, "Optimise site 1", "import")
    assert power_entity.available and price_entity.available
    prices.last_update_success = False
    assert not price_entity.available
    assert power_entity.available
    prices.last_update_success = True
    prices.max_age = timedelta(hours=1)
    live.last_success = NOW - live.max_age
    assert not power_entity.available
    assert price_entity.available
    live.last_success = NOW.replace(tzinfo=None)
    assert not power_entity.available


def test_price_gaps_and_range_only_quotes_remain_unknown(make_coordinator):
    rows = [period(NOW, None, range_min=2, range_max=8), period(NOW + timedelta(hours=1), 12)]
    coordinator = make_coordinator("prices", {"import": rows})
    entity = sensor.OptimisePriceSensor(coordinator, "Optimise site 1", "import")
    upcoming = sensor.OptimisePriceSensor(
        coordinator, "Optimise site 1", "import", next_period=True
    )
    assert not entity.available
    assert entity.native_value is None
    assert not upcoming.available
    assert upcoming.native_value is None
    rows[0]["value"] = 0
    assert entity.available and entity.native_value == 0
    assert not upcoming.available  # Missing immediately adjacent interval is not filled.


async def test_exact_price_boundary_updates_locally(hass, make_coordinator, clock, monkeypatch):
    rows = [period(NOW, 10), period(NOW + timedelta(minutes=30), 12)]
    coordinator = make_coordinator("prices", {"import": rows})
    coordinator.max_age = timedelta(hours=2)
    entity = sensor.OptimisePriceSensor(coordinator, "Optimise site 1", "import")
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    scheduled = []

    def track(_hass, action, point):
        scheduled.append((action, point))
        return Mock()

    monkeypatch.setattr(sensor, "async_track_point_in_utc_time", track)
    await entity.async_added_to_hass()
    assert scheduled[0][1] == NOW + timedelta(minutes=30)
    clock.now = scheduled[0][1]
    scheduled[0][0](clock.now)
    assert entity.native_value == 12
    entity.async_write_ha_state.assert_called_once()
    coordinator.async_request_refresh.assert_not_awaited()
    # The next expiry is scheduled precisely, not a minute-polling approximation.
    assert scheduled[-1][1] == NOW + timedelta(hours=1)
    entity._cancel_timer()


def test_forecasts_are_bounded_expired_removed_and_allowlisted(make_coordinator, clock):
    rows = [
        period(NOW + timedelta(minutes=30 * index), 10, private="do-not-publish")
        for index in range(-2, 110)
    ]
    coordinator = make_coordinator("prices", {"import": rows})
    entity = sensor.OptimisePriceSensor(coordinator, "Optimise site 1", "import")
    forecast = entity.extra_state_attributes["forecast"]
    assert 0 < len(forecast) <= sensor.MAX_ATTRIBUTE_PERIODS
    assert all(
        NOW < datetime.fromisoformat(row["valid_from"]) < NOW + timedelta(days=2)
        for row in forecast
    )
    assert "do-not-publish" not in str(entity.extra_state_attributes)
    clock.now += timedelta(minutes=30)
    coordinator.last_success = clock.now
    assert all(
        datetime.fromisoformat(row["valid_to"]) > clock.now
        for row in entity.extra_state_attributes["forecast"]
    )
    coordinator.last_update_success = False
    assert entity.extra_state_attributes["forecast"] == []
    assert entity.extra_state_attributes["period"] is None


def test_stored_energy_has_storage_measurement_semantics(make_coordinator):
    entity = sensor.OptimiseValueSensor(
        make_coordinator("live", {"batteryEnergyWh": 6000}),
        "Optimise site 1",
        value_spec("battery_energy"),
    )
    assert entity.device_class == SensorDeviceClass.ENERGY_STORAGE
    assert entity.state_class == SensorStateClass.MEASUREMENT
    assert entity.native_value == 6000


def test_interval_metrics_units_without_unproven_reset_statistics(make_coordinator):
    coordinator = make_coordinator(
        "live",
        {
            "metrics_interval_start": NOW.isoformat(),
            "metrics_interval_end": (NOW + timedelta(hours=1)).isoformat(),
            "metrics_error_type": None,
            "siteImportKWh": 1.25,
            "importCostsCents": -123,
        },
    )
    energy = sensor.OptimiseMetricSensor(coordinator, "Optimise site 1", sensor.METRIC_SENSORS[0])
    cost = sensor.OptimiseMetricSensor(
        coordinator,
        "Optimise site 1",
        next(spec for spec in sensor.METRIC_SENSORS if spec.field == "importCostsCents"),
    )
    assert energy.available and energy.native_value == 1.25
    assert energy.native_unit_of_measurement == "kWh"
    assert energy.device_class == SensorDeviceClass.ENERGY
    assert energy.state_class is None
    assert cost.available and cost.native_value == pytest.approx(-1.23)
    assert cost.native_unit_of_measurement == "GBP"
    assert cost.device_class is None and cost.state_class is None
    assert "today" not in energy.entity_description.name.lower()
    assert energy.extra_state_attributes["interval_end"] == (NOW + timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("failure", ["missing_interval", "future", "expired", "error", "null"])
def test_optional_metrics_problem_never_hides_live_soc(make_coordinator, failure):
    data = {
        "stateOfChargePercentage": 45,
        "metrics_interval_start": NOW.isoformat(),
        "metrics_interval_end": (NOW + timedelta(hours=1)).isoformat(),
        "metrics_error_type": None,
        "siteImportKWh": 0,
    }
    if failure == "missing_interval":
        data["metrics_interval_start"] = None
    elif failure == "future":
        data["metrics_interval_start"] = (NOW + timedelta(minutes=30)).isoformat()
    elif failure == "expired":
        data["metrics_interval_start"] = (NOW - timedelta(hours=1)).isoformat()
        data["metrics_interval_end"] = NOW.isoformat()
    elif failure == "error":
        data["metrics_error_type"] = "invalid_metrics"
    else:
        data["siteImportKWh"] = None
    coordinator = make_coordinator("live", data)
    energy = sensor.OptimiseMetricSensor(coordinator, "Optimise site 1", sensor.METRIC_SENSORS[0])
    soc = sensor.OptimiseValueSensor(coordinator, "Optimise site 1", value_spec("battery_soc"))
    assert not energy.available and energy.native_value is None
    assert energy.extra_state_attributes["interval_start"] is None
    assert soc.available and soc.native_value == 45


def test_multiple_batteries_do_not_invent_one_reserve(make_coordinator):
    coordinator = make_coordinator("status", {"minimum_reserve": [20, 35, None]})
    entity = sensor.OptimiseReserveSensor(coordinator, "Optimise site 1")
    assert not entity.available
    assert entity.native_value is None
    assert entity.extra_state_attributes["minimum_reserve_levels"] == [20, 35, None]
    coordinator.data["minimum_reserve"] = [20]
    assert entity.available and entity.native_value == 20


def test_plan_expiry_and_missing_values(make_coordinator, clock):
    rows = [
        {
            "valid_from": NOW.isoformat(),
            "valid_to": (NOW + timedelta(minutes=30)).isoformat(),
            "battery_energy_wh": None,
            "actions": ["charge"],
            "private": "not-public",
        }
    ]
    coordinator = make_coordinator("plan", {"forecast": rows, "error_type": None})
    measurement = sensor.OptimisePlanSensor(coordinator, "Optimise site 1", sensor.PLAN_SENSORS[0])
    status = sensor.OptimisePlanStatusSensor(coordinator, "Optimise site 1")
    assert not measurement.available and measurement.native_value is None
    assert status.native_value == "forecast_available"
    assert "not-public" not in str(status.extra_state_attributes)
    rows[0]["battery_energy_wh"] = 0
    assert measurement.available and measurement.native_value == 0
    clock.now += timedelta(minutes=30)
    coordinator.last_success = clock.now
    assert status.native_value == "no_forecast"
    assert status.extra_state_attributes["forecast"] == []
    assert not measurement.available


async def test_entity_platforms_are_read_only_and_sites_separate(make_coordinator):
    def site(identity):
        return SimpleNamespace(
            **{
                section: make_coordinator(section, site_id=identity)
                for section in sensor.DEFAULT_MAX_AGES
            }
        )

    entry = SimpleNamespace(
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(
            sites={"private-a": site("private-a"), "private-b": site("private-b")}
        ),
    )
    entities = []
    await sensor.async_setup_entry(None, entry, entities.extend)
    await binary_sensor.async_setup_entry(None, entry, entities.extend)
    assert len(entities) == 106
    assert len({entity.unique_id for entity in entities}) == 106
    assert all(isinstance(entity, (SensorEntity, BinarySensorEntity)) for entity in entities)
    assert len({next(iter(entity.device_info["identifiers"])) for entity in entities}) == 2
    assert all(
        entity.device_info["name"] in {"Optimise site 1", "Optimise site 2"} for entity in entities
    )
    assert all(next(iter(entity.device_info["identifiers"]))[0] == DOMAIN for entity in entities)
    assert not any(
        hasattr(entity, action)
        for entity in entities
        for action in (
            "async_turn_on",
            "async_turn_off",
            "async_press",
            "async_set_native_value",
            "async_select_option",
        )
    )


async def test_diagnostics_cannot_read_credentials_client_or_identities(make_coordinator):
    class PrivateEntry:
        @property
        def data(self):
            raise AssertionError("Diagnostics must not access config entry data")

    class Runtime:
        @property
        def client(self):
            raise AssertionError("Diagnostics must not access the authenticated client")

    site = SimpleNamespace(
        **{section: make_coordinator(section) for section in sensor.DEFAULT_MAX_AGES}
    )
    site.prices.data = {"import": [period(NOW)], "export": [], "password": "private-password"}
    site.live.data = {"sitePowerW": 0, "email": "private@example.test"}
    site.plan.last_exception = ValueError("private-token-private-site-id")
    site.plan.data = {"forecast": [], "error_type": "private-provider-body"}
    site.status.data = {
        "devices": [{"device_id": "private-device-id"}],
        "minimum_reserve": [20],
        "error_count": 0,
    }
    runtime = Runtime()
    runtime.sites = {"private-site-id": site}
    entry = PrivateEntry()
    entry.runtime_data = runtime
    result = await diagnostics.async_get_config_entry_diagnostics(None, entry)
    assert result["site_count"] == 1
    sections = result["sites"][0]["sections"]
    assert sections["prices"]["period_counts"] == {"import": 1, "export": 0}
    assert sections["plan"]["error_category"] == "unknown"
    assert sections["plan"]["has_data_error"] is True
    assert sections["status"]["device_error_count"] == 0
    for forbidden in (
        "private",
        "password",
        "email",
        "token",
        "device_id",
        "site_id",
        "rates",
        'forecast":',
    ):
        assert forbidden not in str(result)
    site.plan.last_error_class = "rate_limited"
    result = await diagnostics.async_get_config_entry_diagnostics(None, entry)
    assert result["sites"][0]["sections"]["plan"]["error_category"] == "rate_limited"


@pytest.mark.parametrize(
    ("legacy", "unified"), [(False, True), (True, False), (None, True), (False, None), (None, None)]
)
def test_ev_enrollment_flags_are_independent_unknowns_not_inferred(
    make_coordinator, legacy, unified
):
    status = make_coordinator("status", {"ev_enrolled": legacy, "ev_devices_enrolled": unified})
    entities = {
        key: binary_sensor.OptimiseBinarySensor(
            status,
            "Optimise site 1",
            next(spec for spec in binary_sensor.BINARY_SENSORS if spec.key == key),
        )
        for key in ("ev_enrolled", "ev_devices_enrolled")
    }
    assert entities["ev_enrolled"].available is (legacy is not None)
    assert entities["ev_devices_enrolled"].available is (unified is not None)
    assert entities["ev_enrolled"].is_on is legacy
    assert entities["ev_devices_enrolled"].is_on is unified
    assert entities["ev_enrolled"].entity_description.name == "Legacy EV enrolled"
    assert entities["ev_devices_enrolled"].entity_description.name == "EV devices enrolled"


async def test_ev_device_enrollment_entity_exists_when_provider_field_is_missing(make_coordinator):
    site = SimpleNamespace(
        **{section: make_coordinator(section) for section in sensor.DEFAULT_MAX_AGES}
    )
    site.status.data = {"ev_enrolled": False}
    entry = SimpleNamespace(
        async_on_unload=Mock(), runtime_data=SimpleNamespace(sites={"private-site": site})
    )
    entities = []
    await binary_sensor.async_setup_entry(None, entry, entities.extend)
    assert len(entities) == 9
    target = next(
        entity for entity in entities if entity.unique_id.endswith(":ev_devices_enrolled")
    )
    assert not target.available
    assert target.is_on is None
