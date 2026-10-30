"""Independent fixed status roots retain valid data without hiding auth failures."""

import asyncio
import json
import time
from copy import deepcopy
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
from custom_components.eon_next_optimise.models import DataError, parse_status
from custom_components.eon_next_optimise.queries import OPERATIONS, STATUS_ROOTS

ROOT_DATA = {
    "status": {
        "hasError": False,
        "devices": [
            {
                "deviceTypeEnum": "EV",
                "hasError": False,
                "isOnboarding": False,
                "displayValues": {"brand": "Synthetic", "model": "Vehicle", "status": "Ready"},
            },
            {
                "deviceTypeEnum": "BATTERY",
                "hasError": False,
                "isOnboarding": True,
                "displayValues": {
                    "brand": "Synthetic",
                    "model": "Battery",
                    "status": "Link requested",
                },
            },
        ],
    },
    "status_experience": {
        "batteryEnrolled": True,
        "solarEnrolled": False,
        "evEnrolled": False,
        "evDevicesEnrolled": True,
    },
    "status_strategy": {"status": "ACTIVE", "globalKillswitchEnabled": False},
    "status_settings": {
        "solarCurtailment": {"enabled": False, "supported": True},
        "battery": [{"minReserveLevel": {"value": 25}}],
    },
}


class Response:
    def __init__(self, status, payload, enter=None):
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


def success(operation, *, data=ROOT_DATA, enter=None):
    return Response(200, {"data": {STATUS_ROOTS[operation]: deepcopy(data[operation])}}, enter)


def failed(operation):
    # Even an apparent valid value in the same failed root must be discarded.
    return Response(
        200,
        {
            "data": {STATUS_ROOTS[operation]: deepcopy(ROOT_DATA[operation])},
            "errors": [
                {
                    "message": "PRIVATE_PROVIDER_MESSAGE",
                    "extensions": {"code": "INTERNAL_SERVER_ERROR"},
                }
            ],
        },
    )


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def post(self, url, **kwargs):
        operation = next(
            key
            for key, value in OPERATIONS.items()
            if value["operation_name"] == kwargs["json"]["operationName"]
        )
        self.calls.append((operation, url, kwargs))
        answer = self.responses[operation]
        if callable(answer):
            return answer()
        return answer


def client(responses=None):
    session = Session(responses or {operation: success(operation) for operation in STATUS_ROOTS})
    result = api.OptimiseClient(session, "PRIVATE_EMAIL", "PRIVATE_REFRESH")
    tokens = TokenSet("PRIVATE_ID_TOKEN", "PRIVATE_REFRESH", time.time() + 900, "PRIVATE_SUBJECT")
    result._tokens = tokens
    result._auth = SimpleNamespace(
        refresh_token="PRIVATE_REFRESH", refresh=Mock(return_value=tokens)
    )
    return result


async def test_four_fixed_root_documents_share_one_status_public_shape():
    result = client()
    parsed = await result.async_fetch_section("status", "PRIVATE_SITE")
    assert parsed["device_count"] == 2 and parsed["error_count"] == 0
    assert parsed["devices"][1]["onboarding"] is True
    assert parsed["ev_enrolled"] is False and parsed["ev_devices_enrolled"] is True
    assert parsed["global_killswitch"] is False
    assert parsed["minimum_reserve"] == [25]
    assert result.last_error_info("status", "PRIVATE_SITE") is None
    assert [operation for operation, _url, _request in result._session.calls] == list(STATUS_ROOTS)
    assert len(OPERATIONS) == 12
    for operation, url, request in result._session.calls:
        assert url == api.GRAPHQL_URL and url.startswith("https://")
        assert request["json"] == {
            "operationName": OPERATIONS[operation]["operation_name"],
            "query": OPERATIONS[operation]["query"],
            "variables": {"siteId": "PRIVATE_SITE"},
        }
        assert request["headers"]["Authorization"] == "PRIVATE_ID_TOKEN"
        assert request["allow_redirects"] is False and "ssl" not in request
        assert "PRIVATE_SITE" not in request["json"]["query"]
        assert request["json"]["query"].lstrip().startswith("query ")
        assert "mutation " not in request["json"]["query"]


@pytest.mark.parametrize("broken", list(STATUS_ROOTS))
async def test_each_failing_root_keeps_other_roots_and_never_invents_zero(broken):
    responses = {operation: success(operation) for operation in STATUS_ROOTS}
    responses[broken] = failed(broken)
    result = client(responses)
    parsed = await result.async_fetch_section("status", "PRIVATE_SITE")
    if broken == "status":
        assert parsed["device_count"] is None and parsed["error_count"] is None
        assert parsed["has_error"] is None and parsed["devices"] == []
    else:
        assert parsed["device_count"] == 2
    if broken == "status_experience":
        assert parsed["battery_enrolled"] is None and parsed["ev_enrolled"] is None
        assert parsed["ev_devices_enrolled"] is None
    else:
        assert parsed["ev_enrolled"] is False and parsed["ev_devices_enrolled"] is True
    if broken == "status_strategy":
        assert parsed["strategy_status"] is None and parsed["global_killswitch"] is None
    else:
        assert parsed["strategy_status"] == "ACTIVE"
    if broken == "status_settings":
        assert parsed["curtailment_enabled"] is None and parsed["minimum_reserve"] == []
    else:
        assert parsed["minimum_reserve"] == [25]
    info = result.last_error_info("status", "PRIVATE_SITE").as_dict()
    assert info["category"] == "partial"
    assert info["failed_roots"] == [STATUS_ROOTS[broken]]
    assert info["http_status"] == 200
    assert info["graphql_codes"] == ["INTERNAL_SERVER_ERROR"]
    assert "PRIVATE" not in json.dumps(info)


async def test_malformed_root_isolated_before_merge_and_null_root_does_not_become_zero():
    data = deepcopy(ROOT_DATA)
    data["status_experience"]["evDevicesEnrolled"] = "PRIVATE_INVALID_BOOL"
    data["status_settings"] = None
    result = client({operation: success(operation, data=data) for operation in STATUS_ROOTS})
    parsed = await result.async_fetch_section("status", "PRIVATE_SITE")
    assert parsed["device_count"] == 2
    assert parsed["ev_devices_enrolled"] is None and parsed["battery_enrolled"] is None
    assert parsed["minimum_reserve"] == [] and parsed["curtailment_enabled"] is None
    info = result.last_error_info("status", "PRIVATE_SITE").as_dict()
    assert info["category"] == "partial" and info["failed_roots"] == ["smartshiftExperience"]


async def test_all_failed_roots_raise_with_safe_complete_root_metadata():
    result = client({operation: failed(operation) for operation in STATUS_ROOTS})
    with pytest.raises(CannotConnect):
        await result.async_fetch_section("status", "PRIVATE_SITE")
    info = result.last_error_info("status", "PRIVATE_SITE").as_dict()
    assert info["category"] == "graphql"
    assert info["failed_roots"] == sorted(STATUS_ROOTS.values())
    assert info["graphql_error_count"] == 4
    assert "PRIVATE" not in json.dumps(info)


@pytest.mark.parametrize("fatal_operation", ["status_experience", "status_settings"])
@pytest.mark.parametrize("status,error_type", [(401, AuthenticationError), (429, RateLimited)])
async def test_auth_or_rate_limit_after_successes_propagates_and_stops(
    status, error_type, fatal_operation
):
    responses = {operation: success(operation) for operation in STATUS_ROOTS}
    responses[fatal_operation] = Response(status, {})
    result = client(responses)
    with pytest.raises(error_type):
        await result.async_fetch_section("status", "PRIVATE_SITE")
    operations = [operation for operation, _url, _request in result._session.calls]
    expected = list(STATUS_ROOTS)[: list(STATUS_ROOTS).index(fatal_operation) + 1]
    assert operations == expected + ([fatal_operation] if status == 401 else [])
    info = result.last_error_info("status", "PRIVATE_SITE").as_dict()
    assert info["category"] != "partial"
    assert info["http_status"] == status


async def test_cancellation_after_a_valid_summary_is_not_partial_success():
    async def cancel():
        raise asyncio.CancelledError

    responses = {operation: success(operation) for operation in STATUS_ROOTS}
    responses["status_experience"] = success("status_experience", enter=cancel)
    result = client(responses)
    with pytest.raises(asyncio.CancelledError):
        await result.async_fetch_section("status", "PRIVATE_SITE")
    assert [operation for operation, _url, _request in result._session.calls] == [
        "status",
        "status_experience",
    ]


async def test_partial_cache_only_clears_after_all_four_later_requests_succeed():
    responses = {operation: success(operation) for operation in STATUS_ROOTS}
    responses["status_strategy"] = failed("status_strategy")
    result = client(responses)
    await result.async_fetch_section("status", "PRIVATE_SITE")
    observed = []

    async def observe():
        observed.append(result.last_error_info("status", "PRIVATE_SITE").category)

    result._session.responses = {
        operation: success(operation, enter=observe if operation != "status" else None)
        for operation in STATUS_ROOTS
    }
    await result.async_fetch_section("status", "PRIVATE_SITE")
    assert observed == ["partial", "partial", "partial"]
    assert result.last_error_info("status", "PRIVATE_SITE") is None


async def test_internal_root_operations_cannot_be_selected_as_coordinator_sections():
    result = client()
    with pytest.raises(ValueError):
        await result.async_fetch_section("status_settings", "PRIVATE_SITE")
    assert not result._session.calls


@pytest.mark.parametrize("legacy,new", [(False, True), (True, False), (None, True), (False, None)])
def test_legacy_and_device_ev_enrolment_flags_remain_independent(legacy, new):
    parsed = parse_status(
        {"data": {"smartshiftExperience": {"evEnrolled": legacy, "evDevicesEnrolled": new}}}
    )
    assert parsed["ev_enrolled"] is legacy and parsed["ev_devices_enrolled"] is new
    with pytest.raises(DataError):
        parse_status({"data": {"smartshiftExperience": {"evDevicesEnrolled": 1}}})
