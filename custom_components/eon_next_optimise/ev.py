"""Validate EV telemetry selected from the official app's read-only evLive query.

Vehicle identities remain internal to the coordinator/device registry. This
module does not retain VINs, location, account fields, commands or provider
messages. Missing measurements remain missing, including connectivity: an
online vehicle does not establish whether its charging cable is plugged in.
"""

from typing import Any

from .models import MAX_DEVICES, DataError, _boolean, _data, _list, _number, _object, _text


def _label(value: Any, limit: int = 128) -> str | None:
    """Accept bounded printable telemetry, preserving its provider wording."""
    result = _text(value, limit)
    if result is None:
        return None
    if not result.strip():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise DataError("Invalid EV text telemetry")
    return result


def parse_ev(payload: Any) -> dict[str, Any]:
    """Reduce a GraphQL envelope to bounded telemetry without inferring state.

    Charging speed stays a provider label: no numeric power unit can safely be
    inferred from arbitrary display text. The site price fields retain pence/kWh,
    with feed-in negated to the integration's positive-export-earnings convention.
    """
    live = _object(_data(payload).get("evLive"))
    information = _object(live.get("siteInformation"))
    vehicles = []
    seen = set()
    for raw in _list(live.get("vehicles"), MAX_DEVICES):
        row = _object(raw)
        identity = _label(row.get("entityId"), 256)
        if identity is None or identity in seen:
            raise DataError("Invalid or duplicate EV identity")
        seen.add(identity)
        vehicles.append(
            {
                "entity_id": identity,
                "state_of_charge": _number(row.get("batteryLevel"), percentage=True),
                "live_state": _label(row.get("liveState")),
                "connectivity": _label(row.get("connectivity")),
                "connectivity_label": _label(row.get("connectivityLabel"), 256),
                "charging_speed_label": _label(row.get("chargingSpeedLabel"), 256),
                "make": _label(row.get("makeId")),
                "model": _label(row.get("modelName")),
                "ready_by_eligible": _boolean(row.get("readyByEligible")),
            }
        )
    raw_export = _number(information.get("feedInPriceCents"))
    return {
        "status": _label(live.get("status")),
        "vehicles": vehicles,
        "vehicle_count": len(vehicles) if isinstance(live.get("vehicles"), list) else None,
        "site": {
            "import_price": _number(information.get("generalPriceCents")),
            "export_price": -raw_export if raw_export is not None else None,
            "renewable_percentage": _number(
                information.get("renewablePercentage"), percentage=True
            ),
            "in_demand_window": _boolean(information.get("isInDemandWindow")),
        },
    }
