"""Pure, read-only EV plans and dated history; no identities or control data."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .ev import _label
from .models import DataError, _boolean, _data, _list, _number, _object, parse_time

MAX_TIMELINE_INPUT = 2048
MAX_TIMELINE_ROWS = 100
TIMELINE_HORIZON = timedelta(hours=48)
MAX_HISTORY_INPUT = 366
MAX_HISTORY_DAYS = 31


def _instant(value: Any) -> str | None:
    return parse_time(value).isoformat() if value is not None else None


def _nonnegative(value: Any) -> float | int | None:
    result = _number(value)
    if result is not None and result < 0:
        raise DataError("Invalid negative EV measurement")
    return result


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise DataError("EV timeline requires an aware current time")
    return now.astimezone(timezone.utc)


def parse_ev_timeline(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Keep explicit valid intervals within 48 hours; never stretch forecast gaps.

    At most 100 earliest current/future intervals are retained. A separate metadata
    flag marks truncation, so consumers cannot mistake a short high-frequency plan
    for complete 48-hour coverage. Prices retain the provider's pence/kWh unit.
    Finite SOC estimates outside 0–100% remain raw data for the app gauge; their
    validated state of charge is unknown without discarding the remaining plan.
    """
    raw = _data(payload).get("evTimeline")
    timeline = _object(raw)
    clock = _clock(now)
    try:
        horizon = clock + TIMELINE_HORIZON
    except OverflowError as err:
        raise DataError("Invalid EV timeline clock") from err
    rows = {}
    current = timeline.get("currentInterval")
    entries = [] if current is None else [(current, "current")]
    entries.extend(
        (row, "forecast") for row in _list(timeline.get("futureIntervals"), MAX_TIMELINE_INPUT)
    )
    for value, quality in entries:
        source = _object(value)
        start = parse_time(source.get("intervalStartTimeIso"))
        minutes = _number(source.get("intervalLengthMinutes"))
        if minutes is None or not 0 < minutes <= 1440:
            raise DataError("Invalid EV interval duration")
        try:
            end = start + timedelta(minutes=minutes)
        except OverflowError as err:
            raise DataError("Invalid EV interval duration") from err
        if end <= start:
            raise DataError("Invalid EV interval duration")
        raw_soc = _number(source.get("intervalStartSocPercent"))
        row = {
            "valid_from": start.isoformat(),
            "valid_to": end.isoformat(),
            "state_of_charge": raw_soc if raw_soc is not None and 0 <= raw_soc <= 100 else None,
            "raw_state_of_charge": raw_soc,
            "price": _number(source.get("generalPriceCents")),
            "source": _label(source.get("chargeEnergySourceEnum")),
            "quality": quality,
        }
        if end <= clock or start >= horizon or end > horizon:
            continue
        if start in rows:
            previous = rows[start]
            if any(previous[key] != row[key] for key in row if key != "quality"):
                raise DataError("Conflicting duplicate EV interval")
            continue  # The current source precedes forecast copies of that interval.
        rows[start] = row
    ordered = [rows[start] for start in sorted(rows)]
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if parse_time(current["valid_from"]) < parse_time(previous["valid_to"]):
            raise DataError("Overlapping EV intervals")
    return {
        "timeline": {
            "reported": raw is not None,
            "intervals": ordered[:MAX_TIMELINE_ROWS],
            "truncated": len(ordered) > MAX_TIMELINE_ROWS,
        }
    }


def parse_ev_ready_by(payload: Any) -> dict[str, Any]:
    """Read the configured target without inferring activity from its presence."""
    raw = _data(payload).get("evReadyByState")
    state = _object(raw)
    raw_config = state.get("oneShotConfig")
    config = _object(raw_config)
    return {
        "ready_by": {
            "reported": raw is not None,
            "configuration_reported": raw_config is not None,
            "target_soc": _number(config.get("targetSocPercent"), percentage=True),
            "target_time": _instant(config.get("targetTime")),
            "created_at": _instant(config.get("createdAt")),
            "cancelled_at": _instant(config.get("cancelledAt")),
            "active": _boolean(config.get("isActive")),
            "recurring": _boolean(config.get("isRecurring")),
        }
    }


def _day(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise DataError("Invalid EV history date")
    try:
        result = date.fromisoformat(value)
    except ValueError as err:
        raise DataError("Invalid EV history date") from err
    if result.isoformat() != value:
        raise DataError("Invalid EV history date")
    return value


def parse_ev_history(payload: Any) -> dict[str, Any]:
    """Keep the latest 31 explicitly reported dates, with no counter semantics."""
    raw = _data(payload).get("evDailySummary")
    history = _object(raw)
    rows = {}
    for value in _list(history.get("dailySummary"), MAX_HISTORY_INPUT):
        source = _object(value)
        day = _day(source.get("date"))
        grid = _object(source.get("grid"))
        solar = _object(source.get("solar"))
        battery = _object(source.get("battery"))
        cost_cents = _number(grid.get("totalCostCents"))
        row = {
            "date": day,
            "home_charge_kwh": _nonnegative(source.get("totalHomeChargeAddedKWh")),
            "away_charge_kwh": _nonnegative(source.get("totalAwayChargeAddedKWh")),
            "grid_charge_kwh": _nonnegative(grid.get("totalAddedKWh")),
            "grid_cost": cost_cents / 100 if cost_cents is not None else None,
            "grid_average_price": _number(grid.get("avgPriceCentsPerKWh")),
            "solar_charge_kwh": _nonnegative(solar.get("totalAddedKWh")),
            "battery_charge_kwh": _nonnegative(battery.get("totalAddedKWh")),
        }
        if day in rows and rows[day] != row:
            raise DataError("Conflicting duplicate EV history date")
        rows[day] = row
    return {
        "history": {
            "reported": raw is not None,
            "nominal_capacity_wh": _nonnegative(history.get("nominalBatteryCapacityWh")),
            "nominal_economy_km_per_kwh": _nonnegative(history.get("nominalEconomyKmPerKWh")),
            "days": [rows[day] for day in sorted(rows)[-MAX_HISTORY_DAYS:]],
        }
    }
