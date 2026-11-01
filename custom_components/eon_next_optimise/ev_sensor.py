"""Read-only vehicle telemetry with private identifiers and dynamic discovery."""

import hashlib
import math
from dataclasses import dataclass
from datetime import date

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import DataError, parse_time
from .sensor import (
    FORECAST_HORIZON,
    MAX_ATTRIBUTE_PERIODS,
    OptimiseEntity,
    _data,
    _periods,
    _safe_text,
    finite_number,
    maximum_age,
    section_is_fresh,
    valid_instant,
)


def vehicle_digest(identity: str) -> str:
    """Stable registry identity; never publish the provider's raw vehicle ID."""
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def vehicles(coordinator):
    """Read only the validated current response, never retain missing telemetry."""
    rows = _data(coordinator).get("vehicles")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("entity_id"), str) and row["entity_id"]
    ]


def known_fleet(coordinator):
    rows = vehicles(coordinator)
    count = _data(coordinator).get("vehicle_count")
    return (
        rows
        if section_is_fresh(coordinator) and type(count) is int and count == len(rows)
        else None
    )


def module_instant(value):
    try:
        return parse_time(value)
    except DataError:
        return None


class OptimiseEVEntity(OptimiseEntity):
    """A vehicle child device using the independent EV freshness boundary."""

    def __init__(self, coordinator, site_label, vehicle, number, key):
        self._vehicle_id = vehicle["entity_id"]
        digest = vehicle_digest(self._vehicle_id)
        super().__init__(coordinator, site_label, f"ev:{digest}:{key}")
        self._attr_translation_key = f"ev_{key}"
        site_identity = f"{coordinator.entry.entry_id}:{coordinator.site_id}"
        parent_id = None
        if (hass := getattr(coordinator, "hass", None)) is not None:
            parent = dr.async_get(hass).async_get_or_create(
                config_entry_id=coordinator.entry.entry_id,
                identifiers={(DOMAIN, site_identity)},
                name=site_label,
                manufacturer="E.ON Next / Amber",
                model="Read-only Optimise telemetry",
            )
            parent_id = parent.id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{site_identity}:ev:{digest}")},
            name=f"Optimise EV {number}",
            manufacturer=_safe_text(vehicle.get("make"), 80),
            model=_safe_text(vehicle.get("model"), 80),
            via_device_id=parent_id,
        )

    def _vehicle(self):
        return next(
            (row for row in vehicles(self.coordinator) if row["entity_id"] == self._vehicle_id),
            None,
        )

    @property
    def available(self):
        fleet = getattr(self.coordinator, "fleet_coordinator", None)
        if self.coordinator.section in {"ev_plan", "ev_history"}:
            current = known_fleet(fleet) if fleet is not None else None
            if current is None or not any(row["entity_id"] == self._vehicle_id for row in current):
                return False
        return super().available and self._vehicle() is not None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        fleet = getattr(self.coordinator, "fleet_coordinator", None)
        if fleet is not None:
            self.async_on_remove(fleet.async_add_listener(self._handle_coordinator_update))

    def _change_instants(self, now):
        result = super()._change_instants(now)
        fleet = getattr(self.coordinator, "fleet_coordinator", None)
        success = getattr(fleet, "last_success", None)
        if fleet is not None and valid_instant(success):
            result.append(success + maximum_age(fleet))
        return result


class OptimiseEVModuleEntity(OptimiseEVEntity):
    """Each optional module has its own success and retrieval timestamp."""

    def __init__(self, coordinator, site_label, vehicle, number, key, module):
        super().__init__(coordinator, site_label, vehicle, number, key)
        self.module_name = module

    def _module(self):
        row = self._vehicle()
        module = row.get(self.module_name) if row is not None else None
        return module if isinstance(module, dict) else {}

    @property
    def available(self):
        module = self._module()
        fetched = module_instant(module.get("fetched_at"))
        return bool(
            super().available
            and module.get("query_success") is True
            and module.get("reported") is True
            and fetched is not None
            and fetched <= dt_util.utcnow() < fetched + maximum_age(self.coordinator)
        )

    def _change_instants(self, now):
        result = super()._change_instants(now)
        fetched = module_instant(self._module().get("fetched_at"))
        if fetched is not None:
            result.append(fetched + maximum_age(self.coordinator))
        return result


@dataclass(frozen=True, slots=True)
class EVSensorSpec:
    key: str
    field: str
    name: str


EV_SENSORS = (
    EVSensorSpec("soc", "state_of_charge", "State of charge"),
    EVSensorSpec("state", "live_state", "State"),
    EVSensorSpec("connectivity", "connectivity", "Connectivity"),
    EVSensorSpec("charging_speed", "charging_speed_label", "Charging speed"),
)


class OptimiseEVSensor(OptimiseEVEntity, SensorEntity):
    """Source measurements and labels only; no inferred plug/charging flags."""

    def __init__(self, coordinator, site_label, vehicle, number, description):
        super().__init__(coordinator, site_label, vehicle, number, description.key)
        self.description = description
        self.entity_description = SensorEntityDescription(
            key=f"ev_{description.key}", name=description.name
        )
        if description.key == "soc":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_native_unit_of_measurement = PERCENTAGE

    def _value(self):
        row = self._vehicle()
        if row is None:
            return None
        value = row.get(self.description.field)
        if self.description.key == "soc":
            return value if finite_number(value) and 0 <= value <= 100 else None
        if self.description.key == "connectivity":
            value = _safe_text(row.get("connectivity_label"), 160) or value
        return _safe_text(value, 160)

    @property
    def available(self):
        return super().available and self._value() is not None

    @property
    def native_value(self):
        return self._value() if self.available else None


def setup_dynamic_vehicles(entry, coordinator, factory, async_add_entities):
    """Discover new vehicles locally after updates, retaining unavailable old IDs."""
    known = set()

    @callback
    def add_new():
        # Entry unload callbacks run after the integration's awaited shutdown.
        # A late coordinator update must not recreate removed platform entities.
        if getattr(entry, "state", None) in {
            ConfigEntryState.UNLOAD_IN_PROGRESS,
            ConfigEntryState.NOT_LOADED,
        }:
            return
        added = []
        current = known_fleet(coordinator)
        if current is None:
            return
        for vehicle in current:
            identity = vehicle["entity_id"]
            if identity in known:
                continue
            known.add(identity)
            added.extend(factory(vehicle, len(known)))
        if added:
            async_add_entities(added)

    entry.async_on_unload(coordinator.async_add_listener(add_new))
    add_new()


@dataclass(frozen=True, slots=True)
class EVHistorySpec:
    key: str
    field: str
    name: str
    unit: str
    daily: bool = True
    device_class: SensorDeviceClass | None = None


EV_HISTORY_SENSORS = (
    EVHistorySpec(
        "history_home_charge",
        "home_charge_kwh",
        "Latest reported day home charge",
        UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    EVHistorySpec(
        "history_away_charge",
        "away_charge_kwh",
        "Latest reported day away charge",
        UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    EVHistorySpec(
        "history_grid_charge",
        "grid_charge_kwh",
        "Latest reported day grid charge",
        UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    EVHistorySpec(
        "history_solar_charge",
        "solar_charge_kwh",
        "Latest reported day solar charge",
        UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    EVHistorySpec(
        "history_battery_charge",
        "battery_charge_kwh",
        "Latest reported day battery charge",
        UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
    ),
    EVHistorySpec("history_grid_cost", "grid_cost", "Latest reported day grid cost", "GBP"),
    EVHistorySpec(
        "history_grid_average_price",
        "grid_average_price",
        "Latest reported day average grid price",
        "p/kWh",
    ),
    EVHistorySpec(
        "history_nominal_capacity",
        "nominal_capacity_wh",
        "Nominal battery capacity",
        UnitOfEnergy.WATT_HOUR,
        daily=False,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
    ),
    EVHistorySpec(
        "history_nominal_economy",
        "nominal_economy_km_per_kwh",
        "Nominal economy",
        "km/kWh",
        daily=False,
    ),
)


class OptimiseEVReadyBySensor(OptimiseEVModuleEntity, SensorEntity):
    def __init__(self, coordinator, site_label, vehicle, number, *, timestamp=False):
        key = "plan_target_time" if timestamp else "plan_target_soc"
        super().__init__(coordinator, site_label, vehicle, number, key, "ready_by")
        self.timestamp = timestamp
        self.entity_description = SensorEntityDescription(
            key=f"ev_{key}",
            name="Ready-by target time" if timestamp else "Ready-by target state of charge",
        )
        self._attr_device_class = (
            SensorDeviceClass.TIMESTAMP if timestamp else SensorDeviceClass.BATTERY
        )
        if not timestamp:
            self._attr_native_unit_of_measurement = PERCENTAGE

    def _value(self):
        module = self._module()
        if module.get("configuration_reported") is not True:
            return None
        value = module.get("target_time" if self.timestamp else "target_soc")
        if self.timestamp:
            return module_instant(value)
        return value if finite_number(value) and 0 <= value <= 100 else None

    @property
    def available(self):
        return super().available and self._value() is not None

    @property
    def native_value(self):
        return self._value() if self.available else None


class OptimiseEVForecastSensor(OptimiseEVModuleEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, site_label, vehicle, number):
        super().__init__(coordinator, site_label, vehicle, number, "plan_forecast_soc", "timeline")
        self.entity_description = SensorEntityDescription(
            key="ev_plan_forecast_soc", name="Next forecast state of charge"
        )

    def _rows(self):
        if not super().available:
            return None
        now = dt_util.utcnow()
        return [
            (start, end, row)
            for start, end, row in _periods(self._module().get("intervals"))
            if end > now and start < now + FORECAST_HORIZON and end <= now + FORECAST_HORIZON
        ][:MAX_ATTRIBUTE_PERIODS]

    def _next(self):
        return next(
            (row for start, _end, row in self._rows() or [] if start > dt_util.utcnow()), None
        )

    @property
    def available(self):
        row = self._next()
        value = row.get("state_of_charge") if row else None
        return super().available and finite_number(value) and 0 <= value <= 100

    @property
    def native_value(self):
        return self._next()["state_of_charge"] if self.available else None

    def _change_instants(self, now):
        return super()._change_instants(now) + [
            point for start, end, _row in self._rows() or [] for point in (start, end)
        ]

    @property
    def extra_state_attributes(self):
        rows = self._rows()
        forecast = []
        for start, end, row in rows or []:
            value = row.get("state_of_charge")
            price = row.get("price")
            forecast.append(
                {
                    "valid_from": start.isoformat(),
                    "valid_to": end.isoformat(),
                    "state_of_charge": value
                    if finite_number(value) and 0 <= value <= 100
                    else None,
                    "price_p_per_kwh": price if finite_number(price) else None,
                    "source": _safe_text(row.get("source"), 128),
                    "quality": row.get("quality")
                    if row.get("quality") in {"current", "forecast"}
                    else None,
                }
            )
        return {
            "forecast": forecast,
            "forecast_period_count": len(forecast) if rows is not None else None,
            "forecast_truncated": self._module().get("truncated") if rows is not None else None,
            "forecast_is_measurement": False,
            "forecast_source": "provider",
        }


class OptimiseEVAppSocSensor(OptimiseEVModuleEntity, SensorEntity):
    """Mirror the app's current timeline gauge without changing live telemetry."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, site_label, vehicle, number):
        super().__init__(coordinator, site_label, vehicle, number, "app_soc", "timeline")
        self.entity_description = SensorEntityDescription(
            key="ev_app_soc", name="Battery level shown in app"
        )

    @staticmethod
    def _raw_soc(row):
        return row.get("raw_state_of_charge", row.get("state_of_charge"))

    def _current(self):
        if not super().available:
            return None
        now = dt_util.utcnow()
        matches = [
            (start, end, row)
            for start, end, row in _periods(self._module().get("intervals"))
            if row.get("quality") in {"current", "forecast"} and start <= now < end
        ]
        if len(matches) != 1 or not finite_number(self._raw_soc(matches[0][2])):
            return None
        return matches[0]

    @property
    def available(self):
        return self._current() is not None

    @property
    def native_value(self):
        current = self._current()
        if current is None:
            return None
        # Official app sanitiseSocPercent floors the interval's starting SOC.
        return max(0, min(100, math.floor(self._raw_soc(current[2]))))

    def _change_instants(self, now):
        return super()._change_instants(now) + [
            point
            for start, end, row in _periods(self._module().get("intervals"))
            if row.get("quality") in {"current", "forecast"}
            for point in (start, end)
        ]

    @property
    def extra_state_attributes(self):
        current = self._current()
        if current is None:
            return None
        start, end, row = current
        return {
            "source": "ev_timeline",
            "raw_state_of_charge": self._raw_soc(row),
            "valid_from": start.isoformat(),
            "valid_to": end.isoformat(),
            "quality": row["quality"],
            "forecast_is_measurement": False,
            "display_rounding": "floor",
        }


class OptimiseEVHistorySensor(OptimiseEVModuleEntity, SensorEntity):
    def __init__(self, coordinator, site_label, vehicle, number, description):
        super().__init__(coordinator, site_label, vehicle, number, description.key, "history")
        self.description = description
        self.entity_description = SensorEntityDescription(
            key=f"ev_{description.key}", name=description.name
        )
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        # These are dated reports/nominal configuration, not reset-verified counters.

    def _latest_day(self):
        days = self._module().get("days")
        if not isinstance(days, list):
            return None
        valid = []
        for row in days:
            if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                continue
            try:
                day = date.fromisoformat(row["date"])
            except ValueError:
                continue
            if day.isoformat() == row["date"]:
                valid.append(row)
        return max(valid, key=lambda row: row["date"]) if valid else None

    def _value(self):
        row = self._latest_day() if self.description.daily else self._module()
        value = row.get(self.description.field) if row is not None else None
        return value if finite_number(value) else None

    @property
    def available(self):
        return super().available and self._value() is not None

    @property
    def native_value(self):
        return self._value() if self.available else None

    @property
    def extra_state_attributes(self):
        row = self._latest_day() if super().available else None
        return {
            "reported_date": row.get("date") if self.description.daily and row else None,
            "period_kind": "latest_reported_day"
            if self.description.daily
            else "nominal_configuration",
        }


async def async_setup_ev_sensors(entry, async_add_entities):
    for index, site in enumerate(entry.runtime_data.sites.values(), 1):

        def factory(vehicle, number, site=site, label=f"Optimise site {index}"):
            return [
                *[OptimiseEVSensor(site.ev, label, vehicle, number, spec) for spec in EV_SENSORS],
                OptimiseEVReadyBySensor(site.ev_plan, label, vehicle, number),
                OptimiseEVReadyBySensor(site.ev_plan, label, vehicle, number, timestamp=True),
                OptimiseEVForecastSensor(site.ev_plan, label, vehicle, number),
                OptimiseEVAppSocSensor(site.ev_plan, label, vehicle, number),
                *[
                    OptimiseEVHistorySensor(site.ev_history, label, vehicle, number, spec)
                    for spec in EV_HISTORY_SENSORS
                ],
            ]

        setup_dynamic_vehicles(entry, site.ev, factory, async_add_entities)
