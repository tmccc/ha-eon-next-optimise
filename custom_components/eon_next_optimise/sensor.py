"""Read-only Optimise measurements, forecasts and operational status."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import DataError, parse_time

FORECAST_HORIZON = timedelta(days=2)
MAX_ATTRIBUTE_PERIODS = 100
MAX_ATTRIBUTE_DEVICES = 32
DEFAULT_MAX_AGES = {
    "prices": timedelta(minutes=15),
    "live": timedelta(minutes=3),
    "plan": timedelta(minutes=15),
    "status": timedelta(minutes=45),
    "ev": timedelta(minutes=3),
    "ev_plan": timedelta(minutes=15),
    "ev_history": timedelta(minutes=45),
}


def finite_number(value: Any) -> bool:
    """Do not turn missing, non-finite or boolean values into measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_instant(value: Any) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def maximum_age(coordinator) -> timedelta:
    configured = getattr(coordinator, "max_age", None)
    if isinstance(configured, timedelta) and configured > timedelta(0):
        return configured
    return DEFAULT_MAX_AGES.get(getattr(coordinator, "section", ""), timedelta(minutes=15))


def section_is_fresh(coordinator, now: datetime | None = None) -> bool:
    """Each independent section owns its own success and freshness state."""
    now = now or dt_util.utcnow()
    success = getattr(coordinator, "last_success", None)
    return bool(
        getattr(coordinator, "last_update_success", False)
        and valid_instant(success)
        and timedelta(0) <= now - success < maximum_age(coordinator)
    )


def _data(coordinator) -> dict:
    return coordinator.data if isinstance(coordinator.data, dict) else {}


def _periods(rows: Any) -> list[tuple[datetime, datetime, dict]]:
    """Inspect validated model rows without filling missing periods."""
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start, end = parse_time(row.get("valid_from")), parse_time(row.get("valid_to"))
        except DataError:
            continue
        if end > start:
            result.append((start, end, row))
    return sorted(result, key=lambda item: item[0])


def _remaining_periods(rows, now):
    limit = now + FORECAST_HORIZON
    return [item for item in _periods(rows) if item[1] > now and item[0] < limit][
        :MAX_ATTRIBUTE_PERIODS
    ]


def _price_attribute(row: dict) -> dict:
    """Publish known model fields, never an arbitrary provider object."""
    result = {key: row.get(key) for key in ("valid_from", "valid_to")}
    for key in ("value", "raw_value", "range_min", "range_max", "renewable_percentage"):
        value = row.get(key)
        result[key] = value if finite_number(value) else None
    for key in ("estimate", "demand_window"):
        value = row.get(key)
        result[key] = value if isinstance(value, bool) else None
    result["quality"] = (
        row.get("quality") if row.get("quality") in {"history", "current", "forecast"} else None
    )
    result["interval_basis"] = (
        row.get("interval_basis")
        if row.get("interval_basis") in {"provider_end", "provisional_30m"}
        else None
    )
    return result


def _safe_text(value: Any, limit: int = 160) -> str | None:
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _plan_attribute(row: dict) -> dict:
    result = {key: row.get(key) for key in ("valid_from", "valid_to")}
    for key in (
        "general_price",
        "feed_in_price",
        "battery_energy_wh",
        "solar_power_w",
        "load_power_w",
    ):
        value = row.get(key)
        result[key] = value if finite_number(value) else None
    result["actions"] = [
        text for value in (row.get("actions") or [])[:8] if (text := _safe_text(value))
    ]
    result["decisions"] = [
        {key: _safe_text(item.get(key)) for key in ("type", "action", "reason")}
        for item in (row.get("decisions") or [])[:8]
        if isinstance(item, dict)
    ]
    return result


class OptimiseEntity(CoordinatorEntity):
    """One site and one independent polling section."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, site_label: str, key: str):
        super().__init__(coordinator)
        identity = f"{coordinator.entry.entry_id}:{coordinator.site_id}"
        self._attr_unique_id = f"{identity}:{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=site_label,
            manufacturer="E.ON Next / Amber",
            model="Read-only Optimise telemetry",
        )
        self._cancel_scheduled_update = None

    @property
    def available(self):
        return section_is_fresh(self.coordinator)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_timer)
        self._schedule_update()

    @callback
    def _cancel_timer(self):
        if self._cancel_scheduled_update is not None:
            self._cancel_scheduled_update()
            self._cancel_scheduled_update = None

    def _change_instants(self, now):
        success = getattr(self.coordinator, "last_success", None)
        return [success + maximum_age(self.coordinator)] if valid_instant(success) else []

    @callback
    def _schedule_update(self):
        self._cancel_timer()
        now = dt_util.utcnow()
        future = [point for point in self._change_instants(now) if point > now]
        if future:
            self._cancel_scheduled_update = async_track_point_in_utc_time(
                self.hass, self._scheduled_update, min(future)
            )

    @callback
    def _scheduled_update(self, _now):
        self._cancel_scheduled_update = None
        self._schedule_update()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self):
        self._schedule_update()
        super()._handle_coordinator_update()


class OptimisePriceSensor(OptimiseEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_icon = "mdi:currency-gbp"

    def __init__(self, coordinator, site_label, direction, *, next_period=False):
        self.direction = direction
        self.next_period = next_period
        prefix = "next_" if next_period else ""
        super().__init__(coordinator, site_label, f"{prefix}{direction}_price")
        self.entity_description = SensorEntityDescription(
            key=f"{prefix}{direction}_price",
            name=f"{'Next ' if next_period else ''}{direction} price".capitalize(),
        )

    def _row(self, now):
        periods = _periods(_data(self.coordinator).get(self.direction))
        current = next((item for item in periods if item[0] <= now < item[1]), None)
        if not self.next_period:
            return current[2] if current else None
        # A distant future quote must not fill an unknown next interval.
        return next((row for start, _end, row in periods if current and start == current[1]), None)

    @property
    def available(self):
        now = dt_util.utcnow()
        row = self._row(now)
        return bool(
            section_is_fresh(self.coordinator, now) and row and finite_number(row.get("value"))
        )

    @property
    def native_value(self):
        now = dt_util.utcnow()
        row = self._row(now)
        return (
            row["value"]
            if section_is_fresh(self.coordinator, now) and row and finite_number(row.get("value"))
            else None
        )

    def _change_instants(self, now):
        return super()._change_instants(now) + [
            point
            for start, end, _row in _periods(_data(self.coordinator).get(self.direction))
            for point in (start, end)
        ]

    @property
    def extra_state_attributes(self):
        now = dt_util.utcnow()
        fresh = section_is_fresh(self.coordinator, now)
        row = self._row(now) if fresh else None
        attrs = {
            "currency": "GBP",
            "price_basis": "import_cost" if self.direction == "import" else "export_earnings",
            "forecast_is_settlement": False,
            "period": _price_attribute(row) if row else None,
        }
        if not self.next_period:
            attrs["forecast"] = [
                _price_attribute(item)
                for start, _end, item in _remaining_periods(
                    _data(self.coordinator).get(self.direction), now
                )
                if fresh and start > now
            ]
        return attrs


@dataclass(frozen=True, slots=True)
class ValueSpec:
    key: str
    field: str
    name: str
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    text: bool = False


LIVE_SENSORS = (
    ValueSpec(
        "battery_soc",
        "stateOfChargePercentage",
        "Battery state of charge",
        PERCENTAGE,
        SensorDeviceClass.BATTERY,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "site_power",
        "sitePowerW",
        "Grid power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "solar_power",
        "solarPowerW",
        "Solar power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "load_power",
        "fixedLoadPowerW",
        "Home load power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "battery_power",
        "batteryPowerW",
        "Battery power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "grid_import_power",
        "gridImportPowerW",
        "Grid import power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "grid_export_power",
        "gridExportPowerW",
        "Grid export power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "battery_charge_power",
        "batteryChargePowerW",
        "Battery charge power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "battery_discharge_power",
        "batteryDischargePowerW",
        "Battery discharge power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "battery_energy",
        "batteryEnergyWh",
        "Battery stored energy",
        UnitOfEnergy.WATT_HOUR,
        SensorDeviceClass.ENERGY_STORAGE,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "battery_capacity",
        "batteryMaxEnergyWh",
        "Battery energy capacity",
        UnitOfEnergy.WATT_HOUR,
        SensorDeviceClass.ENERGY_STORAGE,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "renewable_percentage",
        "currentRenewablePercentage",
        "Current renewable percentage",
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec("power_state", "powerState", "Power state", text=True),
)
METRIC_SENSORS = (
    ValueSpec(
        "interval_grid_import_energy",
        "siteImportKWh",
        "Interval grid import energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_grid_export_energy",
        "siteExportKWh",
        "Interval grid export energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_grid_net_energy",
        "siteNetKWh",
        "Interval grid net energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_battery_import_energy",
        "batteryImportKWh",
        "Interval battery import energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_battery_export_energy",
        "batteryExportKWh",
        "Interval battery export energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_battery_net_energy",
        "batteryNetKWh",
        "Interval battery net energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_solar_export_energy",
        "solarExportKWh",
        "Interval solar export energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_solar_import_energy",
        "solarImportKWh",
        "Interval solar import energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec(
        "interval_home_load_energy",
        "fixedLoadNetKWh",
        "Interval home load energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
    ),
    ValueSpec("interval_import_cost", "importCostsCents", "Interval import cost", "GBP"),
    ValueSpec("interval_export_earnings", "exportEarningsCents", "Interval export earnings", "GBP"),
    ValueSpec("interval_total_earnings", "totalEarningsCents", "Interval total earnings", "GBP"),
)
STATUS_SENSORS = (
    ValueSpec("strategy_status", "strategy_status", "Battery strategy status", text=True),
    ValueSpec(
        "device_count",
        "device_count",
        "Connected devices",
        None,
        None,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "device_error_count",
        "error_count",
        "Devices with errors",
        None,
        None,
        SensorStateClass.MEASUREMENT,
    ),
)
PLAN_SENSORS = (
    ValueSpec(
        "forecast_battery_energy",
        "battery_energy_wh",
        "Forecast battery energy",
        UnitOfEnergy.WATT_HOUR,
        SensorDeviceClass.ENERGY_STORAGE,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "forecast_solar_power",
        "solar_power_w",
        "Forecast solar power",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    ValueSpec(
        "forecast_load_power",
        "load_power_w",
        "Forecast home load",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
)


class OptimiseValueSensor(OptimiseEntity, SensorEntity):
    def __init__(self, coordinator, site_label, description):
        super().__init__(coordinator, site_label, description.key)
        self.description = description
        self.entity_description = SensorEntityDescription(
            key=description.key, name=description.name
        )
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class

    def _value(self):
        value = _data(self.coordinator).get(self.description.field)
        return _safe_text(value, 128) if self.description.text else value

    @property
    def available(self):
        value = self._value()
        return bool(
            super().available
            and (value is not None if self.description.text else finite_number(value))
        )

    @property
    def native_value(self):
        return self._value() if self.available else None

    @property
    def extra_state_attributes(self):
        if self.description.key != "device_count" or not super().available:
            return None
        devices = _data(self.coordinator).get("devices")
        if not isinstance(devices, list):
            return None
        return {
            "devices": [
                {key: _safe_text(device.get(key)) for key in ("type", "brand", "model", "status")}
                | {
                    key: device.get(key) if isinstance(device.get(key), bool) else None
                    for key in ("has_error", "onboarding")
                }
                for device in devices[:MAX_ATTRIBUTE_DEVICES]
                if isinstance(device, dict)
            ]
        }


class OptimiseMetricSensor(OptimiseValueSensor):
    """Interval statistics, without guessing daily resets or lifetime totals."""

    def _interval(self):
        data = _data(self.coordinator)
        try:
            start = parse_time(data.get("metrics_interval_start"))
            end = parse_time(data.get("metrics_interval_end"))
        except DataError:
            return None
        return (start, end) if end > start else None

    def _value(self):
        value = super()._value()
        if not finite_number(value):
            return None
        return value / 100 if self.description.field.endswith("Cents") else value

    @property
    def available(self):
        interval = self._interval()
        now = dt_util.utcnow()
        return bool(
            super().available
            and _data(self.coordinator).get("metrics_error_type") is None
            and interval
            and interval[0] <= now < interval[1]
        )

    def _change_instants(self, now):
        interval = self._interval()
        return super()._change_instants(now) + (list(interval) if interval else [])

    @property
    def extra_state_attributes(self):
        interval = self._interval() if self.available else None
        return {
            "interval_start": interval[0].isoformat() if interval else None,
            "interval_end": interval[1].isoformat() if interval else None,
            "interval_statistics": True,
        }


class OptimiseReserveSensor(OptimiseEntity, SensorEntity):
    entity_description = SensorEntityDescription(
        key="minimum_reserve", name="Battery minimum reserve"
    )
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-lock"

    def __init__(self, coordinator, site_label):
        super().__init__(coordinator, site_label, "minimum_reserve")

    def _levels(self):
        values = _data(self.coordinator).get("minimum_reserve")
        if not isinstance(values, list):
            return []
        return [
            value if finite_number(value) and 0 <= value <= 100 else None
            for value in values[:MAX_ATTRIBUTE_DEVICES]
        ]

    @property
    def available(self):
        levels = self._levels()
        return bool(super().available and len(levels) == 1 and levels[0] is not None)

    @property
    def native_value(self):
        return self._levels()[0] if self.available else None

    @property
    def extra_state_attributes(self):
        return {
            "minimum_reserve_levels": self._levels() if super().available else [],
            "aggregation": "single_battery_only",
        }


class OptimisePlanSensor(OptimiseValueSensor):
    def _row(self):
        now = dt_util.utcnow()
        return next(
            (
                row
                for _start, _end, row in _remaining_periods(
                    _data(self.coordinator).get("forecast"), now
                )
            ),
            None,
        )

    def _value(self):
        row = self._row()
        return row.get(self.description.field) if row else None

    def _change_instants(self, now):
        return super()._change_instants(now) + [
            point
            for start, end, _row in _periods(_data(self.coordinator).get("forecast"))
            for point in (start, end)
        ]

    @property
    def extra_state_attributes(self):
        row = self._row() if super().available else None
        return {"period": _plan_attribute(row) if row else None, "forecast_is_measurement": False}


class OptimisePlanStatusSensor(OptimiseEntity, SensorEntity):
    entity_description = SensorEntityDescription(key="plan_status", name="Plan status")

    def __init__(self, coordinator, site_label):
        super().__init__(coordinator, site_label, "plan_status")

    @property
    def native_value(self):
        if not self.available:
            return None
        error = _data(self.coordinator).get("error_type")
        if error:
            return (
                error
                if isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", error)
                else "data_error"
            )
        return "forecast_available" if self._rows() else "no_forecast"

    def _rows(self):
        return _remaining_periods(_data(self.coordinator).get("forecast"), dt_util.utcnow())

    def _change_instants(self, now):
        return super()._change_instants(now) + [
            point for start, end, _row in self._rows() for point in (start, end)
        ]

    @property
    def extra_state_attributes(self):
        return {
            "forecast": [_plan_attribute(row) for _start, _end, row in self._rows()]
            if self.available
            else [],
            "forecast_is_settlement": False,
        }


class OptimiseLastUpdateSensor(OptimiseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, site_label):
        section = coordinator.section
        super().__init__(coordinator, site_label, f"{section}_last_update")
        self.entity_description = SensorEntityDescription(
            key=f"{section}_last_update", name=f"{section.title()} last successful update"
        )

    @property
    def native_value(self):
        return self.coordinator.last_success if self.available else None


async def async_setup_entry(hass, entry, async_add_entities):
    entities = []
    for index, (_site_id, site) in enumerate(entry.runtime_data.sites.items(), 1):
        label = f"Optimise site {index}"
        entities.extend(
            OptimisePriceSensor(site.prices, label, direction, next_period=upcoming)
            for direction in ("import", "export")
            for upcoming in (False, True)
        )
        entities.extend(OptimiseValueSensor(site.live, label, spec) for spec in LIVE_SENSORS)
        entities.extend(OptimiseMetricSensor(site.live, label, spec) for spec in METRIC_SENSORS)
        entities.extend(OptimiseValueSensor(site.status, label, spec) for spec in STATUS_SENSORS)
        entities.append(OptimiseReserveSensor(site.status, label))
        entities.extend(OptimisePlanSensor(site.plan, label, spec) for spec in PLAN_SENSORS)
        entities.append(OptimisePlanStatusSensor(site.plan, label))
        entities.extend(
            OptimiseLastUpdateSensor(getattr(site, section), label) for section in DEFAULT_MAX_AGES
        )
    async_add_entities(entities)
    # Relative Python import; only the standard sensor platform is registered.
    from .ev_sensor import async_setup_ev_sensors

    await async_setup_ev_sensors(entry, async_add_entities)
