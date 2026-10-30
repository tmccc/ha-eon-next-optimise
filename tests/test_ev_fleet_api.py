"""Offline per-car plans/history using only a supplied fresh synthetic fleet."""

import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.eon_next_optimise import api
from custom_components.eon_next_optimise.auth import TokenSet
from custom_components.eon_next_optimise.exceptions import (
    AuthenticationError,
    CannotConnect,
    RateLimited,
)
from custom_components.eon_next_optimise.models import MAX_DEVICES, parse_time
from custom_components.eon_next_optimise.queries import EV_FLEET_ROOTS, OPERATIONS

FLEET = [
    {
        "entity_id": "PRIVATE_CAR_A",
        "make": "Synthetic",
        "model": "One",
        "PRIVATE_VIN": "PRIVATE_SECRET",
    },
    {"entity_id": "PRIVATE_CAR_B", "make": "Synthetic", "model": "Two"},
]
NOW = datetime.now(timezone.utc)
RAW = {
    "ev_timeline": {
        "currentInterval": None,
        "futureIntervals": [
            {
                "intervalStartTimeIso": (NOW + timedelta(minutes=10)).isoformat(),
                "intervalLengthMinutes": 30,
                "intervalStartSocPercent": 70,
                "generalPriceCents": 26,
                "chargeEnergySourceEnum": "GRID",
            }
        ],
    },
    "ev_ready_by": {
        "oneShotConfig": {
            "targetSocPercent": 80,
            "targetTime": (NOW + timedelta(hours=3)).isoformat(),
            "isActive": True,
            "isRecurring": False,
        }
    },
    "ev_history": {
        "nominalBatteryCapacityWh": 75000,
        "dailySummary": [
            {
                "date": "2026-10-30",
                "totalHomeChargeAddedKWh": 4,
                "grid": {"totalAddedKWh": 4, "totalCostCents": 104, "avgPriceCentsPerKWh": 26},
            }
        ],
    },
}


class Response:
    def __init__(self, status=200, payload=None, enter=None):
        self.status = status
        self.payload = payload
        self.headers = {"Retry-After": "60"}
        self.content = self
        self.enter = enter

    async def __aenter__(self):
        if self.enter:
            await self.enter()
        return self

    async def __aexit__(self, *_args):
        return None

    async def iter_chunked(self, size):
        assert size == 64 * 1024
        yield json.dumps(self.payload).encode()


def response(operation, *, failed=False, null=False, enter=None):
    payload = {"data": {EV_FLEET_ROOTS[operation]: None if null else deepcopy(RAW[operation])}}
    if failed:
        payload["errors"] = [
            {"message": "PRIVATE_PROVIDER_MESSAGE", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}
        ]
    return Response(payload=payload, enter=enter)


class Session:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def post(self, url, **kwargs):
        operation = next(
            name
            for name, details in OPERATIONS.items()
            if details["operation_name"] == kwargs["json"]["operationName"]
        )
        entity = kwargs["json"]["variables"].get("entityId")
        self.calls.append((operation, entity, url, kwargs))
        return self.answers.get((operation, entity)) or response(operation)


def client(answers=None):
    result = api.OptimiseClient(Session(answers), "PRIVATE_EMAIL", "PRIVATE_REFRESH")
    tokens = TokenSet("PRIVATE_ID_JWT", "PRIVATE_REFRESH", time.time() + 900, "PRIVATE_SUBJECT")
    result._tokens = tokens
    result._auth = SimpleNamespace(
        refresh_token="PRIVATE_REFRESH", refresh=Mock(return_value=tokens)
    )
    return result


@pytest.mark.parametrize(
    "section,expected_operations,modules",
    [
        ("ev_plan", ["ev_timeline", "ev_ready_by"], ["timeline", "ready_by"]),
        ("ev_history", ["ev_history"], ["history"]),
    ],
)
async def test_fixed_per_car_queries_use_only_supplied_fleet_and_exact_variables(
    section, expected_operations, modules
):
    result = client()
    fleet = deepcopy(FLEET)
    output = await result.async_fetch_ev_fleet_section(section, "PRIVATE_SITE", fleet)
    assert fleet == FLEET
    assert output["vehicle_count"] == 2
    assert [row["entity_id"] for row in output["vehicles"]] == [row["entity_id"] for row in FLEET]
    assert "PRIVATE_VIN" not in json.dumps(output) and "PRIVATE_SECRET" not in json.dumps(output)
    assert [(op, entity) for op, entity, _url, _request in result._session.calls] == [
        (op, car["entity_id"]) for car in FLEET for op in expected_operations
    ]
    for row in output["vehicles"]:
        assert set(row) == {"entity_id", "make", "model", *modules}
        for module in modules:
            assert row[module]["reported"] and row[module]["query_success"]
            assert parse_time(row[module]["fetched_at"]).utcoffset() == timedelta(0)
    for operation, entity, url, request in result._session.calls:
        variables = {"entityId": entity}
        if operation != "ev_history":
            variables["siteId"] = "PRIVATE_SITE"
        assert request["json"] == {
            "operationName": OPERATIONS[operation]["operation_name"],
            "query": OPERATIONS[operation]["query"],
            "variables": variables,
        }
        assert url == api.GRAPHQL_URL and url.startswith("https://")
        assert request["headers"]["Authorization"] == "PRIVATE_ID_JWT"
        assert request["allow_redirects"] is False and "ssl" not in request
        assert entity not in request["json"]["query"]
        assert request["json"]["query"].lstrip().startswith("query ")
        assert "mutation " not in request["json"]["query"]


@pytest.mark.parametrize(
    "broken,module", [("ev_timeline", "timeline"), ("ev_ready_by", "ready_by")]
)
async def test_one_car_module_failure_keeps_siblings_and_other_car_without_stale_values(
    broken, module
):
    result = client({(broken, "PRIVATE_CAR_A"): response(broken, failed=True)})
    output = await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", FLEET)
    failed = output["vehicles"][0][module]
    assert (
        failed["reported"] is False
        and failed["query_success"] is False
        and failed["fetched_at"] is None
    )
    assert output["vehicles"][1][module]["query_success"] is True
    other = "ready_by" if module == "timeline" else "timeline"
    assert output["vehicles"][0][other]["query_success"] is True
    assert failed.get("target_soc") is None
    assert failed.get("intervals", []) == []
    info = result.last_error_info("ev_plan", "PRIVATE_SITE").as_dict()
    assert info["category"] == "partial" and info["failed_request_count"] == 1
    assert info["failed_roots"] == [EV_FLEET_ROOTS[broken]]
    assert "PRIVATE" not in json.dumps(info)


async def test_all_plan_requests_failed_raise_with_unique_root_names_and_real_request_count():
    answers = {
        (op, car["entity_id"]): response(op, failed=True)
        for car in FLEET
        for op in ("ev_timeline", "ev_ready_by")
    }
    result = client(answers)
    with pytest.raises(CannotConnect):
        await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", FLEET)
    info = result.last_error_info("ev_plan", "PRIVATE_SITE").as_dict()
    assert info["category"] == "graphql" and info["failed_request_count"] == 4
    assert info["failed_roots"] == ["evReadyByState", "evTimeline"]
    assert "PRIVATE" not in json.dumps(info)


async def test_known_empty_fleet_needs_no_request_and_null_success_is_unknown_not_zero():
    result = client()
    assert await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", []) == {
        "vehicles": [],
        "vehicle_count": 0,
    }
    assert not result._session.calls
    result._session.answers = {
        (op, "PRIVATE_CAR_A"): response(op, null=True) for op in ("ev_timeline", "ev_ready_by")
    }
    output = await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", FLEET[:1])
    row = output["vehicles"][0]
    assert row["timeline"]["reported"] is False and row["timeline"]["query_success"] is True
    assert row["ready_by"]["target_soc"] is None and row["ready_by"]["active"] is None
    assert row["ready_by"]["fetched_at"] is not None
    assert result.last_error_info("ev_plan", "PRIVATE_SITE") is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [None],
        [{"entity_id": ""}],
        [{"entity_id": "x" * 257}],
        [{"entity_id": "PRIVATE\nCAR"}],
        [FLEET[0], FLEET[0]],
        [FLEET[0], {"entity_id": "PRIVATE_CAR_C", "model": 42}],
        [{"entity_id": str(index)} for index in range(MAX_DEVICES + 1)],
    ],
)
async def test_whole_fleet_is_validated_before_any_request(bad):
    result = client()
    with pytest.raises(ValueError):
        await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", bad)
    assert not result._session.calls
    result._auth.refresh.assert_not_called()


@pytest.mark.parametrize("status,error_type", [(401, AuthenticationError), (429, RateLimited)])
async def test_authentication_and_rate_limits_after_success_are_not_partial(status, error_type):
    result = client({("ev_ready_by", "PRIVATE_CAR_A"): Response(status, {})})
    with pytest.raises(error_type):
        await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", FLEET)
    assert {entity for _op, entity, _url, _request in result._session.calls} == {"PRIVATE_CAR_A"}
    assert result.last_error_info("ev_plan", "PRIVATE_SITE").as_dict()["category"] != "partial"


async def test_cancelled_per_car_request_propagates_without_reading_the_next_car():
    async def cancel():
        raise asyncio.CancelledError

    result = client({("ev_ready_by", "PRIVATE_CAR_A"): response("ev_ready_by", enter=cancel)})
    with pytest.raises(asyncio.CancelledError):
        await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", FLEET)
    assert len(result._session.calls) == 2


async def test_history_partial_cache_remains_until_every_car_has_recovered():
    result = client({("ev_history", "PRIVATE_CAR_B"): response("ev_history", failed=True)})
    output = await result.async_fetch_ev_fleet_section("ev_history", "PRIVATE_SITE", FLEET)
    assert output["vehicles"][0]["history"]["days"][0]["grid_cost"] == 1.04
    assert output["vehicles"][1]["history"]["days"] == []
    observed = []

    async def observe():
        observed.append(result.last_error_info("ev_history", "PRIVATE_SITE").category)

    result._session.answers = {
        ("ev_history", "PRIVATE_CAR_B"): response("ev_history", enter=observe)
    }
    await result.async_fetch_ev_fleet_section("ev_history", "PRIVATE_SITE", FLEET)
    assert observed == ["partial"]
    assert result.last_error_info("ev_history", "PRIVATE_SITE") is None


@pytest.mark.parametrize(
    "operation,site,entity",
    [
        ("live", "PRIVATE_SITE", "PRIVATE_CAR"),
        ("ev_timeline", "PRIVATE_SITE", None),
        ("ev_ready_by", None, "PRIVATE_CAR"),
        ("ev_history", "PRIVATE_SITE", ""),
    ],
)
async def test_entity_id_can_only_be_used_by_the_three_fixed_vehicle_documents(
    operation, site, entity
):
    result = client()
    with pytest.raises(ValueError):
        await result._query(operation, site, entity_id=entity)
    assert not result._session.calls


async def test_fleet_sections_cannot_fall_back_to_normal_or_stopped_client_queries():
    result = client()
    with pytest.raises(ValueError):
        await result.async_fetch_section("ev_history", "PRIVATE_SITE")
    with pytest.raises(ValueError):
        await result.async_fetch_ev_fleet_section("status", "PRIVATE_SITE", FLEET)
    await result.async_shutdown()
    with pytest.raises(CannotConnect):
        await result.async_fetch_ev_fleet_section("ev_plan", "PRIVATE_SITE", [])
    assert not result._session.calls
