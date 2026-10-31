"""A bad optional renewable percentage must not hide otherwise valid prices."""

from copy import deepcopy

import pytest

from custom_components.eon_next_optimise.models import (
    DataError,
    coverage,
    parse_live,
    parse_prices,
    parse_time,
)


def period(start="2026-10-31T01:30:00Z", value=22, renewable=50, **fields):
    return {
        "start": start,
        "kwhPriceInCents": value,
        "renewablePercentage": renewable,
        **fields,
    }


def payload(renewable=150, **forecast_fields):
    return {
        "data": {
            "sitePricing": {
                "meterWindows": [
                    {
                        "usageType": "GENERAL",
                        "previousPeriods": [period("2026-10-31T01:00:00Z", 21)],
                        "currentPeriod": period(),
                        "forecastPeriods": [
                            period("2026-10-31T02:00:00Z", 23, renewable, **forecast_fields)
                        ],
                    },
                    {
                        "usageType": "FEED_IN",
                        "currentPeriod": period(value=-20),
                        "forecastPeriods": [period("2026-10-31T02:00:00Z", -21)],
                    },
                ]
            }
        }
    }


@pytest.mark.parametrize("renewable", [101, -1, -100, 100.00001, -0.00001, 10**300, -(10**300)])
def test_finite_out_of_range_forecast_metadata_is_unknown_without_losing_tariffs(renewable):
    source = payload(renewable)
    original = deepcopy(source)
    result = parse_prices(source)
    current, _ = coverage(result["import"], parse_time("2026-10-31T01:37:00Z"))
    assert current["value"] == 22 and current["renewable_percentage"] == 50
    forecast = result["import"][-1]
    assert forecast["value"] == 23 and forecast["quality"] == "forecast"
    assert forecast["renewable_percentage"] is None
    assert forecast["valid_from"] == "2026-10-31T02:00:00+00:00"
    assert forecast["valid_to"] == "2026-10-31T02:30:00+00:00"
    assert len(result["import"]) == 3 and len(result["export"]) == 2
    assert result["export"][0]["value"] == 20
    assert source == original


@pytest.mark.parametrize("group", ["previousPeriods", "currentPeriod", "forecastPeriods"])
@pytest.mark.parametrize("direction", ["GENERAL", "FEED_IN"])
def test_only_the_invalid_percentage_field_becomes_unknown_in_each_group(group, direction):
    source = payload()
    window = next(
        item
        for item in source["data"]["sitePricing"]["meterWindows"]
        if item["usageType"] == direction
    )
    row = period(
        "2026-10-31T00:30:00Z",
        value=0,
        renewable=-1,
        estimate=False,
        demandWindow=False,
        indicator="NEUTRAL",
        kwhPriceRange={"minInCents": 0, "maxInCents": 0},
    )
    window[group] = row if group == "currentPeriod" else [row]
    result = parse_prices(source)["import" if direction == "GENERAL" else "export"]
    parsed = next(item for item in result if item["valid_from"] == "2026-10-31T00:30:00+00:00")
    assert parsed["value"] == 0 and parsed["raw_value"] == 0
    assert parsed["renewable_percentage"] is None
    assert parsed["range_min"] == 0 and parsed["range_max"] == 0
    assert parsed["estimate"] is False and parsed["demand_window"] is False
    assert parsed["indicator"] == "NEUTRAL"


@pytest.mark.parametrize("renewable", [None, 0, 0.5, 50, 99.99999, 100])
def test_missing_or_supported_renewable_percentage_is_retained(renewable):
    result = parse_prices(payload(renewable))
    assert result["import"][-1]["renewable_percentage"] == renewable


@pytest.mark.parametrize(
    "renewable", [True, False, "101", [], {}, float("nan"), float("inf"), -float("inf"), 10**1000]
)
def test_wrong_typed_and_nonfinite_renewable_metadata_still_rejects_payload(renewable):
    with pytest.raises(DataError):
        parse_prices(payload(renewable))


@pytest.mark.parametrize("soc", [-1, 101, 100.00001, -0.00001, True, "50", float("nan")])
def test_battery_soc_percentage_validation_is_not_relaxed(soc):
    with pytest.raises(DataError):
        parse_live({"data": {"smartshift": {"live": {"stateOfChargePercentage": soc}}}})


@pytest.mark.parametrize(
    "fields",
    [
        {"kwhPriceInCents": "23"},
        {"kwhPriceInCents": float("inf")},
        {"start": "2026-10-31T02:05:00Z"},
        {"estimate": "false"},
        {"demandWindow": 0},
        {"indicator": 0},
        {"kwhPriceRange": {"minInCents": 25, "maxInCents": 20}},
    ],
)
def test_other_price_and_metadata_validation_stays_strict(fields):
    source = payload()
    source["data"]["sitePricing"]["meterWindows"][0]["forecastPeriods"][0].update(fields)
    with pytest.raises(DataError):
        parse_prices(source)


def test_duplicate_conflicts_and_partial_graphql_errors_are_not_suppressed():
    source = payload()
    window = source["data"]["sitePricing"]["meterWindows"][0]
    window["forecastPeriods"].append(period("2026-10-31T02:00:00Z", value=24))
    with pytest.raises(DataError, match="Conflicting duplicate"):
        parse_prices(source)
    source = payload()
    source["errors"] = [{"message": "synthetic-private-provider-message"}]
    with pytest.raises(DataError) as caught:
        parse_prices(source)
    assert "synthetic-private" not in str(caught.value)


def test_empty_price_response_remains_empty_and_does_not_invent_zero():
    assert parse_prices({"data": {"sitePricing": {"meterWindows": []}}}) == {
        "import": [],
        "export": [],
    }


def test_unsupported_percentage_does_not_create_a_price_when_quote_is_missing():
    source = payload()
    source["data"]["sitePricing"]["meterWindows"][0]["forecastPeriods"][0]["kwhPriceInCents"] = None
    result = parse_prices(source)["import"][-1]
    assert result["renewable_percentage"] is None and result["value"] is None
