"""Synthetic, offline EV contracts; no cloud or emulator/account access."""

import json
import re
from copy import deepcopy

import pytest

from custom_components.eon_next_optimise.ev import parse_ev
from custom_components.eon_next_optimise.models import MAX_DEVICES, DataError
from custom_components.eon_next_optimise.queries import EV_QUERY, OPERATIONS


def vehicle(identity="synthetic_ev_one", **fields):
    """A provider-shaped fixture with no real vehicle/account data."""
    return {
        "entityId": identity,
        "batteryLevel": 69,
        "liveState": "Pending next charge",
        "connectivity": "ONLINE",
        "connectivityLabel": "Connected",
        "chargingSpeedLabel": "0 kW",
        "makeId": "tesla",
        "modelName": "Model 3",
        "readyByEligible": True,
        **fields,
    }


def envelope(vehicles=None, **live):
    return {
        "data": {
            "evLive": {
                "status": "ACTIVE",
                "vehicles": [vehicle()] if vehicles is None else vehicles,
                "siteInformation": {
                    "generalPriceCents": 26,
                    "feedInPriceCents": -23,
                    "renewablePercentage": 48,
                    "isInDemandWindow": False,
                },
                **live,
            }
        }
    }


def test_fixed_ev_document_is_query_only_and_excludes_private_and_control_fields():
    assert EV_QUERY.startswith("query OptimiseEv($siteId: String!) {")
    assert EV_QUERY.count("query ") == 1
    assert OPERATIONS["ev"] == {"operation_name": "OptimiseEv", "query": EV_QUERY}

    # Compare actual selected schema names, rather than provider display strings.
    selections = re.sub(r"\([^()]*\)", "", EV_QUERY[EV_QUERY.index("{") :])
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", selections))
    assert tokens == {
        "evLive",
        "status",
        "vehicles",
        "entityId",
        "batteryLevel",
        "liveState",
        "connectivity",
        "connectivityLabel",
        "chargingSpeedLabel",
        "makeId",
        "modelName",
        "readyByEligible",
        "siteInformation",
        "generalPriceCents",
        "feedInPriceCents",
        "renewablePercentage",
        "isInDemandWindow",
    }


def test_direct_values_preserve_raw_state_and_labels_without_inferred_plug_or_power():
    result = parse_ev(envelope())
    assert result == {
        "status": "ACTIVE",
        "vehicle_count": 1,
        "vehicles": [
            {
                "entity_id": "synthetic_ev_one",
                "state_of_charge": 69,
                "live_state": "Pending next charge",
                "connectivity": "ONLINE",
                "connectivity_label": "Connected",
                "charging_speed_label": "0 kW",
                "make": "tesla",
                "model": "Model 3",
                "ready_by_eligible": True,
            }
        ],
        "site": {
            "import_price": 26,
            "export_price": 23,
            "renewable_percentage": 48,
            "in_demand_window": False,
        },
    }
    row = result["vehicles"][0]
    assert not {"plugged_in", "charging", "power", "range", "range_km"} & row.keys()


def test_multiple_cars_keep_separate_identities_and_provider_values():
    result = parse_ev(
        envelope(
            [
                vehicle(),
                vehicle(
                    "synthetic_ev_two", batteryLevel=0, liveState="Unplugged", readyByEligible=False
                ),
            ]
        )
    )
    assert result["vehicle_count"] == 2
    assert [row["entity_id"] for row in result["vehicles"]] == [
        "synthetic_ev_one",
        "synthetic_ev_two",
    ]
    assert result["vehicles"][1]["state_of_charge"] == 0
    assert result["vehicles"][1]["ready_by_eligible"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {}},
        {"data": {"evLive": None}},
        {"data": {"evLive": {}}},
        {"data": {"evLive": {"vehicles": None}}},
    ],
)
def test_missing_ev_data_remains_unknown(payload):
    result = parse_ev(payload)
    assert result["status"] is None
    assert result["vehicles"] == []
    assert result["vehicle_count"] is None
    assert all(value is None for value in result["site"].values())


def test_empty_vehicle_array_means_zero_not_unknown():
    assert parse_ev(envelope([]))["vehicle_count"] == 0


def test_missing_vehicle_measurements_do_not_become_zero_false_or_another_car():
    result = parse_ev(envelope([{"entityId": "synthetic_ev_one"}]))["vehicles"][0]
    assert result["entity_id"] == "synthetic_ev_one"
    assert all(value is None for key, value in result.items() if key != "entity_id")


@pytest.mark.parametrize("value", [0, 0.5, 69, 100])
def test_valid_soc_boundaries(value):
    assert (
        parse_ev(envelope([vehicle(batteryLevel=value)]))["vehicles"][0]["state_of_charge"] == value
    )


@pytest.mark.parametrize("value", [-1, 101, True, "69", float("nan"), float("inf"), 10**1000])
def test_invalid_soc_is_rejected_without_clamping(value):
    with pytest.raises(DataError):
        parse_ev(envelope([vehicle(batteryLevel=value)]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("liveState", 1),
        ("liveState", "x" * 129),
        ("liveState", "bad\nstate"),
        ("connectivity", True),
        ("connectivityLabel", "x" * 257),
        ("chargingSpeedLabel", {}),
        ("modelName", []),
        ("makeId", 123),
        ("readyByEligible", 1),
        ("readyByEligible", "false"),
    ],
)
def test_malformed_selected_vehicle_fields_are_rejected(field, value):
    with pytest.raises(DataError):
        parse_ev(envelope([vehicle(**{field: value})]))


@pytest.mark.parametrize("value", [None, "", " ", True, "x" * 257, "bad\x00id"])
def test_missing_or_invalid_identity_is_rejected_without_echo(value):
    with pytest.raises(DataError) as caught:
        parse_ev(envelope([vehicle(value)]))
    assert "synthetic" not in str(caught.value)


def test_duplicate_identity_fails_without_silently_overwriting_a_car():
    with pytest.raises(DataError, match="duplicate"):
        parse_ev(envelope([vehicle(), vehicle(batteryLevel=10)]))


@pytest.mark.parametrize("collection", [{}, "vehicles", [None], [vehicle()] * (MAX_DEVICES + 1)])
def test_invalid_or_unbounded_collections_are_rejected(collection):
    with pytest.raises(DataError):
        parse_ev(envelope(collection))


@pytest.mark.parametrize(
    "field,value",
    [
        ("generalPriceCents", True),
        ("feedInPriceCents", "-23"),
        ("renewablePercentage", 101),
        ("isInDemandWindow", 0),
    ],
)
def test_invalid_site_measurements_are_rejected(field, value):
    payload = envelope()
    payload["data"]["evLive"]["siteInformation"][field] = value
    with pytest.raises(DataError):
        parse_ev(payload)


def test_negative_prices_and_zero_are_preserved_without_duplicate_tariff_assumptions():
    result = parse_ev(envelope(siteInformation={"generalPriceCents": 0, "feedInPriceCents": 3}))
    assert result["site"]["import_price"] == 0
    assert result["site"]["export_price"] == -3


def test_unselected_private_or_control_fields_never_survive_and_input_is_unchanged():
    private = "synthetic-private-do-not-retain"
    payload = envelope(
        [
            vehicle(
                vin=private,
                vehicleLocationLabel=private,
                commandHistory=[{"message": private}],
                warning={"message": private},
                location=private,
                accessToken=private,
            )
        ]
    )
    payload["data"]["evLive"]["siteInformation"]["accountId"] = private
    original = deepcopy(payload)
    result = parse_ev(payload)
    assert private not in json.dumps(result)
    assert payload == original


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"data": None}, {"data": []}, {"data": {"evLive": []}}]
)
def test_invalid_envelopes_fail_with_generic_errors(payload):
    with pytest.raises(DataError):
        parse_ev(payload)


def test_graphql_partial_data_is_rejected_without_provider_message():
    payload = envelope()
    payload["errors"] = [{"message": "synthetic-private-provider-message"}]
    with pytest.raises(DataError) as caught:
        parse_ev(payload)
    assert "synthetic-private" not in str(caught.value)
