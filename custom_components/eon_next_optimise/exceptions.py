"""Sanitized integration errors; provider responses and secrets are never messages."""

import re
from dataclasses import dataclass

from .queries import EV_FLEET_ROOTS, OPERATIONS, STATUS_ROOTS

FAILED_ROOT_NAMES = frozenset((*STATUS_ROOTS.values(), *EV_FLEET_ROOTS.values()))
MAX_FAILED_REQUESTS = 256

ERROR_CATEGORIES = frozenset(
    {
        "http",
        "graphql",
        "parser_invalid",
        "invalid_json",
        "response_too_large",
        "network",
        "timeout",
        "authentication",
        "authentication_connection",
        "rate_limited",
        "stopped",
        "unknown",
        "partial",
    }
)
GRAPHQL_ERROR_CODES = frozenset(
    {
        "GRAPHQL_VALIDATION_FAILED",
        "GRAPHQL_PARSE_FAILED",
        "BAD_USER_INPUT",
        "INTERNAL_SERVER_ERROR",
        "FORBIDDEN",
        "UNAUTHENTICATED",
        "NOT_AUTHENTICATED",
        "UNAUTHORIZED",
        "RATE_LIMITED",
        "TOO_MANY_REQUESTS",
        "PERSISTED_QUERY_NOT_FOUND",
        "PERSISTED_QUERY_NOT_SUPPORTED",
        "BAD_REQUEST",
        "SERVICE_UNAVAILABLE",
    }
)
GRAPHQL_ERROR_ISSUES = frozenset(
    {
        "cannot_query_field",
        "unknown_argument",
        "variable_type",
        "required_argument",
        "selection_shape",
        "unknown_type",
        "fragment_type",
    }
)
GRAPHQL_TYPES = frozenset(
    {
        "String",
        "SmartShiftMetricsWithInterval",
        "SmartShiftDataError",
        "PastAndForecastData",
        "SmartShiftBatteryActionDecision",
        "SmartShiftSolarActionDecision",
    }
)
# These literals come only from our fixed public documents, never a response.
GRAPHQL_FIELDS = (
    frozenset(
        re.findall(r"[A-Za-z_][A-Za-z_0-9]*", " ".join(op["query"] for op in OPERATIONS.values()))
    )
    - GRAPHQL_TYPES
    - {"query", "on"}
    - {op["operation_name"] for op in OPERATIONS.values()}
)


def _limited_names(values, permitted, limit):
    if not isinstance(values, (tuple, list)):
        return []
    return sorted({value for value in values if isinstance(value, str) and value in permitted})[
        :limit
    ]


@dataclass(frozen=True, slots=True)
class ProviderErrorInfo:
    """Only cached diagnostic metadata; deliberately no body/message/identity field."""

    category: str
    http_status: int | None = None
    graphql_error_count: int = 0
    unrecognized_error_count: int = 0
    graphql_codes: tuple[str, ...] = ()
    graphql_paths: tuple[str, ...] = ()
    graphql_fields: tuple[str, ...] = ()
    graphql_types: tuple[str, ...] = ()
    graphql_issues: tuple[str, ...] = ()
    failed_roots: tuple[str, ...] = ()
    failed_request_count: int = 0

    def as_dict(self) -> dict:
        """Revalidate the allowlist at the diagnostics boundary as defence in depth."""
        paths = []
        if isinstance(self.graphql_paths, (tuple, list)):
            paths = sorted(
                {
                    path
                    for path in self.graphql_paths
                    if isinstance(path, str)
                    and 1 <= len(path.split(".")) <= 12
                    and all(part in GRAPHQL_FIELDS for part in path.split("."))
                }
            )[:16]

        def count(value):
            return (
                min(32, max(0, value))
                if isinstance(value, int) and not isinstance(value, bool)
                else 0
            )

        status = self.http_status
        return {
            "category": self.category
            if isinstance(self.category, str) and self.category in ERROR_CATEGORIES
            else "unknown",
            "http_status": status
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
            else None,
            "graphql_error_count": count(self.graphql_error_count),
            "unrecognized_error_count": count(self.unrecognized_error_count),
            "graphql_codes": _limited_names(self.graphql_codes, GRAPHQL_ERROR_CODES, 16),
            "graphql_paths": paths,
            "graphql_fields": _limited_names(self.graphql_fields, GRAPHQL_FIELDS, 32),
            "graphql_types": _limited_names(self.graphql_types, GRAPHQL_TYPES, 16),
            "graphql_issues": _limited_names(self.graphql_issues, GRAPHQL_ERROR_ISSUES, 8),
            "failed_roots": _limited_names(self.failed_roots, FAILED_ROOT_NAMES, 7),
            "failed_request_count": min(MAX_FAILED_REQUESTS, max(0, self.failed_request_count))
            if isinstance(self.failed_request_count, int)
            and not isinstance(self.failed_request_count, bool)
            else 0,
        }


class OptimiseError(Exception):
    """Base error for this integration."""


class AuthenticationError(OptimiseError):
    """The account needs a new interactive login."""


class ChallengeRequired(AuthenticationError):
    """A Cognito login challenge needs user attention."""

    def __init__(self, challenge: str = "challenge_required") -> None:
        self.challenge = challenge
        super().__init__("Optimise login requires an additional verification step")


class CannotConnect(OptimiseError):
    """A temporary provider, network, or response failure."""


class RateLimited(CannotConnect):
    """Provider rate limit with a bounded retry time in seconds."""

    def __init__(self, retry_after: float = 300) -> None:
        self.retry_after = retry_after
        super().__init__("Optimise service is temporarily rate limited")
