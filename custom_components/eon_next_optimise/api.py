"""Fixed-endpoint, query-only Optimise client with one shared renewable session."""

import asyncio
import inspect
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial

import aiohttp

from .auth import OptimiseAuthenticator, TokenSet
from .const import (
    GRAPHQL_URL,
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    MAX_RETRY_AFTER,
    SECTIONS,
    TOKEN_REFRESH_MARGIN,
)
from .ev import parse_ev
from .ev_plan import parse_ev_history, parse_ev_ready_by, parse_ev_timeline
from .exceptions import (
    GRAPHQL_ERROR_CODES,
    GRAPHQL_FIELDS,
    GRAPHQL_TYPES,
    AuthenticationError,
    CannotConnect,
    ProviderErrorInfo,
    RateLimited,
)
from .models import (
    MAX_DEVICES,
    DataError,
    parse_live,
    parse_plan,
    parse_prices,
    parse_sites,
    parse_status,
)
from .queries import EV_FLEET_ROOTS, OPERATIONS, STATUS_ROOTS


def _parse_status_root(root: str, payload):
    """Validate exactly the requested root; never accept errored sibling data."""
    if (
        not isinstance(payload, dict)
        or payload.get("errors")
        or not isinstance(payload.get("data"), dict)
        or root not in payload["data"]
    ):
        raise DataError("Missing or failed status module")
    return parse_status({"data": {root: payload["data"][root]}})


_PARSERS = {
    "sites": parse_sites,
    "prices": parse_prices,
    "live": parse_live,
    "plan": parse_plan,
    **{operation: partial(_parse_status_root, root) for operation, root in STATUS_ROOTS.items()},
    "ev": parse_ev,
    "ev_timeline": parse_ev_timeline,
    "ev_ready_by": parse_ev_ready_by,
    "ev_history": parse_ev_history,
}

_EV_FLEET_MODULES = {
    "ev_plan": (("ev_timeline", "timeline"), ("ev_ready_by", "ready_by")),
    "ev_history": (("ev_history", "history"),),
}


def _identifier(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and bool(value.strip())
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _fleet_metadata(vehicles) -> list[dict]:
    """Validate the complete supplied fleet before sending any request."""
    if not isinstance(vehicles, list) or len(vehicles) > MAX_DEVICES:
        raise ValueError("Invalid vehicle fleet")
    result, seen = [], set()
    for vehicle in vehicles:
        if not isinstance(vehicle, dict) or not _identifier(vehicle.get("entity_id")):
            raise ValueError("Invalid vehicle fleet")
        identity = vehicle["entity_id"]
        if identity in seen:
            raise ValueError("Duplicate vehicle identity")
        seen.add(identity)
        metadata = {"entity_id": identity}
        for field in ("make", "model"):
            value = vehicle.get(field)
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("Invalid vehicle metadata")
            metadata[field] = value
        result.append(metadata)
    return result


def _fleet_error_info(failures, success_count: int) -> ProviderErrorInfo:
    infos = [info for _root, info in failures if info is not None]
    statuses = {info.http_status for info in infos if info.http_status is not None}
    categories = {info.category for info in infos}
    return ProviderErrorInfo(
        category="partial"
        if success_count
        else next(iter(categories))
        if len(categories) == 1
        else "unknown",
        http_status=next(iter(statuses)) if len(statuses) == 1 else None,
        graphql_error_count=min(32, sum(info.graphql_error_count for info in infos)),
        unrecognized_error_count=min(32, sum(info.unrecognized_error_count for info in infos)),
        graphql_codes=tuple(sorted({code for info in infos for code in info.graphql_codes})),
        graphql_paths=tuple(sorted({path for info in infos for path in info.graphql_paths})),
        graphql_fields=tuple(sorted({field for info in infos for field in info.graphql_fields})),
        graphql_types=tuple(sorted({kind for info in infos for kind in info.graphql_types})),
        graphql_issues=tuple(sorted({issue for info in infos for issue in info.graphql_issues})),
        failed_roots=tuple(sorted({root for root, _info in failures})),
        failed_request_count=len(failures),
    )


_STATUS_FIELDS = {
    "unifiedDevicesSummary": ("has_error", "devices", "device_count", "error_count"),
    "smartshiftExperience": (
        "battery_enrolled",
        "solar_enrolled",
        "ev_enrolled",
        "ev_devices_enrolled",
    ),
    "smartshiftBatteryStrategyConfig": ("strategy_status", "global_killswitch"),
    "smartshiftUserSettings": (
        "curtailment_enabled",
        "curtailment_supported",
        "minimum_reserve",
    ),
}

_NAME = r"[A-Za-z_][A-Za-z_0-9]*"
_TYPE = r"[\[\]!A-Za-z_0-9]+"
_VALIDATION_PATTERNS = (
    (
        "cannot_query_field",
        re.compile(
            rf'Cannot query field "(?P<field>{_NAME})" on type "(?P<type>{_NAME})"\.(?: Did you mean [^\r\n]{{1,1024}})?'
        ),
    ),
    (
        "unknown_argument",
        re.compile(
            rf'Unknown argument "(?P<argument>{_NAME})" on field "{_NAME}\.(?P<field>{_NAME})"\.(?: Did you mean [^\r\n]{{1,1024}})?'
        ),
    ),
    (
        "variable_type",
        re.compile(
            rf'Variable "\$(?P<field>{_NAME})" of type "(?P<type>{_TYPE})" used in position expecting type "(?P<expected>{_TYPE})"\.'
        ),
    ),
    (
        "required_argument",
        re.compile(
            rf'Field "(?P<field>{_NAME})" argument "(?P<argument>{_NAME})" of type "(?P<type>{_TYPE})" is required, but it was not provided\.'
        ),
    ),
    (
        "selection_shape",
        re.compile(
            rf'Field "(?P<field>{_NAME})" (?:must not have a selection since type "(?P<type>{_TYPE})" has no subfields|of type "(?P<expected>{_TYPE})" must have a selection of subfields)\.(?: Did you mean [^\r\n]{{1,1024}})?'
        ),
    ),
    (
        "unknown_type",
        re.compile(rf'Unknown type "(?P<type>{_NAME})"\.(?: Did you mean [^\r\n]{{1,1024}})?'),
    ),
    (
        "fragment_type",
        re.compile(
            rf'Fragment cannot be spread here as objects of type "(?P<type>{_NAME})" can never be of type "(?P<expected>{_NAME})"\.'
        ),
    ),
)


def provider_error_info(
    category: str, status: int | None = None, payload=None
) -> ProviderErrorInfo:
    """Reduce errors to fixed schema literals; never retain a message or response."""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return ProviderErrorInfo(category, status, int(bool(errors)), int(bool(errors)))
    codes, paths, fields, types, issues = set(), set(), set(), set(), set()
    unrecognized = 0
    for error in errors[:32]:
        if not isinstance(error, dict):
            unrecognized += 1
            continue
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if isinstance(code, str) and code in GRAPHQL_ERROR_CODES:
            codes.add(code)
        path = error.get("path")
        if isinstance(path, list) and 1 <= len(path) <= 24:
            names = [part for part in path if isinstance(part, str)]
            if (
                names
                and len(names) <= 12
                and all(name in GRAPHQL_FIELDS for name in names)
                and all(
                    isinstance(part, str)
                    or (isinstance(part, int) and not isinstance(part, bool) and part >= 0)
                    for part in path
                )
            ):
                paths.add(".".join(names))
        message = error.get("message")
        recognized = False
        if isinstance(message, str) and len(message) <= 2048:
            for issue, pattern in _VALIDATION_PATTERNS:
                if match := pattern.fullmatch(message):
                    issues.add(issue)
                    for key, value in match.groupdict().items():
                        if key in {"field", "argument"} and value in GRAPHQL_FIELDS:
                            fields.add(value)
                        elif key in {"type", "expected"} and value:
                            name = value.strip("[]!")
                            if name in GRAPHQL_TYPES:
                                types.add(name)
                    recognized = True
                    break
        if not recognized:
            unrecognized += 1
    return ProviderErrorInfo(
        category,
        status,
        min(32, len(errors)),
        unrecognized,
        tuple(sorted(codes)),
        tuple(sorted(paths)),
        tuple(sorted(fields)),
        tuple(sorted(types)),
        tuple(sorted(issues)),
    )


def retry_after_seconds(value: str | None) -> float:
    """Accept bounded delta-seconds or HTTP dates; never spin on a 429."""
    try:
        seconds = float(value) if value is not None else 300.0
    except TypeError, ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = (stamp - datetime.now(timezone.utc)).total_seconds()
        except TypeError, ValueError, OverflowError:
            seconds = 300.0
    if not 1 <= seconds <= MAX_RETRY_AFTER:
        seconds = min(MAX_RETRY_AFTER, max(1, seconds)) if seconds == seconds else 300.0
    return seconds


class OptimiseClient:
    """API operations cannot accept a URL, an arbitrary document, or a mutation."""

    def __init__(
        self,
        session,
        username: str,
        refresh_token: str | None = None,
        *,
        executor: Callable | None = None,
        token_callback: Callable | None = None,
    ) -> None:
        self._session = session
        self._executor = executor or asyncio.to_thread
        self._token_callback = token_callback
        self._auth = OptimiseAuthenticator(username, refresh_token)
        self._tokens: TokenSet | None = None
        self._auth_lock = asyncio.Lock()
        self._request_limit = asyncio.Semaphore(2)
        self._retry_at = 0.0
        self._auth_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._last_error_info: dict[tuple[str, str | None], ProviderErrorInfo] = {}

    def last_error_info(self, operation: str, site_id: str | None) -> ProviderErrorInfo | None:
        """Read cached metadata only; this method performs no IO or token access."""
        return self._last_error_info.get((operation, site_id))

    def _record_error(self, operation: str, site_id: str | None, info: ProviderErrorInfo) -> None:
        key = (operation, site_id)
        if key not in self._last_error_info and len(self._last_error_info) >= 640:
            self._last_error_info.pop(next(iter(self._last_error_info)))
        self._last_error_info[key] = info

    async def _http_error_info(self, response) -> ProviderErrorInfo:
        """Inspect bounded JSON on known error statuses, without accepting its data."""
        payload = None
        if response.status in {400, 403, 422}:
            chunks, total = [], 0
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > min(MAX_RESPONSE_BYTES, 64 * 1024):
                        break
                    chunks.append(chunk)
                else:
                    payload = json.loads(b"".join(chunks))
            except aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError:
                pass
        return provider_error_info("http", response.status, payload)

    async def _query_tokens(self, operation, site_id, rejected_token=None):
        try:
            return await self._get_tokens(rejected_token=rejected_token)
        except RateLimited:
            self._record_error(operation, site_id, ProviderErrorInfo("rate_limited"))
            raise
        except AuthenticationError:
            self._record_error(operation, site_id, ProviderErrorInfo("authentication"))
            raise
        except CannotConnect:
            category = "stopped" if self._closing else "authentication_connection"
            self._record_error(operation, site_id, ProviderErrorInfo(category))
            raise

    @property
    def refresh_token(self) -> str | None:
        return self._auth.refresh_token

    @property
    def account_subject(self) -> str | None:
        return self._tokens.subject if self._tokens else None

    def set_token_callback(self, callback: Callable | None) -> None:
        self._token_callback = callback

    async def _persist_refresh(self, refresh_token: str) -> None:
        if self._token_callback:
            result = self._token_callback(refresh_token)
            if inspect.isawaitable(result):
                await result

    async def _accept_tokens(self, tokens: TokenSet) -> None:
        self._tokens = tokens
        self._auth.refresh_token = tokens.refresh_token
        await self._persist_refresh(tokens.refresh_token)

    async def _auth_job(self, function: Callable, *args) -> TokenSet:
        """Finish token rotation/persistence before propagating cancellation.

        Cancelling an executor Future cannot stop its worker thread. Shield the
        complete SDK + acceptance + storage operation, keeping the auth lock until
        it finishes so another coordinator cannot reuse an invalidated token.
        """
        if self._closing:
            raise CannotConnect("Optimise client is stopped")
        if time.monotonic() < self._retry_at:
            raise RateLimited(self._retry_at - time.monotonic())

        async def complete() -> TokenSet:
            previous_refresh = self._auth.refresh_token
            try:
                tokens = await self._executor(function, *args)
            except RateLimited as err:
                delay = retry_after_seconds(str(err.retry_after))
                self._retry_at = max(self._retry_at, time.monotonic() + delay)
                raise RateLimited(delay) from None
            except Exception:
                # A rotating token can arrive before a transient JWKS failure.
                # The authenticator exposes only that trusted renewable token;
                # unverified ID/access JWTs are never accepted for API calls.
                rotated = self._auth.refresh_token
                if rotated and rotated != previous_refresh:
                    await self._persist_refresh(rotated)
                raise
            await self._accept_tokens(tokens)
            return tokens

        task = asyncio.create_task(complete(), name="optimise-auth-session")
        self._auth_tasks.add(task)
        cancelled = False
        try:
            while True:
                try:
                    tokens = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return tokens
        finally:
            self._auth_tasks.discard(task)

    async def async_shutdown(self) -> None:
        """Stop new requests and drain critical auth jobs; keep HA's session open."""
        self._closing = True
        if self._auth_tasks:
            await asyncio.shield(asyncio.gather(*self._auth_tasks, return_exceptions=True))

    async def async_sign_in(self, password: str) -> None:
        async with self._auth_lock:
            await self._auth_job(self._auth.sign_in, password)

    async def async_complete_mfa(self, code: str) -> None:
        async with self._auth_lock:
            await self._auth_job(self._auth.complete_mfa, code)

    async def _get_tokens(self, rejected_token: str | None = None) -> TokenSet:
        async with self._auth_lock:
            if self._closing:
                raise CannotConnect("Optimise client is stopped")
            if time.monotonic() < self._retry_at:
                raise RateLimited(self._retry_at - time.monotonic())
            current = self._tokens
            rejected = current and current.id_token == rejected_token
            if current and not rejected and current.expires_at - time.time() > TOKEN_REFRESH_MARGIN:
                return current
            tokens = await self._auth_job(self._auth.refresh)
            return tokens

    async def async_discover_sites(self) -> list[dict]:
        return await self._query("sites", None)

    async def async_fetch_section(self, section: str, site_id: str) -> dict:
        if section not in SECTIONS or section in _EV_FLEET_MODULES:
            raise ValueError("Unsupported read-only section")
        if not isinstance(site_id, str) or not site_id or len(site_id) > 512:
            raise ValueError("Invalid site selection")
        if section == "status":
            return await self._status(site_id)
        return await self._query(section, site_id)

    async def async_fetch_ev_fleet_section(self, section: str, site_id: str, vehicles) -> dict:
        """Read optional per-car modules using only a supplied current fleet.

        The coordinator owns fleet freshness; this client never discovers or caches
        vehicle telemetry to make these calls. A failed module gets fresh unknown
        values, while fatal auth/rate-limit/cancellation stops the whole operation.
        """
        if section not in _EV_FLEET_MODULES or not _identifier(site_id):
            raise ValueError("Unsupported vehicle telemetry section")
        fleet = _fleet_metadata(vehicles)
        if self._closing:
            self._record_error(section, site_id, ProviderErrorInfo("stopped"))
            raise CannotConnect("Optimise client is stopped")
        rows, failures = [], []
        success_count = 0
        previous_info = self.last_error_info(section, site_id)
        for metadata in fleet:
            row = dict(metadata)
            for operation, module in _EV_FLEET_MODULES[section]:
                unknown = _PARSERS[operation]({"data": {EV_FLEET_ROOTS[operation]: None}})[module]
                try:
                    parsed = await self._query(operation, site_id, entity_id=metadata["entity_id"])
                except AuthenticationError, RateLimited:
                    info = self.last_error_info(operation, site_id)
                    if info is not None:
                        self._record_error(section, site_id, info)
                    raise
                except CannotConnect:
                    if self._closing:
                        self._record_error(section, site_id, ProviderErrorInfo("stopped"))
                        raise
                    failures.append(
                        (EV_FLEET_ROOTS[operation], self.last_error_info(operation, site_id))
                    )
                    row[module] = {**unknown, "query_success": False, "fetched_at": None}
                else:
                    success_count += 1
                    row[module] = {
                        **parsed[module],
                        "query_success": True,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                if operation == section and previous_info is not None:
                    self._record_error(section, site_id, previous_info)
            rows.append(row)
        if failures:
            self._record_error(section, site_id, _fleet_error_info(failures, success_count))
            if not success_count:
                raise CannotConnect("Optimise vehicle telemetry is temporarily unavailable")
        else:
            self._last_error_info.pop((section, site_id), None)
        return {"vehicles": rows, "vehicle_count": len(fleet)}

    async def _status(self, site_id: str) -> dict:
        """Keep status roots independent while retaining one coordinator contract."""
        result = parse_status({"data": {}})
        failures = []
        success_count = 0
        previous_status_info = self.last_error_info("status", site_id)
        for operation, root in STATUS_ROOTS.items():
            try:
                parsed = await self._query(operation, site_id)
            except AuthenticationError, RateLimited:
                info = self.last_error_info(operation, site_id)
                if info is not None:
                    self._record_error("status", site_id, info)
                raise
            except CannotConnect:
                if self._closing:
                    self._record_error("status", site_id, ProviderErrorInfo("stopped"))
                    raise
                failures.append((root, self.last_error_info(operation, site_id)))
            else:
                success_count += 1
                result.update({field: parsed[field] for field in _STATUS_FIELDS[root]})
            if operation == "status" and previous_status_info is not None:
                # A successful summary alone does not complete the whole status poll.
                self._record_error("status", site_id, previous_status_info)
        if failures:
            infos = [info for _root, info in failures if info is not None]
            statuses = {info.http_status for info in infos if info.http_status is not None}
            categories = {info.category for info in infos}
            self._record_error(
                "status",
                site_id,
                ProviderErrorInfo(
                    category="partial"
                    if success_count
                    else next(iter(categories))
                    if len(categories) == 1
                    else "unknown",
                    http_status=next(iter(statuses)) if len(statuses) == 1 else None,
                    graphql_error_count=min(32, sum(info.graphql_error_count for info in infos)),
                    unrecognized_error_count=min(
                        32, sum(info.unrecognized_error_count for info in infos)
                    ),
                    graphql_codes=tuple(
                        sorted({code for info in infos for code in info.graphql_codes})
                    ),
                    graphql_paths=tuple(
                        sorted({path for info in infos for path in info.graphql_paths})
                    ),
                    graphql_fields=tuple(
                        sorted({field for info in infos for field in info.graphql_fields})
                    ),
                    graphql_types=tuple(
                        sorted({kind for info in infos for kind in info.graphql_types})
                    ),
                    graphql_issues=tuple(
                        sorted({issue for info in infos for issue in info.graphql_issues})
                    ),
                    failed_roots=tuple(root for root, _info in failures),
                    failed_request_count=len(failures),
                ),
            )
            if not success_count:
                raise CannotConnect("Optimise status modules are temporarily unavailable")
        else:
            self._last_error_info.pop(("status", site_id), None)
        return result

    async def _query(self, operation: str, site_id: str | None, *, entity_id: str | None = None):
        if self._closing:
            self._record_error(operation, site_id, ProviderErrorInfo("stopped"))
            raise CannotConnect("Optimise client is stopped")
        if operation not in OPERATIONS or operation not in _PARSERS:
            raise ValueError("Unsupported read-only operation")
        op = OPERATIONS[operation]
        document = op["query"]
        if not document.lstrip().startswith("query ") or "mutation " in document:
            raise ValueError("Only fixed read-only queries may be sent")
        if time.monotonic() < self._retry_at:
            self._record_error(operation, site_id, ProviderErrorInfo("rate_limited"))
            raise RateLimited(self._retry_at - time.monotonic())
        if operation in EV_FLEET_ROOTS:
            if not _identifier(entity_id) or (
                operation != "ev_history" and not _identifier(site_id)
            ):
                raise ValueError("Invalid vehicle telemetry selection")
            variables = {"entityId": entity_id}
            if operation != "ev_history":
                variables["siteId"] = site_id
        else:
            if entity_id is not None:
                raise ValueError("Vehicle identity is not accepted by this operation")
            variables = {"siteId": site_id} if site_id is not None else {}
        tokens = await self._query_tokens(operation, site_id)
        for attempt in range(2):
            status = None
            try:
                async with self._request_limit:
                    if self._closing:
                        self._record_error(operation, site_id, ProviderErrorInfo("stopped"))
                        raise CannotConnect("Optimise client is stopped")
                    if time.monotonic() < self._retry_at:
                        self._record_error(operation, site_id, ProviderErrorInfo("rate_limited"))
                        raise RateLimited(self._retry_at - time.monotonic())
                    async with self._session.post(
                        GRAPHQL_URL,
                        json={
                            "operationName": op["operation_name"],
                            "query": document,
                            "variables": variables,
                        },
                        headers={"Authorization": tokens.id_token, "Accept": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                        allow_redirects=False,
                    ) as response:
                        status = response.status
                        if response.status == 401:
                            self._record_error(
                                operation, site_id, ProviderErrorInfo("http", status)
                            )
                            if attempt == 0:
                                tokens = await self._query_tokens(
                                    operation, site_id, tokens.id_token
                                )
                                continue
                            raise AuthenticationError("Optimise session requires a new login")
                        if response.status == 403:
                            self._record_error(
                                operation, site_id, await self._http_error_info(response)
                            )
                            raise CannotConnect("Optimise access is currently unavailable")
                        if response.status == 429:
                            self._record_error(
                                operation, site_id, ProviderErrorInfo("http", status)
                            )
                            delay = retry_after_seconds(response.headers.get("Retry-After"))
                            self._retry_at = time.monotonic() + delay
                            raise RateLimited(delay)
                        if response.status != 200:
                            self._record_error(
                                operation, site_id, await self._http_error_info(response)
                            )
                            raise CannotConnect("Optimise data service is temporarily unavailable")
                        chunks = []
                        total = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > MAX_RESPONSE_BYTES:
                                self._record_error(
                                    operation,
                                    site_id,
                                    ProviderErrorInfo("response_too_large", status),
                                )
                                raise CannotConnect("Optimise response exceeds the allowed size")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        try:
                            payload = json.loads(body)
                        except ValueError, UnicodeError:
                            self._record_error(
                                operation, site_id, ProviderErrorInfo("invalid_json", status)
                            )
                            raise CannotConnect("Optimise returned an invalid response") from None
                has_errors = isinstance(payload, dict) and bool(payload.get("errors"))
                if has_errors:
                    self._record_error(
                        operation, site_id, provider_error_info("graphql", status, payload)
                    )
                if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
                    codes = {
                        error.get("extensions", {}).get("code")
                        for error in payload["errors"]
                        if isinstance(error, dict)
                        and isinstance(error.get("extensions"), dict)
                        and isinstance(error["extensions"].get("code"), str)
                    }
                    if codes & {"UNAUTHENTICATED", "NOT_AUTHENTICATED", "UNAUTHORIZED"}:
                        if attempt == 0:
                            tokens = await self._query_tokens(operation, site_id, tokens.id_token)
                            continue
                        raise AuthenticationError("Optimise session requires a new login")
                    if codes & {"RATE_LIMITED", "TOO_MANY_REQUESTS"}:
                        self._retry_at = time.monotonic() + 300
                        raise RateLimited(300)
                try:
                    parsed = _PARSERS[operation](payload)
                except DataError, KeyError, TypeError, ValueError:
                    if not has_errors:
                        self._record_error(
                            operation, site_id, ProviderErrorInfo("parser_invalid", status)
                        )
                    raise CannotConnect("Optimise returned incomplete or invalid data") from None
                self._last_error_info.pop((operation, site_id), None)
                return parsed
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                category = "timeout" if isinstance(err, asyncio.TimeoutError) else "network"
                self._record_error(operation, site_id, ProviderErrorInfo(category, status))
                raise CannotConnect("Optimise data service is temporarily unavailable") from None
        raise AuthenticationError("Optimise session requires a new login")


# Compatibility import for callers of the original price-only beta.
PriceFeedError = CannotConnect
