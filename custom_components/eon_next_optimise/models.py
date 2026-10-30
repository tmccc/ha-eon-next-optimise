"""Validate telemetry into bounded, JSON-safe data without HA or network access.

Prices retain API pence/kWh; feed-in values become export earnings by negation.
Official app static code supports those units and the power directions below.
No additional VAT or account-specific adjustment is applied.
"""

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_PERIODS = 512
MAX_DEVICES = 128
MAX_SITES = 128
MAX_TEXT = 512
EXPECTED_PRICE_INTERVAL = timedelta(minutes=30)


class DataError(Exception):
    """A response cannot safely be represented as telemetry."""


def _object(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DataError("Expected a telemetry object")
    return value


def _list(value: Any, limit: int = MAX_PERIODS) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise DataError("Invalid telemetry collection")
    return value


def _number(value: Any, *, percentage: bool = False) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError("Invalid numeric telemetry")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (percentage and not 0 <= value <= 100):
        raise DataError("Numeric telemetry outside supported range")
    return value


def _boolean(value: Any) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise DataError("Invalid boolean telemetry")
    return value


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise DataError("Invalid text telemetry")
    return value


def _data(payload: Any) -> Mapping[str, Any]:
    envelope = _object(payload)
    if envelope.get("errors"):
        raise DataError("GraphQL operation failed")
    if not isinstance(envelope.get("data"), Mapping):
        raise DataError("Missing GraphQL telemetry data")
    return envelope["data"]


def parse_time(value: Any) -> datetime:
    """Parse a timezone-aware API instant and normalize it to UTC."""
    if not isinstance(value, str) or len(value) > 64:
        raise DataError("Invalid telemetry timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as err:
        raise DataError("Invalid telemetry timestamp") from err


def parse_sites(payload: Any) -> list[dict[str, Any]]:
    """Discover accessible sites without changing the app's selected site."""
    settings = _object(_data(payload).get("settings"))
    selected = _text(settings.get("selectedSiteId"), 256)
    statuses = {}
    for row in _list(settings.get("sites"), MAX_SITES):
        row = _object(row)
        site_id = _text(row.get("siteId"), 256)
        if not site_id:
            raise DataError("Missing site identity")
        if site_id in statuses:
            raise DataError("Duplicate site identity")
        statuses[site_id] = _text(row.get("siteStatus"))
    ids = _list(settings.get("siteIds"), MAX_SITES)
    # sites may carry the accessible identities when siteIds is null/absent.
    if not ids:
        ids = list(statuses)
    result = []
    seen = set()
    for value in ids:
        site_id = _text(value, 256)
        if not site_id or site_id in seen:
            raise DataError("Invalid or duplicate site identity")
        seen.add(site_id)
        result.append(
            {"site_id": site_id, "selected": site_id == selected, "status": statuses.get(site_id)}
        )
    return result


def _range(period: Mapping[str, Any], export: bool) -> tuple[Any, Any]:
    limits = _object(period.get("kwhPriceRange"))
    low = _number(limits.get("minInCents"))
    high = _number(limits.get("maxInCents"))
    if low is not None and high is not None and low > high:
        raise DataError("Invalid price range")
    if export:
        return (-high if high is not None else None, -low if low is not None else None)
    return low, high


def parse_prices(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Parse each price direction independently; keep gaps and source provenance."""
    pricing = _object(_data(payload).get("sitePricing"))
    result: dict[str, list[dict[str, Any]]] = {"import": [], "export": []}
    seen = set()
    for window in _list(pricing.get("meterWindows"), MAX_DEVICES):
        window = _object(window)
        direction = {"GENERAL": "import", "FEED_IN": "export"}.get(_text(window.get("usageType")))
        if direction is None:
            continue
        if direction in seen:
            raise DataError("Ambiguous duplicate price channel")
        seen.add(direction)
        periods: dict[datetime, dict[str, Any]] = {}
        for key, quality in (
            ("previousPeriods", "history"),
            ("forecastPeriods", "forecast"),
            ("currentPeriod", "current"),
        ):
            entries = [window.get(key)] if key == "currentPeriod" else _list(window.get(key))
            for period in entries:
                if period is None:
                    continue
                period = _object(period)
                start = parse_time(period.get("start"))
                explicit_end = period.get("end")
                if explicit_end is None:
                    if start.minute not in (0, 30) or start.second or start.microsecond:
                        raise DataError("Unverified price interval duration")
                    try:
                        end = start + EXPECTED_PRICE_INTERVAL
                    except OverflowError as err:
                        raise DataError("Invalid price interval") from err
                else:
                    end = parse_time(explicit_end)
                if end <= start or end - start > timedelta(days=1):
                    raise DataError("Invalid price interval")
                raw = _number(period.get("kwhPriceInCents"))
                value = -raw if raw is not None and direction == "export" else raw
                low, high = _range(period, direction == "export")
                row = {
                    "valid_from": start.isoformat(),
                    "valid_to": end.isoformat(),
                    "value": value,
                    "raw_value": raw,
                    "quality": quality,
                    "estimate": _boolean(period.get("estimate")),
                    "range_min": low,
                    "range_max": high,
                    "renewable_percentage": _number(
                        period.get("renewablePercentage"), percentage=True
                    ),
                    "indicator": _text(period.get("indicator")),
                    "demand_window": _boolean(period.get("demandWindow")),
                    "interval_basis": "provider_end"
                    if explicit_end is not None
                    else "provisional_30m",
                }
                if start in periods:
                    previous = periods[start]
                    if any(
                        previous[field] != row[field]
                        for field in ("value", "valid_to", "range_min", "range_max")
                    ):
                        raise DataError("Conflicting duplicate price interval")
                periods[start] = row
                if len(periods) > MAX_PERIODS:
                    raise DataError("Too many price intervals")
        rows = [periods[start] for start in sorted(periods)]
        for previous, current in zip(rows, rows[1:], strict=False):
            if parse_time(current["valid_from"]) < parse_time(previous["valid_to"]):
                raise DataError("Overlapping or unverified price intervals")
        result[direction] = rows
    return result


LIVE_NUMBERS = (
    "sitePowerW",
    "solarPowerW",
    "fixedLoadPowerW",
    "batteryPowerW",
    "batteryEnergyWh",
    "batteryMaxEnergyWh",
    "stateOfChargePercentage",
    "currentGeneralUsagePrice",
    "currentFeedInPrice",
    "currentRenewablePercentage",
)
LIVE_TEXT = (
    "powerState",
    "powerStateDescription",
    "powerStateAction",
    "powerStateActionTitle",
    "powerStateActionDescription",
)
LIVE_METRICS = (
    "importCostsCents",
    "exportEarningsCents",
    "totalEarningsCents",
    "siteImportKWh",
    "siteExportKWh",
    "siteNetKWh",
    "batteryImportKWh",
    "batteryExportKWh",
    "batteryNetKWh",
    "solarExportKWh",
    "solarImportKWh",
    "fixedLoadNetKWh",
)


def _live_metrics(value: Any) -> dict[str, Any]:
    """An absent or failed optional metrics union must not hide valid power/SOC."""
    result = dict.fromkeys(LIVE_METRICS)
    result.update(metrics_interval_start=None, metrics_interval_end=None, metrics_error_type=None)
    try:
        metrics = _object(value)
        if not metrics:
            return result
        kind = _text(metrics.get("__typename"), 128)
        if kind == "SmartShiftDataError":
            result["metrics_error_type"] = _error_type(metrics) or "data_error"
            return result
        if kind != "SmartShiftMetricsWithInterval":
            result["metrics_error_type"] = "unsupported_data_type"
            return result
        start = (
            parse_time(metrics["intervalStart"])
            if metrics.get("intervalStart") is not None
            else None
        )
        end = parse_time(metrics["intervalEnd"]) if metrics.get("intervalEnd") is not None else None
        if start is not None and end is not None and end <= start:
            raise DataError("Invalid metrics interval")
        numbers = {key: _number(metrics.get(key)) for key in LIVE_METRICS}
    except DataError:
        result["metrics_error_type"] = "invalid_metrics"
        return result
    result.update(numbers)
    result["metrics_interval_start"] = start.isoformat() if start is not None else None
    result["metrics_interval_end"] = end.isoformat() if end is not None else None
    return result


def parse_live(payload: Any) -> dict[str, Any]:
    """Keep raw values and add app-proven non-negative directional power."""
    live = _object(_object(_data(payload).get("smartshift")).get("live"))
    result = {
        key: _number(
            live.get(key),
            percentage=key in {"stateOfChargePercentage", "currentRenewablePercentage"},
        )
        for key in LIVE_NUMBERS
    }
    result.update({key: _text(live.get(key)) for key in LIVE_TEXT})
    result["isSolarCurtailmentActive"] = _boolean(live.get("isSolarCurtailmentActive"))
    for source, positive, negative in (
        ("sitePowerW", "gridImportPowerW", "gridExportPowerW"),
        ("batteryPowerW", "batteryDischargePowerW", "batteryChargePowerW"),
    ):
        value = result[source]
        result[positive] = max(value, 0) if value is not None else None
        result[negative] = max(-value, 0) if value is not None else None
    result.update(_live_metrics(live.get("liveMetrics")))
    return result


def _error_type(value: Any) -> str | None:
    return _text(_object(value).get("type"), 128)


def parse_plan(payload: Any) -> dict[str, Any]:
    """Decode forecast union types without publishing provider error bodies."""
    plan = _object(_object(_data(payload).get("smartshift")).get("plan"))
    data = _object(plan.get("forecastData"))
    result: dict[str, Any] = {"forecast": [], "error_type": None}
    if not data:
        return result
    kind = _text(data.get("__typename"), 128)
    if kind == "SmartShiftDataError":
        result["error_type"] = _error_type(data) or "data_error"
        return result
    if kind != "PastAndForecastData":
        result["error_type"] = "unsupported_data_type"
        return result
    result["error_type"] = _error_type(data.get("forecastError"))
    periods = {}
    for period in _list(data.get("forecast")):
        period = _object(period)
        start = parse_time(period.get("periodStart"))
        end = parse_time(period.get("periodEnd"))
        if end <= start or end - start > timedelta(days=1):
            raise DataError("Invalid plan interval")
        raw_export = _number(period.get("feedInPriceCents"))
        actions = [_text(action) for action in _list(period.get("actionDescriptions"), 32)]
        decisions = []
        for decision in _list(period.get("actionDecisions"), 32):
            decision = _object(decision)
            decisions.append(
                {
                    "type": _text(decision.get("__typename"), 128),
                    "action": _text(decision.get("batteryAction", decision.get("solarAction"))),
                    "reason": _text(decision.get("reason")),
                }
            )
        row = {
            "valid_from": start.isoformat(),
            "valid_to": end.isoformat(),
            "general_price": _number(period.get("generalUsagePriceCents")),
            "feed_in_price": -raw_export if raw_export is not None else None,
            "battery_energy_wh": _number(period.get("batteryEnergyWh")),
            "solar_power_w": _number(period.get("solarPowerW")),
            "load_power_w": _number(period.get("fixedLoadPowerW")),
            "actions": [action for action in actions if action is not None],
            "decisions": decisions,
        }
        if start in periods and periods[start] != row:
            raise DataError("Conflicting duplicate plan interval")
        periods[start] = row
    result["forecast"] = [periods[start] for start in sorted(periods)]
    for previous, current in zip(result["forecast"], result["forecast"][1:], strict=False):
        if parse_time(current["valid_from"]) < parse_time(previous["valid_to"]):
            raise DataError("Overlapping plan intervals")
    return result


def parse_status(payload: Any) -> dict[str, Any]:
    """Read automation/device status; never infer a site-wide battery reserve."""
    data = _data(payload)
    experience = _object(data.get("smartshiftExperience"))
    strategy = _object(data.get("smartshiftBatteryStrategyConfig"))
    settings = _object(data.get("smartshiftUserSettings"))
    curtailment = _object(settings.get("solarCurtailment"))
    summary = _object(data.get("unifiedDevicesSummary"))
    reserves = [
        _number(_object(_object(item).get("minReserveLevel")).get("value"), percentage=True)
        for item in _list(settings.get("battery"), MAX_DEVICES)
    ]
    devices = []
    for device in _list(summary.get("devices"), MAX_DEVICES):
        device = _object(device)
        display = _object(device.get("displayValues"))
        devices.append(
            {
                "type": _text(device.get("deviceTypeEnum")),
                "brand": _text(display.get("brand")),
                "model": _text(display.get("model")),
                "status": _text(display.get("status")),
                "has_error": _boolean(device.get("hasError")),
                "onboarding": _boolean(device.get("isOnboarding")),
            }
        )
    devices_known = isinstance(summary.get("devices"), list)
    errors_known = devices_known and all(device["has_error"] is not None for device in devices)
    return {
        "battery_enrolled": _boolean(experience.get("batteryEnrolled")),
        "solar_enrolled": _boolean(experience.get("solarEnrolled")),
        "ev_enrolled": _boolean(experience.get("evEnrolled")),
        "ev_devices_enrolled": _boolean(experience.get("evDevicesEnrolled")),
        "strategy_status": _text(strategy.get("status")),
        "global_killswitch": _boolean(strategy.get("globalKillswitchEnabled")),
        "curtailment_enabled": _boolean(curtailment.get("enabled")),
        "curtailment_supported": _boolean(curtailment.get("supported")),
        "minimum_reserve": reserves,
        "has_error": _boolean(summary.get("hasError")),
        "devices": devices,
        "device_count": len(devices) if devices_known else None,
        "error_count": sum(device["has_error"] for device in devices) if errors_known else None,
    }


def coverage(rows: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    """Find a current half-open interval and contiguous coverage without filling gaps."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise DataError("Current time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    current = next(
        (row for row in rows if parse_time(row["valid_from"]) <= now < parse_time(row["valid_to"])),
        None,
    )
    if current is None:
        return None, None
    end = parse_time(current["valid_to"])
    for row in rows:
        if parse_time(row["valid_from"]) == end:
            end = parse_time(row["valid_to"])
    return current, end.isoformat()
