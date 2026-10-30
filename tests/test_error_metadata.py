"""Offline error categorisation with synthetic private markers and no network."""

import asyncio
import json
import time
from collections import deque
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest

from custom_components.eon_next_optimise import api, diagnostics
from custom_components.eon_next_optimise.auth import TokenSet
from custom_components.eon_next_optimise.const import SECTIONS
from custom_components.eon_next_optimise.exceptions import CannotConnect, ProviderErrorInfo
from custom_components.eon_next_optimise.queries import OPERATIONS


class Response:
    def __init__(self, status=200, payload=None, *, chunks=None):
        self.status = status
        self.headers = {}
        self.chunks = chunks or [json.dumps(payload).encode()]
        self.yielded = 0
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def iter_chunked(self, size):
        assert size == 64 * 1024
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class Session:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = 0
        self.requests = []

    def post(self, *args, **kwargs):
        self.calls += 1
        self.requests.append((args, kwargs))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def client(responses):
    result = api.OptimiseClient(Session(responses), "PRIVATE_EMAIL", "PRIVATE_REFRESH")
    result._tokens = TokenSet("PRIVATE_ID_JWT", "PRIVATE_REFRESH", time.time() + 900, "PRIVATE_SUB")
    return result


def validation_error(message, *, path=None, code="GRAPHQL_VALIDATION_FAILED"):
    return {
        "message": message,
        "path": path,
        "extensions": {"code": code, "secret": "PRIVATE_EXTRA"},
    }


async def test_ev_uses_fixed_read_only_query_and_shared_raw_id_authentication():
    result = client([Response(payload={"data": {"evLive": {"status": "READY", "vehicles": []}}})])
    parsed = await result.async_fetch_section("ev", "PRIVATE_SITE")
    assert parsed["vehicles"] == []
    assert result._session.calls == 1
    _args, request = result._session.requests[0]
    assert request["json"] == {
        "operationName": OPERATIONS["ev"]["operation_name"],
        "query": OPERATIONS["ev"]["query"],
        "variables": {"siteId": "PRIVATE_SITE"},
    }
    assert request["headers"]["Authorization"] == "PRIVATE_ID_JWT"
    assert request["allow_redirects"] is False
    assert "ssl" not in request
    assert "PRIVATE_SITE" not in request["json"]["query"]
    assert request["json"]["query"].lstrip().startswith("query ")
    assert "mutation " not in request["json"]["query"]
    info = api.provider_error_info(
        "http",
        400,
        {"errors": [validation_error('Cannot query field "batteryLevel" on type "PRIVATE_TYPE".')]},
    ).as_dict()
    assert info["graphql_fields"] == ["batteryLevel"]


@pytest.mark.parametrize("status", [400, 403, 422])
async def test_error_http_json_reduced_without_accepting_data(status, caplog):
    payload = {
        "data": {"smartshift": {"live": {"stateOfChargePercentage": 55}}},
        "errors": [
            validation_error('Cannot query field "liveMetrics" on type "PRIVATE_TYPE".'),
            validation_error("PRIVATE_MESSAGE", code="PRIVATE_CODE", path=["PRIVATE_SITE"]),
        ],
    }
    result = client([Response(status, payload)])
    with pytest.raises(CannotConnect):
        await result.async_fetch_section("live", "PRIVATE_SITE")
    info = result.last_error_info("live", "PRIVATE_SITE").as_dict()
    assert info["category"] == "http"
    assert info["http_status"] == status
    assert info["graphql_fields"] == ["liveMetrics"]
    assert info["graphql_codes"] == ["GRAPHQL_VALIDATION_FAILED"]
    assert info["graphql_error_count"] == 2
    assert info["unrecognized_error_count"] == 1
    assert info["graphql_types"] == []
    assert "PRIVATE" not in json.dumps(info)
    assert "PRIVATE" not in caplog.text
    assert result._session.calls == 1


@pytest.mark.parametrize(
    ("message", "issue", "fields", "types"),
    [
        (
            'Cannot query field "liveMetrics" on type "PRIVATE_TYPE".',
            "cannot_query_field",
            ["liveMetrics"],
            [],
        ),
        (
            'Unknown argument "siteId" on field "PRIVATE_TYPE.live".',
            "unknown_argument",
            ["live", "siteId"],
            [],
        ),
        (
            'Variable "$siteId" of type "String" used in position expecting type "PRIVATE_TYPE!".',
            "variable_type",
            ["siteId"],
            ["String"],
        ),
        (
            'Field "smartshiftExperience" argument "PRIVATE_ARG" of type "String!" is required, but it was not provided.',
            "required_argument",
            ["smartshiftExperience"],
            ["String"],
        ),
        (
            'Field "liveMetrics" must not have a selection since type "String" has no subfields.',
            "selection_shape",
            ["liveMetrics"],
            ["String"],
        ),
        (
            'Unknown type "SmartShiftMetricsWithInterval".',
            "unknown_type",
            [],
            ["SmartShiftMetricsWithInterval"],
        ),
        (
            'Fragment cannot be spread here as objects of type "PRIVATE_TYPE" can never be of type "SmartShiftDataError".',
            "fragment_type",
            [],
            ["SmartShiftDataError"],
        ),
    ],
)
def test_only_fixed_schema_names_from_anchored_validation_messages(message, issue, fields, types):
    info = api.provider_error_info("http", 400, {"errors": [validation_error(message)]}).as_dict()
    assert info["graphql_issues"] == [issue]
    assert info["graphql_fields"] == fields
    assert info["graphql_types"] == types
    assert info["unrecognized_error_count"] == 0
    assert "PRIVATE" not in json.dumps(info)


def test_paths_drop_indices_and_reject_unknown_segments_and_message_prefixes():
    payload = {
        "errors": [
            validation_error(
                "PRIVATE_MESSAGE", path=["smartshift", "live", "liveMetrics", 23, "siteImportKWh"]
            ),
            validation_error("PRIVATE_MESSAGE", path=["smartshift", "PRIVATE_SITE", "live"]),
            validation_error(
                'PRIVATE_PREFIX Cannot query field "liveMetrics" on type "PRIVATE_TYPE".'
            ),
        ]
    }
    info = api.provider_error_info("graphql", 200, payload).as_dict()
    assert info["graphql_paths"] == ["smartshift.live.liveMetrics.siteImportKWh"]
    assert info["graphql_fields"] == []
    assert info["unrecognized_error_count"] == 3
    assert "PRIVATE" not in json.dumps(info)


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (
            Response(
                payload={
                    "data": {"smartshift": {"live": {"stateOfChargePercentage": "PRIVATE_VALUE"}}}
                }
            ),
            "parser_invalid",
        ),
        (
            Response(payload={"data": None, "errors": [validation_error("PRIVATE_MESSAGE")]}),
            "graphql",
        ),
        (Response(chunks=[b"PRIVATE_NON_JSON"]), "invalid_json"),
        (aiohttp.ClientConnectionError("PRIVATE_MESSAGE"), "network"),
        (asyncio.TimeoutError("PRIVATE_MESSAGE"), "timeout"),
    ],
)
async def test_http_200_parser_graphql_and_transport_errors_are_distinct(response, category):
    result = client([response])
    with pytest.raises(CannotConnect):
        await result.async_fetch_section("live", "PRIVATE_SITE")
    info = result.last_error_info("live", "PRIVATE_SITE").as_dict()
    assert info["category"] == category
    assert info["http_status"] == (None if category in {"network", "timeout"} else 200)
    assert "PRIVATE" not in json.dumps(info)


async def test_error_body_reads_are_bounded_and_never_accepted():
    response = Response(400, chunks=[b"x" * 65536, b"PRIVATE_EXTRA", b"must-not-be-read"])
    result = client([response])
    with pytest.raises(CannotConnect):
        await result.async_fetch_section("live", "PRIVATE_SITE")
    assert response.yielded == 2
    assert result.last_error_info("live", "PRIVATE_SITE").as_dict()["http_status"] == 400


async def test_success_clears_only_its_own_site_and_section_error_slot():
    result = client(
        [Response(400), Response(403), Response(payload={"data": {"smartshift": {"live": None}}})]
    )
    for site in ("PRIVATE_SITE_A", "PRIVATE_SITE_B"):
        with pytest.raises(CannotConnect):
            await result.async_fetch_section("live", site)
    await result.async_fetch_section("live", "PRIVATE_SITE_A")
    assert result.last_error_info("live", "PRIVATE_SITE_A") is None
    assert result.last_error_info("live", "PRIVATE_SITE_B").http_status == 403
    assert result.last_error_info("prices", "PRIVATE_SITE_B") is None


async def test_authentication_dependency_failure_has_separate_cache_category():
    result = client([])
    result._tokens = None
    result._auth = SimpleNamespace(
        refresh=Mock(side_effect=CannotConnect("PRIVATE_JWKS")), refresh_token="PRIVATE_REFRESH"
    )
    with pytest.raises(CannotConnect):
        await result.async_fetch_section("live", "PRIVATE_SITE")
    assert (
        result.last_error_info("live", "PRIVATE_SITE").as_dict()["category"]
        == "authentication_connection"
    )
    assert not result._session.calls


async def test_diagnostics_reads_only_cached_metadata_without_queries_or_credentials():
    result = client(
        [Response(400, {"errors": [validation_error('Unknown type "SmartShiftDataError".')]})]
    )
    with pytest.raises(CannotConnect):
        await result.async_fetch_section("live", "PRIVATE_SITE")

    class PrivateEntry:
        @property
        def data(self):
            raise AssertionError("Entry credentials must not be accessed")

    class PrivateAuth:
        @property
        def refresh_token(self):
            raise AssertionError("Client credentials must not be accessed")

    result._auth = PrivateAuth()
    result._session.responses.clear()
    sections = {
        section: SimpleNamespace(
            client=result,
            site_id="PRIVATE_SITE",
            data={},
            last_success=None,
            last_update_success=False,
            max_age=timedelta(minutes=3),
            last_error_class="connection",
            last_exception=None,
        )
        for section in SECTIONS
    }
    sections["ev"].data = {
        "vehicle_count": 2,
        "vehicles": [
            {"entity_id": "PRIVATE_CAR_A", "state_of_charge": 69},
            {"entity_id": "PRIVATE_CAR_B", "state_of_charge": None},
        ],
    }
    entry = PrivateEntry()
    entry.runtime_data = SimpleNamespace(sites={"PRIVATE_SITE": SimpleNamespace(**sections)})
    output = await diagnostics.async_get_config_entry_diagnostics(None, entry)
    assert output["sites"][0]["sections"]["live"]["provider_error"]["graphql_types"] == [
        "SmartShiftDataError"
    ]
    assert output["sites"][0]["sections"]["prices"]["provider_error"] is None
    assert output["sites"][0]["sections"]["ev"]["vehicle_count"] == 2
    assert output["sites"][0]["sections"]["ev"]["vehicles_with_soc"] == 1
    assert "PRIVATE" not in json.dumps(output)
    assert result._session.calls == 1


def test_diagnostics_boundary_revalidates_even_manually_constructed_metadata():
    info = ProviderErrorInfo(
        "PRIVATE_CATEGORY",
        999,
        1000,
        -1,
        ("PRIVATE_CODE", "FORBIDDEN"),
        ("smartshift.PRIVATE_SITE.live", "smartshift.live"),
        ("PRIVATE_FIELD", "live"),
        ("PRIVATE_TYPE", "String"),
        ("PRIVATE_ISSUE", "variable_type"),
    ).as_dict()
    assert info["category"] == "unknown" and info["http_status"] is None
    assert info["graphql_error_count"] == 32 and info["unrecognized_error_count"] == 0
    assert info["graphql_paths"] == ["smartshift.live"]
    assert "PRIVATE" not in json.dumps(info)
