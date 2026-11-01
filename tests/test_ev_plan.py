"""Offline EV plan/history contracts using synthetic, non-account telemetry."""

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eon_next_optimise.ev_plan import (
    MAX_HISTORY_DAYS,
    MAX_HISTORY_INPUT,
    MAX_TIMELINE_INPUT,
    MAX_TIMELINE_ROWS,
    parse_ev_history,
    parse_ev_ready_by,
    parse_ev_timeline,
)
from custom_components.eon_next_optimise.models import DataError
from custom_components.eon_next_optimise.queries import EV_FLEET_ROOTS, OPERATIONS

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def interval(start=None, **fields):
    return {
        "intervalStartTimeIso": (start or NOW).isoformat(),
        "intervalLengthMinutes": 30,
        "intervalStartSocPercent": 69,
        "generalPriceCents": 26,
        "chargeEnergySourceEnum": "Idle",
        **fields,
    }


def timeline(current=None, future=None, **extra):
    return {
        "data": {
            "evTimeline": {
                "currentInterval": current,
                "futureIntervals": future or [],
                **extra,
            }
        }
    }


def ready(**fields):
    return {
        "data": {
            "evReadyByState": {
                "oneShotConfig": {
                    "targetSocPercent": 80,
                    "targetTime": "2026-10-31T07:00:00+01:00",
                    "createdAt": "2026-10-30T11:00:00Z",
                    "cancelledAt": None,
                    "isActive": True,
                    "isRecurring": False,
                    **fields,
                }
            }
        }
    }


def day(date="2026-10-29", **fields):
    return {
        "date": date,
        "totalHomeChargeAddedKWh": 3,
        "totalAwayChargeAddedKWh": 1.5,
        "grid": {"totalAddedKWh": 2, "totalCostCents": 52, "avgPriceCentsPerKWh": 26},
        "solar": {"totalAddedKWh": 0.75},
        "battery": {"totalAddedKWh": 0.25},
        **fields,
    }


def history(days=None, **fields):
    return {
        "data": {
            "evDailySummary": {
                "nominalBatteryCapacityWh": 60000,
                "nominalEconomyKmPerKWh": 6,
                "dailySummary": [day()] if days is None else days,
                **fields,
            }
        }
    }


def selected_names(document):
    selected = re.sub(r"\([^()]*\)", "", document[document.index("{") :])
    return set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", selected))


def test_exact_trimmed_documents_only_query_reviewed_safe_fields():
    expected = {
        "ev_timeline": {
            "evTimeline",
            "currentInterval",
            "futureIntervals",
            "intervalStartTimeIso",
            "intervalStartSocPercent",
            "intervalLengthMinutes",
            "generalPriceCents",
            "chargeEnergySourceEnum",
        },
        "ev_ready_by": {
            "evReadyByState",
            "oneShotConfig",
            "targetSocPercent",
            "targetTime",
            "createdAt",
            "isActive",
            "cancelledAt",
            "isRecurring",
        },
        "ev_history": {
            "evDailySummary",
            "nominalBatteryCapacityWh",
            "nominalEconomyKmPerKWh",
            "dailySummary",
            "date",
            "totalHomeChargeAddedKWh",
            "totalAwayChargeAddedKWh",
            "grid",
            "totalAddedKWh",
            "totalCostCents",
            "avgPriceCentsPerKWh",
            "solar",
            "battery",
        },
    }
    for operation, fields in expected.items():
        document = OPERATIONS[operation]["query"]
        assert document.startswith("query " + OPERATIONS[operation]["operation_name"] + "(")
        assert document.count("query ") == 1
        assert selected_names(document) == fields
        assert "entityId: $entityId" in document
        assert ("$siteId" in document) is (operation != "ev_history")
        assert EV_FLEET_ROOTS[operation] in fields


def test_timeline_preserves_current_and_future_units_source_and_explicit_duration():
    result = parse_ev_timeline(
        timeline(interval(NOW - timedelta(minutes=10)), [interval(NOW + timedelta(minutes=20))]),
        now=NOW,
    )["timeline"]
    assert result["reported"] is True and result["truncated"] is False
    first, second = result["intervals"]
    assert first == {
        "valid_from": "2026-10-30T11:50:00+00:00",
        "valid_to": "2026-10-30T12:20:00+00:00",
        "state_of_charge": 69,
        "raw_state_of_charge": 69,
        "price": 26,
        "source": "Idle",
        "quality": "current",
    }
    assert second["quality"] == "forecast" and second["valid_from"] == first["valid_to"]


def test_timeline_filters_finished_and_beyond_horizon_without_extending_gaps():
    future = [
        interval(NOW - timedelta(hours=1)),
        interval(NOW + timedelta(hours=1)),
        interval(NOW + timedelta(hours=47, minutes=30)),
        interval(NOW + timedelta(hours=48)),
        interval(NOW + timedelta(hours=47, minutes=50)),
    ]
    rows = parse_ev_timeline(timeline(future=future), now=NOW)["timeline"]["intervals"]
    assert len(rows) == 2
    assert rows[0]["valid_from"] == "2026-10-30T13:00:00+00:00"
    assert rows[0]["valid_to"] == "2026-10-30T13:30:00+00:00"
    assert rows[-1]["valid_to"] == "2026-11-01T12:00:00+00:00"


def test_timeline_bounded_rows_mark_truncation_for_high_frequency_forecasts():
    rows = [interval(NOW + timedelta(minutes=i), intervalLengthMinutes=1) for i in range(110)]
    result = parse_ev_timeline(timeline(future=rows), now=NOW)["timeline"]
    assert len(result["intervals"]) == MAX_TIMELINE_ROWS
    assert result["truncated"] is True


def test_distant_soc_above_capacity_does_not_discard_current_or_valid_forecasts():
    future = [
        interval(
            NOW + timedelta(minutes=5 * index),
            intervalLengthMinutes=5,
            intervalStartSocPercent=70 if index < 100 else 100.08902189553524,
        )
        for index in range(1, 289)
    ]
    result = parse_ev_timeline(timeline(interval(intervalLengthMinutes=5), future), now=NOW)[
        "timeline"
    ]
    assert result["reported"] is True and result["truncated"] is True
    assert len(result["intervals"]) == MAX_TIMELINE_ROWS
    assert result["intervals"][0]["state_of_charge"] == 69
    assert result["intervals"][0]["quality"] == "current"
    assert all(row["state_of_charge"] == 70 for row in result["intervals"][1:])


@pytest.mark.parametrize("soc", [-0.2, -1, 100.08902189553524, 101])
def test_out_of_range_soc_is_unknown_without_losing_raw_app_estimate_or_other_fields(soc):
    result = parse_ev_timeline(timeline(interval(intervalStartSocPercent=soc)), now=NOW)["timeline"]
    row = result["intervals"][0]
    assert row["state_of_charge"] is None
    assert row["raw_state_of_charge"] == soc
    assert row["price"] == 26 and row["source"] == "Idle"
    assert row["valid_to"] == (NOW + timedelta(minutes=30)).isoformat()


@pytest.mark.parametrize("soc", [0, 100])
def test_soc_percentage_bounds_remain_valid(soc):
    row = parse_ev_timeline(timeline(interval(intervalStartSocPercent=soc)), now=NOW)["timeline"][
        "intervals"
    ][0]
    assert row["state_of_charge"] == row["raw_state_of_charge"] == soc


def test_timeline_current_duplicate_wins_only_when_values_agree():
    result = parse_ev_timeline(timeline(interval(), [interval()]), now=NOW)["timeline"]
    assert len(result["intervals"]) == 1 and result["intervals"][0]["quality"] == "current"
    with pytest.raises(DataError, match="Conflicting"):
        parse_ev_timeline(timeline(interval(), [interval(intervalStartSocPercent=68)]), now=NOW)
    with pytest.raises(DataError, match="Conflicting"):
        parse_ev_timeline(
            timeline(
                interval(intervalStartSocPercent=100.1),
                [interval(intervalStartSocPercent=100.2)],
            ),
            now=NOW,
        )


def test_timeline_overlaps_are_rejected_and_dst_instants_remain_distinct():
    with pytest.raises(DataError, match="Overlapping"):
        parse_ev_timeline(timeline(interval(), [interval(NOW + timedelta(minutes=15))]), now=NOW)
    clock = datetime(2026, 10, 25, tzinfo=timezone.utc)
    rows = [
        interval(intervalStartTimeIso="2026-10-25T01:00:00+01:00"),
        interval(intervalStartTimeIso="2026-10-25T01:00:00+00:00"),
    ]
    parsed = parse_ev_timeline(timeline(future=rows), now=clock)["timeline"]["intervals"]
    assert [row["valid_from"] for row in parsed] == [
        "2026-10-25T00:00:00+00:00",
        "2026-10-25T01:00:00+00:00",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("intervalStartTimeIso", None),
        ("intervalStartTimeIso", "2026-10-30T12:00:00"),
        ("intervalLengthMinutes", None),
        ("intervalLengthMinutes", 0),
        ("intervalLengthMinutes", -1),
        ("intervalLengthMinutes", 1441),
        ("intervalLengthMinutes", True),
        ("intervalLengthMinutes", float("inf")),
        ("intervalLengthMinutes", 1e-100),
        ("intervalStartSocPercent", True),
        ("intervalStartSocPercent", "69"),
        ("intervalStartSocPercent", float("nan")),
        ("intervalStartSocPercent", float("inf")),
        ("intervalStartSocPercent", -float("inf")),
        ("generalPriceCents", True),
        ("chargeEnergySourceEnum", "x" * 129),
        ("chargeEnergySourceEnum", "bad\nstate"),
    ],
)
def test_invalid_timeline_values_fail_safely(field, value):
    with pytest.raises(DataError):
        parse_ev_timeline(timeline(interval(**{field: value})), now=NOW)


@pytest.mark.parametrize("clock", [datetime(2026, 10, 30), "now", 0])
def test_timeline_rejects_implicit_timezone_clock(clock):
    with pytest.raises(DataError):
        parse_ev_timeline(timeline(interval()), now=clock)


def test_timeline_missing_measurements_remain_none_and_negative_prices_are_valid():
    missing = {"intervalStartTimeIso": NOW.isoformat(), "intervalLengthMinutes": 5}
    row = parse_ev_timeline(timeline(missing), now=NOW)["timeline"]["intervals"][0]
    assert row["state_of_charge"] is None and row["price"] is None and row["source"] is None
    assert row["raw_state_of_charge"] is None
    row = parse_ev_timeline(timeline(interval(generalPriceCents=-2)), now=NOW)["timeline"][
        "intervals"
    ][0]
    assert row["price"] == -2


def test_ready_by_normalizes_explicit_offsets_without_inferring_activity():
    result = parse_ev_ready_by(ready())["ready_by"]
    assert result == {
        "reported": True,
        "configuration_reported": True,
        "target_soc": 80,
        "target_time": "2026-10-31T06:00:00+00:00",
        "created_at": "2026-10-30T11:00:00+00:00",
        "cancelled_at": None,
        "active": True,
        "recurring": False,
    }
    row = parse_ev_ready_by(ready(isActive=None, isRecurring=None))["ready_by"]
    assert row["active"] is None and row["recurring"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("targetSocPercent", 101),
        ("targetSocPercent", True),
        ("targetTime", "2026-10-30T12:00:00"),
        ("createdAt", "not-a-date"),
        ("cancelledAt", 12),
        ("isActive", 1),
        ("isRecurring", "false"),
    ],
)
def test_invalid_ready_by_fields_are_rejected(field, value):
    with pytest.raises(DataError):
        parse_ev_ready_by(ready(**{field: value}))


def test_dated_history_preserves_kwh_and_converts_only_grid_cents_to_gbp():
    result = parse_ev_history(history())["history"]
    assert result == {
        "reported": True,
        "nominal_capacity_wh": 60000,
        "nominal_economy_km_per_kwh": 6,
        "days": [
            {
                "date": "2026-10-29",
                "home_charge_kwh": 3,
                "away_charge_kwh": 1.5,
                "grid_charge_kwh": 2,
                "grid_cost": 0.52,
                "grid_average_price": 26,
                "solar_charge_kwh": 0.75,
                "battery_charge_kwh": 0.25,
            }
        ],
    }
    assert not {"today", "total_increasing", "last_reset"} & result.keys()


def test_history_uses_latest_31_reported_dates_sorted_without_current_day_assumption():
    first = NOW.date() - timedelta(days=60)
    rows = [day((first + timedelta(days=i)).isoformat()) for i in range(40)]
    rows.reverse()
    result = parse_ev_history(history(rows))["history"]["days"]
    assert len(result) == MAX_HISTORY_DAYS
    assert result[0]["date"] == (first + timedelta(days=9)).isoformat()
    assert result[-1]["date"] == (first + timedelta(days=39)).isoformat()
    assert result[-1]["date"] != NOW.date().isoformat()


def test_history_duplicate_day_deduplicates_identical_and_rejects_conflicts():
    assert len(parse_ev_history(history([day(), day()]))["history"]["days"]) == 1
    with pytest.raises(DataError, match="Conflicting"):
        parse_ev_history(history([day(), day(totalHomeChargeAddedKWh=4)]))


@pytest.mark.parametrize(
    "date", [None, "2026-02-30", "2026-1-01", "2026-10-29T12:00:00Z", "20261029"]
)
def test_history_requires_explicit_valid_calendar_date(date):
    with pytest.raises(DataError):
        parse_ev_history(history([day(date)]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("totalHomeChargeAddedKWh", -1),
        ("totalAwayChargeAddedKWh", True),
        ("totalAwayChargeAddedKWh", float("nan")),
        ("grid", {"totalAddedKWh": -0.1}),
        ("solar", {"totalAddedKWh": -1}),
        ("battery", {"totalAddedKWh": "1"}),
        ("grid", {"totalCostCents": float("inf")}),
    ],
)
def test_history_rejects_negative_energy_and_nonfinite_or_wrong_typed_values(field, value):
    with pytest.raises(DataError):
        parse_ev_history(history([day(**{field: value})]))


def test_history_zero_energy_negative_grid_cost_and_missing_values_stay_distinct():
    row = parse_ev_history(
        history(
            [
                day(
                    totalHomeChargeAddedKWh=0,
                    grid={
                        "totalAddedKWh": None,
                        "totalCostCents": -25,
                        "avgPriceCentsPerKWh": -2,
                    },
                )
            ]
        )
    )["history"]["days"][0]
    assert row["home_charge_kwh"] == 0 and row["grid_charge_kwh"] is None
    assert row["grid_cost"] == -0.25 and row["grid_average_price"] == -2
    missing = parse_ev_history(history([{"date": "2026-10-29"}]))["history"]["days"][0]
    assert all(value is None for key, value in missing.items() if key != "date")


@pytest.mark.parametrize(
    "field,value",
    [
        ("nominalBatteryCapacityWh", -1),
        ("nominalBatteryCapacityWh", True),
        ("nominalEconomyKmPerKWh", -1),
        ("nominalEconomyKmPerKWh", "6"),
    ],
)
def test_history_capacity_and_economy_are_validated_without_derived_range(field, value):
    with pytest.raises(DataError):
        parse_ev_history(history(**{field: value}))


@pytest.mark.parametrize(
    "parser,root,key",
    [
        (parse_ev_timeline, "evTimeline", "timeline"),
        (parse_ev_ready_by, "evReadyByState", "ready_by"),
        (parse_ev_history, "evDailySummary", "history"),
    ],
)
def test_missing_module_fresh_defaults_do_not_invent_measurements(parser, root, key):
    result = parser({"data": {root: None}})[key]
    assert result["reported"] is False
    if key == "timeline":
        assert result["intervals"] == []
    elif key == "ready_by":
        assert result["configuration_reported"] is False
        assert all(
            result[field] is None for field in ("target_soc", "target_time", "active", "recurring")
        )
    else:
        assert result["days"] == []
        assert result["nominal_capacity_wh"] is None
        assert result["nominal_economy_km_per_kwh"] is None


@pytest.mark.parametrize(
    "parser,payload",
    [
        (parse_ev_timeline, timeline(interval())),
        (parse_ev_ready_by, ready()),
        (parse_ev_history, history()),
    ],
)
def test_private_extras_never_survive_and_partial_graphql_errors_fail(parser, payload):
    private = "SYNTHETIC_PRIVATE_DATA"
    root = next(iter(payload["data"]))
    payload["data"][root].update(
        vin=private,
        location=private,
        entityId=private,
        commandHistory=[private],
        message=private,
        totalCost=99999,
    )
    original = deepcopy(payload)
    result = parser(payload)
    assert private not in json.dumps(result)
    assert payload == original
    payload["errors"] = [{"message": private}]
    with pytest.raises(DataError) as caught:
        parser(payload)
    assert private not in str(caught.value)


def test_unbounded_collections_are_rejected_before_row_processing():
    with pytest.raises(DataError):
        parse_ev_timeline(timeline(future=[None] * (MAX_TIMELINE_INPUT + 1)), now=NOW)
    with pytest.raises(DataError):
        parse_ev_history(history([None] * (MAX_HISTORY_INPUT + 1)))


@pytest.mark.parametrize(
    "parser,root",
    [
        (parse_ev_timeline, "evTimeline"),
        (parse_ev_ready_by, "evReadyByState"),
        (parse_ev_history, "evDailySummary"),
    ],
)
@pytest.mark.parametrize("value", [[], "invalid", 1, True])
def test_malformed_root_objects_are_rejected(parser, root, value):
    with pytest.raises(DataError):
        parser({"data": {root: value}})
