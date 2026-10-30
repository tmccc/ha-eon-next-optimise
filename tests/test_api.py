"""Exercise fixed read-only documents, authentication races and bounded HTTP IO."""

import asyncio
import json
import threading
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from custom_components.eon_next_optimise import api
from custom_components.eon_next_optimise.auth import TokenSet
from custom_components.eon_next_optimise.const import GRAPHQL_URL, HTTP_TIMEOUT, MAX_RETRY_AFTER
from custom_components.eon_next_optimise.exceptions import (
    AuthenticationError,
    CannotConnect,
    RateLimited,
)
from custom_components.eon_next_optimise.queries import OPERATIONS, STATUS_ROOTS

SITES = {"data": {"settings": {"siteIds": ["synthetic-site"], "selectedSiteId": "synthetic-site"}}}
PAYLOADS = {
    "sites": SITES,
    "prices": {
        "data": {
            "sitePricing": {
                "meterWindows": [
                    {
                        "usageType": "GENERAL",
                        "currentPeriod": {
                            "start": "2026-10-30T12:00:00Z",
                            "kwhPriceInCents": 12.5,
                        },
                    }
                ]
            }
        }
    },
    "live": {
        "data": {
            "smartshift": {
                "live": {
                    "stateOfChargePercentage": 50,
                    "sitePowerW": -200,
                }
            }
        }
    },
    "plan": {
        "data": {
            "smartshift": {
                "plan": {
                    "forecastData": {
                        "__typename": "PastAndForecastData",
                        "forecast": [],
                    }
                }
            }
        }
    },
    "status": {
        "data": {
            "smartshiftExperience": {"batteryEnrolled": True},
            "unifiedDevicesSummary": {"hasError": False, "devices": []},
        }
    },
}


class Content:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.yielded = 0

    async def iter_chunked(self, size):
        assert size == 64 * 1024
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.error:
            raise self.error


class Response:
    """HTTP data is delivered as chunks, without a .json convenience shortcut."""

    def __init__(
        self,
        status=200,
        payload=None,
        *,
        body=None,
        chunks=None,
        headers=None,
        enter=None,
        exit=None,
        stream_error=None,
    ):
        self.status = status
        self.headers = headers or {}
        self.enter = enter
        self.exit = exit
        if body is None:
            body = json.dumps(payload if payload is not None else SITES).encode()
        self.content = Content(chunks if chunks is not None else [body], stream_error)

    async def __aenter__(self):
        if self.enter:
            await self.enter()
        return self

    async def __aexit__(self, *args):
        if self.exit:
            await self.exit()


class Session:
    """Fail on unplanned IO and retain only synthetic request arguments."""

    def __init__(self, responses):
        self.responses = responses if callable(responses) else deque(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        if callable(self.responses):
            return self.responses(url, kwargs)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def tokens(identity="synthetic-id-token", refresh="synthetic-refresh-token", expires=None):
    return TokenSet(identity, refresh, expires or time.time() + 900, "synthetic-subject")


async def executor(function, *args):
    await asyncio.sleep(0)
    return function(*args)


def client(responses, *, current=True, callback=None, execute=executor):
    result = api.OptimiseClient(
        Session(responses),
        "synthetic@example.invalid",
        "synthetic-original-refresh",
        executor=execute,
        token_callback=callback,
    )
    result._auth = SimpleNamespace(
        refresh_token="synthetic-original-refresh",
        refresh=Mock(return_value=tokens("synthetic-renewed-id", "synthetic-rotated-refresh")),
        sign_in=Mock(return_value=tokens()),
        complete_mfa=Mock(return_value=tokens()),
    )
    if current:
        result._tokens = tokens()
    return result


@pytest.mark.parametrize("section", list(PAYLOADS))
async def test_only_fixed_queries_raw_id_header_and_tls_defaults(section):
    operations = list(STATUS_ROOTS) if section == "status" else [section]
    responses = (
        [
            Response(payload={"data": {root: PAYLOADS["status"]["data"].get(root)}})
            for root in STATUS_ROOTS.values()
        ]
        if section == "status"
        else [Response(payload=PAYLOADS[section])]
    )
    result = client(responses)
    if section == "sites":
        parsed = await result.async_discover_sites()
        assert parsed[0]["site_id"] == "synthetic-site"
    else:
        parsed = await result.async_fetch_section(section, "synthetic-site")
        assert isinstance(parsed, dict)
    assert len(result._session.calls) == len(operations)
    for operation, (url, request) in zip(operations, result._session.calls, strict=True):
        assert url == GRAPHQL_URL and url.startswith("https://")
        assert request["headers"] == {
            "Authorization": "synthetic-id-token",
            "Accept": "application/json",
        }
        assert request["allow_redirects"] is False
        assert "ssl" not in request and "proxy" not in request
        assert request["timeout"].total == HTTP_TIMEOUT
        assert request["json"] == {
            "operationName": OPERATIONS[operation]["operation_name"],
            "query": OPERATIONS[operation]["query"],
            "variables": {} if section == "sites" else {"siteId": "synthetic-site"},
        }
        assert request["json"]["query"].lstrip().startswith("query ")
        assert "mutation " not in request["json"]["query"]
    result._auth.sign_in.assert_not_called()
    result._auth.refresh.assert_not_called()


async def test_site_selection_is_a_variable_not_document_interpolation():
    site = 'synthetic-site") { mutation setBatteryMode { __typename } } #'
    result = client([Response(payload=PAYLOADS["live"])])
    await result.async_fetch_section("live", site)
    _, request = result._session.calls[0]
    assert request["json"]["variables"] == {"siteId": site}
    assert site not in request["json"]["query"]


@pytest.mark.parametrize("section", ["mutation", "https://synthetic.invalid/graphql", "sites", ""])
async def test_unknown_sections_cannot_send_requests(section):
    result = client([])
    with pytest.raises(ValueError):
        await result.async_fetch_section(section, "synthetic-site")
    assert not result._session.calls
    result._auth.refresh.assert_not_called()


@pytest.mark.parametrize("site", [None, [], 42, "", "x" * 513])
async def test_invalid_site_selection_is_rejected_without_io(site):
    result = client([])
    with pytest.raises(ValueError):
        await result.async_fetch_section("live", site)
    assert not result._session.calls


async def test_tampered_mutation_document_is_rejected(monkeypatch):
    monkeypatch.setitem(
        OPERATIONS, "sites", {"operation_name": "Synthetic", "query": "mutation Unsafe { setMode }"}
    )
    result = client([])
    with pytest.raises(ValueError):
        await result.async_discover_sites()
    assert not result._session.calls


async def test_sign_in_runs_in_executor_and_persists_only_refresh_token():
    execute = AsyncMock(side_effect=lambda function, *args: function(*args))
    callback = AsyncMock()
    result = client([], current=False, callback=callback, execute=execute)
    await result.async_sign_in("synthetic-password-do-not-retain")
    execute.assert_awaited_once_with(result._auth.sign_in, "synthetic-password-do-not-retain")
    callback.assert_awaited_once_with("synthetic-refresh-token")
    assert result.refresh_token == "synthetic-refresh-token"
    assert result.account_subject == "synthetic-subject"
    assert not hasattr(result, "password")
    assert "synthetic-password-do-not-retain" not in vars(result).values()
    assert not result._session.calls


async def test_default_executor_runs_blocking_auth_off_event_loop():
    result = client([], current=False, execute=None)
    original_thread = threading.get_ident()
    auth_threads = []

    def sign_in(_password):
        auth_threads.append(threading.get_ident())
        return tokens()

    result._auth.sign_in = sign_in
    await result.async_sign_in("synthetic-password")
    assert auth_threads and auth_threads[0] != original_thread


async def test_mfa_uses_same_executor_and_rotation_callback():
    execute = AsyncMock(side_effect=lambda function, *args: function(*args))
    first = Mock()
    callback = Mock()
    result = client([], callback=first, execute=execute)
    result.set_token_callback(callback)
    await result.async_complete_mfa("123456")
    execute.assert_awaited_once_with(result._auth.complete_mfa, "123456")
    callback.assert_called_once_with("synthetic-refresh-token")
    first.assert_not_called()


async def test_401_refreshes_once_and_retries_with_new_id_token():
    callback = AsyncMock()
    result = client([Response(401), Response(payload=SITES)], callback=callback)
    await result.async_discover_sites()
    assert [call[1]["headers"]["Authorization"] for call in result._session.calls] == [
        "synthetic-id-token",
        "synthetic-renewed-id",
    ]
    result._auth.refresh.assert_called_once_with()
    result._auth.sign_in.assert_not_called()
    callback.assert_awaited_once_with("synthetic-rotated-refresh")


async def test_second_401_stops_without_repeating_refresh():
    result = client([Response(401), Response(401)])
    with pytest.raises(AuthenticationError):
        await result.async_discover_sites()
    assert len(result._session.calls) == 2
    result._auth.refresh.assert_called_once()


async def test_simultaneous_rejected_tokens_share_one_refresh():
    barrier = asyncio.Barrier(2)

    def answer(_url, kwargs):
        if kwargs["headers"]["Authorization"] == "synthetic-id-token":
            return Response(401, enter=barrier.wait)
        return Response(payload=SITES)

    callback = AsyncMock()
    result = client(answer, callback=callback)
    await asyncio.gather(result.async_discover_sites(), result.async_discover_sites())
    assert len(result._session.calls) == 4
    result._auth.refresh.assert_called_once()
    callback.assert_awaited_once()


async def test_expired_session_refresh_is_shared_across_concurrent_sections():
    result = client(lambda _url, _kwargs: Response(payload=PAYLOADS["live"]))
    result._tokens = tokens(expires=time.time() - 10)
    await asyncio.gather(*(result.async_fetch_section("live", "synthetic-site") for _ in range(6)))
    result._auth.refresh.assert_called_once()
    assert len(result._session.calls) == 6


async def test_cognito_rate_limit_blocks_all_queued_section_refreshes():
    result = client([], current=False)
    result._auth.refresh.side_effect = RateLimited(300)
    outcomes = await asyncio.gather(
        result.async_discover_sites(),
        result.async_fetch_section("prices", "synthetic-site"),
        result.async_fetch_section("live", "synthetic-site"),
        result.async_fetch_section("status", "synthetic-site"),
        return_exceptions=True,
    )
    assert all(isinstance(outcome, RateLimited) for outcome in outcomes)
    result._auth.refresh.assert_called_once_with()
    assert not result._session.calls
    assert result._retry_at > api.time.monotonic() + 299


async def test_cancelled_executor_refresh_persists_once_before_unlocking_queued_read():
    started = threading.Event()
    release = threading.Event()
    callback = AsyncMock()
    result = client([Response(payload=SITES)], current=False, callback=callback, execute=None)

    def refresh():
        started.set()
        assert release.wait(timeout=4)
        result._auth.refresh_token = "synthetic-rotated-refresh"
        return tokens("synthetic-renewed-id", "synthetic-rotated-refresh")

    result._auth.refresh.side_effect = refresh
    first = asyncio.create_task(result.async_discover_sites())
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(result.async_discover_sites())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        assert not callback.await_count
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 2)
        assert (await asyncio.wait_for(second, 2))[0]["site_id"] == "synthetic-site"
        result._auth.refresh.assert_called_once()
        callback.assert_awaited_once_with("synthetic-rotated-refresh")
        assert len(result._session.calls) == 1
        assert result._session.calls[0][1]["headers"]["Authorization"] == "synthetic-renewed-id"
        assert not result._auth_tasks
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_repeated_cancel_and_shutdown_wait_for_rotation_persistence():
    started = threading.Event()
    release_sdk = threading.Event()
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    saved = []

    async def persist(refresh):
        saved.append(refresh)
        persistence_started.set()
        await release_persistence.wait()

    result = client([], current=False, callback=persist, execute=None)

    def refresh():
        started.set()
        assert release_sdk.wait(timeout=4)
        result._auth.refresh_token = "synthetic-rotated-refresh"
        return tokens("synthetic-renewed-id", "synthetic-rotated-refresh")

    result._auth.refresh.side_effect = refresh
    pending = asyncio.create_task(result.async_discover_sites())
    shutdown = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(3):
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        shutdown = asyncio.create_task(result.async_shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        with pytest.raises(CannotConnect, match="stopped"):
            await result.async_discover_sites()
        release_sdk.set()
        await asyncio.wait_for(persistence_started.wait(), 2)
        assert not pending.done()
        assert not shutdown.done()
        release_persistence.set()
        await asyncio.wait_for(shutdown, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        result._auth.refresh.assert_called_once()
        assert saved == ["synthetic-rotated-refresh"]
        assert not result._session.calls
        assert not result._auth_tasks
        assert not result._auth_lock.locked()
    finally:
        release_sdk.set()
        release_persistence.set()
        await asyncio.gather(
            *(task for task in (pending, shutdown) if task), return_exceptions=True
        )


@pytest.mark.parametrize("wait_on", ["auth", "request"])
async def test_shutdown_prevents_already_queued_reads_from_starting_new_io(wait_on):
    result = client([Response(payload=SITES)], current=wait_on != "auth")
    if wait_on == "auth":
        await result._auth_lock.acquire()
    else:
        await result._request_limit.acquire()
        await result._request_limit.acquire()
    pending = asyncio.create_task(result.async_discover_sites())
    try:
        await asyncio.sleep(0)
        await result.async_shutdown()
    finally:
        if wait_on == "auth":
            result._auth_lock.release()
        else:
            result._request_limit.release()
            result._request_limit.release()
    with pytest.raises(CannotConnect, match="stopped"):
        await pending
    result._auth.refresh.assert_not_called()
    assert not result._session.calls


@pytest.mark.parametrize("method", ["async_sign_in", "async_complete_mfa"])
async def test_closed_client_cannot_start_new_authentication(method):
    result = client([], current=False)
    await result.async_shutdown()
    with pytest.raises(CannotConnect, match="stopped"):
        await getattr(result, method)("synthetic-input")
    result._auth.sign_in.assert_not_called()
    result._auth.complete_mfa.assert_not_called()
    assert not result._auth_tasks


async def test_revoked_refresh_never_falls_back_to_sign_in():
    result = client([], current=False)
    result._auth.refresh.side_effect = AuthenticationError("Optimise login requires user attention")
    with pytest.raises(AuthenticationError):
        await result.async_discover_sites()
    assert not result._session.calls
    result._auth.sign_in.assert_not_called()


@pytest.mark.parametrize("status", [302, 403, 404, 500, 503])
async def test_non_401_failures_do_not_refresh_or_retry(status):
    result = client([Response(status, {"private": "synthetic-provider-private-marker"})])
    with pytest.raises(CannotConnect) as caught:
        await result.async_discover_sites()
    assert not isinstance(caught.value, AuthenticationError)
    assert "synthetic" not in str(caught.value)
    assert len(result._session.calls) == 1
    result._auth.refresh.assert_not_called()


async def test_http_429_cooldown_applies_to_every_section(monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock.value, time=time.time))
    result = client([Response(429, headers={"Retry-After": "60"}), Response(payload=SITES)])
    with pytest.raises(RateLimited) as caught:
        await result.async_discover_sites()
    assert caught.value.retry_after == 60
    with pytest.raises(RateLimited):
        await result.async_fetch_section("live", "synthetic-site")
    assert len(result._session.calls) == 1
    clock.value += 61
    await result.async_discover_sites()
    assert len(result._session.calls) == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, 300),
        ("30", 30),
        ("0", 1),
        ("-2", 1),
        ("999999", MAX_RETRY_AFTER),
        ("inf", MAX_RETRY_AFTER),
        ("-inf", 1),
        ("nan", 300),
        ("not-a-date", 300),
        ("", 300),
    ],
)
def test_retry_after_delta_is_bounded(header, expected):
    assert api.retry_after_seconds(header) == expected


def test_retry_after_accepts_http_date(monkeypatch):
    base = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return base

    monkeypatch.setattr(api, "datetime", Clock)
    assert api.retry_after_seconds(format_datetime(base + timedelta(seconds=120))) == 120
    assert api.retry_after_seconds(format_datetime(base - timedelta(seconds=120))) == 1


@pytest.mark.parametrize("code", ["UNAUTHENTICATED", "NOT_AUTHENTICATED", "UNAUTHORIZED"])
async def test_graphql_auth_error_refreshes_once(code):
    failure = {"errors": [{"message": "synthetic-private-marker", "extensions": {"code": code}}]}
    result = client([Response(payload=failure), Response(payload=SITES)])
    await result.async_discover_sites()
    result._auth.refresh.assert_called_once()
    assert len(result._session.calls) == 2


async def test_repeated_graphql_auth_error_requires_reauthentication():
    payload = {"errors": [{"extensions": {"code": "UNAUTHENTICATED"}}]}
    result = client([Response(payload=payload), Response(payload=payload)])
    with pytest.raises(AuthenticationError):
        await result.async_discover_sites()
    result._auth.refresh.assert_called_once()
    assert len(result._session.calls) == 2


@pytest.mark.parametrize("code", ["RATE_LIMITED", "TOO_MANY_REQUESTS"])
async def test_graphql_rate_limit_sets_global_cooldown(code):
    result = client([Response(payload={"errors": [{"extensions": {"code": code}}]})])
    with pytest.raises(RateLimited):
        await result.async_discover_sites()
    with pytest.raises(RateLimited):
        await result.async_fetch_section("status", "synthetic-site")
    assert len(result._session.calls) == 1
    result._auth.refresh.assert_not_called()


@pytest.mark.parametrize(
    "errors",
    [
        [{"message": "synthetic-provider-private-marker"}],
        [None, "synthetic-provider-private-marker"],
        [{"extensions": None}],
        [{"extensions": []}],
        [{"extensions": {"code": []}}],
        [{"extensions": {"code": {"private": "synthetic-provider-private-marker"}}}],
        "synthetic-provider-private-marker",
    ],
)
async def test_malformed_graphql_errors_remain_sanitized(errors, caplog):
    result = client([Response(payload={"errors": errors, "data": SITES["data"]})])
    with pytest.raises(CannotConnect) as caught:
        await result.async_discover_sites()
    assert "synthetic" not in str(caught.value)
    assert "synthetic" not in caplog.text
    assert len(result._session.calls) == 1
    result._auth.refresh.assert_not_called()


@pytest.mark.parametrize(
    "body", [b"<html>synthetic-private-marker</html>", b"\xff", b"null", b"[]", b"42"]
)
async def test_invalid_json_or_shape_is_a_temporary_sanitized_failure(body):
    result = client([Response(body=body)])
    with pytest.raises(CannotConnect) as caught:
        await result.async_discover_sites()
    assert "synthetic" not in str(caught.value)
    assert len(result._session.calls) == 1


async def test_response_limit_uses_actual_chunks_not_claimed_content_length(monkeypatch):
    response = Response(
        chunks=[b"x" * 8, b"x" * 8, b"x", b"never-consumed"], headers={"Content-Length": "1"}
    )
    monkeypatch.setattr(api, "MAX_RESPONSE_BYTES", 16)
    result = client([response])
    with pytest.raises(CannotConnect, match="allowed size"):
        await result.async_discover_sites()
    assert response.content.yielded == 3


async def test_exact_response_limit_accepts_valid_chunked_json(monkeypatch):
    body = json.dumps(SITES).encode()
    monkeypatch.setattr(api, "MAX_RESPONSE_BYTES", len(body))
    result = client([Response(chunks=[body[:10], body[10:]])])
    assert (await result.async_discover_sites())[0]["site_id"] == "synthetic-site"


@pytest.mark.parametrize(
    "error", [aiohttp.ClientConnectionError("synthetic-private-marker"), asyncio.TimeoutError()]
)
async def test_transport_errors_are_sanitized_and_not_retried(error):
    result = client([error])
    with pytest.raises(CannotConnect) as caught:
        await result.async_discover_sites()
    assert "synthetic" not in str(caught.value)
    assert len(result._session.calls) == 1


async def test_interrupted_body_stream_is_sanitized():
    result = client(
        [
            Response(
                chunks=[b"{"], stream_error=aiohttp.ClientPayloadError("synthetic-private-marker")
            )
        ]
    )
    with pytest.raises(CannotConnect) as caught:
        await result.async_discover_sites()
    assert "synthetic" not in str(caught.value)


async def test_request_semaphore_bounds_parallel_reads():
    active = 0
    maximum = 0

    async def enter():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)

    async def exit():
        nonlocal active
        active -= 1

    result = client(lambda _url, _kwargs: Response(payload=SITES, enter=enter, exit=exit))
    await asyncio.gather(*(result.async_discover_sites() for _ in range(8)))
    assert maximum == 2
    assert active == 0
    assert len(result._session.calls) == 8
