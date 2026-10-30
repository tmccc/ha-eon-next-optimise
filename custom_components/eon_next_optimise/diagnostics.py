"""Allow-list operational diagnostics without account/site/device identities."""

from homeassistant.util import dt as dt_util

from .api import OptimiseClient
from .const import SECTIONS
from .exceptions import STATUS_ROOTS, ProviderErrorInfo
from .sensor import _data, finite_number, section_is_fresh, valid_instant


def _error_category(coordinator):
    """Never stringify an exception that may contain provider or login data."""
    category = getattr(coordinator, "last_error_class", None)
    if category in {"authentication", "rate_limited", "connection", "invalid_data", "dependency"}:
        return category
    error = getattr(coordinator, "last_exception", None)
    if error is None:
        return None
    name = type(error).__name__
    if name in {"AuthenticationError", "ConfigEntryAuthFailed"}:
        return "authentication"
    if name in {"TimeoutError", "ServerTimeoutError"}:
        return "timeout"
    if name in {"UpdateFailed", "DataError", "TelemetryError", "PriceFeedError"}:
        return "update"
    return "unknown"


def _count(value):
    return len(value) if isinstance(value, list) else 0


def _provider_error(coordinator, section):
    """Read one cache slot; never inspect credentials or perform a request."""
    client = getattr(coordinator, "client", None)
    if not isinstance(client, OptimiseClient):
        return None
    info = client.last_error_info(section, getattr(coordinator, "site_id", None))
    return info.as_dict() if isinstance(info, ProviderErrorInfo) else None


def _vehicle_scope(rows):
    """Compare private fleet identities internally; never include them in output."""
    if not isinstance(rows, list):
        return None
    identities = [row.get("entity_id") if isinstance(row, dict) else None for row in rows]
    if any(not isinstance(identity, str) or not identity for identity in identities):
        return None
    unique = frozenset(identities)
    return unique if len(unique) == len(identities) else None


def _section_diagnostics(coordinator, section, now):
    success = getattr(coordinator, "last_success", None)
    age = (now - success).total_seconds() if valid_instant(success) else None
    data = _data(coordinator)
    result = {
        "last_update_success": bool(getattr(coordinator, "last_update_success", False)),
        "fresh": section_is_fresh(coordinator, now),
        "last_success": success.isoformat() if valid_instant(success) else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "error_category": _error_category(coordinator),
        "provider_error": _provider_error(coordinator, section),
    }
    if section in {"status", "ev_plan", "ev_history"}:
        provider_error = result["provider_error"]
        allowed_roots = (
            set(STATUS_ROOTS.values())
            if section == "status"
            else ({"evTimeline", "evReadyByState"} if section == "ev_plan" else {"evDailySummary"})
        )
        failed_roots = {
            root for root in (provider_error or {}).get("failed_roots", []) if root in allowed_roots
        }
        result["all_fields_current"] = bool(
            result["fresh"] and result["last_update_success"] and provider_error is None
        )
        result["partial_data"] = bool(
            provider_error is not None and provider_error.get("category") == "partial"
        )
        result["failed_root_count"] = (
            len(failed_roots) if failed_roots else (0 if provider_error is None else None)
        )
    if section == "prices":
        result["period_counts"] = {
            direction: _count(data.get(direction)) for direction in ("import", "export")
        }
    elif section == "live":
        result["numeric_fields_available"] = sum(finite_number(value) for value in data.values())
    elif section == "plan":
        result["forecast_periods"] = _count(data.get("forecast"))
        result["has_data_error"] = bool(data.get("error_type"))
    elif section == "status":
        result["devices_reported"] = _count(data.get("devices"))
        result["reserve_settings_reported"] = _count(data.get("minimum_reserve"))
        errors = data.get("error_count")
        result["device_error_count"] = errors if finite_number(errors) else None
    elif section == "ev":
        vehicles = data.get("vehicles")
        count = data.get("vehicle_count")
        known_vehicles = (
            isinstance(vehicles, list)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count == len(vehicles)
        )
        result["vehicle_count"] = count if known_vehicles else None
        result["vehicles_with_soc"] = (
            sum(
                isinstance(vehicle, dict) and finite_number(vehicle.get("state_of_charge"))
                for vehicle in vehicles
            )
            if known_vehicles
            else None
        )
    elif section in {"ev_plan", "ev_history"}:
        rows, count = data.get("vehicles"), data.get("vehicle_count")
        known = isinstance(rows, list) and type(count) is int and count == len(rows)
        dependency = getattr(coordinator, "current_fleet", None)
        fleet = dependency() if callable(dependency) else None
        result["dependency_current"] = fleet is not None
        current_scope = _vehicle_scope(fleet)
        response_scope = _vehicle_scope(rows) if known else None
        known = bool(known and current_scope is not None and response_scope == current_scope)
        modules = ("timeline", "ready_by") if section == "ev_plan" else ("history",)
        values = (
            [row.get(module) for row in rows for module in modules if isinstance(row, dict)]
            if known
            else []
        )
        values = [value for value in values if isinstance(value, dict)]
        result["all_fields_current"] = result["all_fields_current"] and known
        result["vehicle_count"] = count if known else None
        result["expected_module_count"] = count * len(modules) if known else None
        result["successful_module_count"] = (
            sum(value.get("query_success") is True for value in values) if known else None
        )
        result["all_fields_current"] = bool(
            result["all_fields_current"]
            and known
            and len(values) == count * len(modules)
            and result["successful_module_count"] == result["expected_module_count"]
        )
        result["reported_module_count"] = (
            sum(
                value.get("query_success") is True and value.get("reported") is True
                for value in values
            )
            if known
            else None
        )
        collection = "intervals" if section == "ev_plan" else "days"
        module = "timeline" if section == "ev_plan" else "history"
        reports = [row.get(module) for row in rows if isinstance(row, dict)] if known else []
        reports = [
            value
            for value in reports
            if isinstance(value, dict)
            and value.get("query_success") is True
            and value.get("reported") is True
            and isinstance(value.get(collection), list)
        ]
        result[
            "forecast_periods_reported" if section == "ev_plan" else "history_day_rows_reported"
        ] = (
            sum(len(value[collection]) for value in reports)
            if reports or known and count == 0
            else None
        )
    return result


async def async_get_config_entry_diagnostics(hass, entry):
    """Return counts/freshness and safe cached errors, without entry data or IO."""
    now = dt_util.utcnow()
    sites = []
    for index, site in enumerate(entry.runtime_data.sites.values(), 1):
        sites.append(
            {
                "index": index,
                "sections": {
                    section: _section_diagnostics(getattr(site, section), section, now)
                    for section in SECTIONS
                },
            }
        )
    return {"site_count": len(sites), "sites": sites}
