"""Cognito SRP sign-in and rotating refresh tokens, with no saved password.

All methods here block and must run in Home Assistant's executor. Only the fixed
production pool is used. Sign-in never changes passwords, confirms devices, or
silently completes a provider challenge on the user's behalf.
"""

import json
import math
import re
import time
import urllib.request
from dataclasses import dataclass, field

from botocore.config import Config
from botocore.exceptions import ClientError
from pycognito import Cognito
from pycognito.exceptions import (
    ForceChangePasswordException,
    MFAChallengeException,
    SMSMFAChallengeException,
    SoftwareTokenMFAChallengeException,
    TokenVerificationException,
)

from .const import CLIENT_ID, JWKS_URL, REGION, USER_POOL_ID
from .exceptions import AuthenticationError, CannotConnect, ChallengeRequired, RateLimited


@dataclass(frozen=True, slots=True)
class TokenSet:
    """Verified ID token and renewable session, deliberately excluded from repr."""

    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    subject: str = field(repr=False)


class _ProductionCognito(Cognito):
    """Use only E.ON's app-configured Cognito pool's public signing keys.

    The base library accepts an environment JWKS override. This integration
    always fetches the issuer's real HTTPS keys and refreshes once on rotation.
    urllib does not implicitly read .netrc credentials for this public request.
    """

    def get_keys(self):
        if self.pool_jwk:
            return self.pool_jwk
        with urllib.request.urlopen(JWKS_URL, timeout=15) as response:
            payload = json.loads(response.read(256 * 1024))
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise CannotConnect("Optimise signing-key service returned an invalid response")
        self.pool_jwk = payload
        return payload

    def get_key(self, kid):
        for attempt in range(2):
            for key in self.get_keys()["keys"]:
                if isinstance(key, dict) and key.get("kid") == kid:
                    return key
            if attempt == 0:
                self.pool_jwk = None
        raise TokenVerificationException("No matching production signing key")


class OptimiseAuthenticator:
    """Keep a renewable Cognito session in memory; never retain the password."""

    def __init__(self, username: str, refresh_token: str | None = None) -> None:
        self.username = username.strip()
        self.refresh_token = refresh_token
        self._cognito: Cognito | None = None
        self._pending_challenge: str | None = None

    def _client(self) -> Cognito:
        if self._cognito is None:
            self._cognito = _ProductionCognito(
                USER_POOL_ID,
                CLIENT_ID,
                user_pool_region=REGION,
                username=self.username,
                refresh_token=self.refresh_token,
                # InitiateAuth/SRP are public Cognito operations, not IAM calls.
                # Explicit placeholders prevent probing an unrelated AWS profile.
                access_key="unused",
                secret_key="unused",
                botocore_config=Config(
                    connect_timeout=10,
                    read_timeout=20,
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._cognito

    def _tokens(self, client: Cognito) -> TokenSet:
        claims = client.id_claims
        if not isinstance(claims, dict):
            raise CannotConnect("Optimise did not provide verified identity claims")
        expiry = claims.get("exp")
        subject = claims.get("sub")
        if (
            not isinstance(expiry, (int, float))
            or isinstance(expiry, bool)
            or not math.isfinite(expiry)
            or expiry <= time.time()
            or not isinstance(subject, str)
            or not subject
            or not isinstance(client.id_token, str)
            or not client.id_token
        ):
            raise CannotConnect("Optimise returned an invalid renewable session")
        refresh = client.refresh_token or self.refresh_token
        if not isinstance(refresh, str) or not refresh:
            raise AuthenticationError("Optimise did not provide a renewable session")
        self.refresh_token = refresh
        return TokenSet(client.id_token, refresh, float(expiry), subject)

    @staticmethod
    def _translate(exc: Exception) -> None:
        if isinstance(exc, (ForceChangePasswordException, MFAChallengeException)):
            if isinstance(exc, ForceChangePasswordException):
                challenge = "new_password_required"
            elif isinstance(exc, SMSMFAChallengeException):
                challenge = "sms_mfa"
            elif isinstance(exc, SoftwareTokenMFAChallengeException):
                challenge = "totp_mfa"
            else:
                challenge = "mfa_required"
            raise ChallengeRequired(challenge) from None
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {
                "NotAuthorizedException",
                "UserNotFoundException",
                "UserNotConfirmedException",
                "PasswordResetRequiredException",
                "RefreshTokenReuseException",
                "CodeMismatchException",
                "ExpiredCodeException",
            }:
                raise AuthenticationError("Optimise login requires user attention") from None
            if code in {"TooManyRequestsException", "LimitExceededException"}:
                raise RateLimited(300) from None
            raise CannotConnect(
                "Optimise authentication service is temporarily unavailable"
            ) from None
        if isinstance(exc, OptimiseErrorTypes):
            raise exc from None
        # Includes JWKS/TLS failures, SDK transports and verification failures.
        # A key rollover or network outage must not be mistaken for a bad password.
        raise CannotConnect("Optimise authentication service is temporarily unavailable") from None

    def sign_in(self, password: str) -> TokenSet:
        try:
            client = self._client()
            client.authenticate(password)
            return self._tokens(client)
        except Exception as exc:
            if isinstance(exc, SMSMFAChallengeException):
                self._pending_challenge = "sms_mfa"
            elif isinstance(exc, SoftwareTokenMFAChallengeException):
                self._pending_challenge = "totp_mfa"
            self._translate(exc)
            raise AssertionError("unreachable") from None

    def complete_mfa(self, code: str) -> TokenSet:
        """Respond only to an already-issued user-entered MFA challenge."""
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise AuthenticationError("Enter the six-digit verification code")
        try:
            client = self._client()
            if self._pending_challenge == "sms_mfa":
                client.respond_to_sms_mfa_challenge(code)
            elif self._pending_challenge == "totp_mfa":
                client.respond_to_software_token_mfa_challenge(code)
            else:
                raise AuthenticationError("Start a new Optimise login")
            tokens = self._tokens(client)
            self._pending_challenge = None
            return tokens
        except Exception as exc:
            self._translate(exc)
            raise AssertionError("unreachable") from None

    def refresh(self) -> TokenSet:
        if not self.refresh_token:
            raise AuthenticationError("Optimise needs a new login")
        try:
            client = self._client()
            # This API supports both rotating and non-rotating Cognito refresh
            # sessions. Preserve any newly rotated refresh token immediately.
            response = client.client.get_tokens_from_refresh_token(
                ClientId=CLIENT_ID, RefreshToken=self.refresh_token
            )
            result = response.get("AuthenticationResult")
            if not isinstance(result, dict):
                raise CannotConnect("Optimise returned an invalid refresh response")
            rotated = result.get("RefreshToken")
            if rotated is not None:
                if not isinstance(rotated, str) or not rotated or len(rotated) > 16384:
                    raise CannotConnect("Optimise returned an invalid refresh response")
                # The trusted HTTPS refresh response may invalidate the old token
                # before a subsequent JWKS request succeeds. Retain its renewable
                # session even if validating the new short-lived JWT must retry.
                client.refresh_token = rotated
                self.refresh_token = rotated
            client.verify_token(result.get("AccessToken"), "access_token", "access")
            client.verify_token(result.get("IdToken"), "id_token", "id")
            return self._tokens(client)
        except Exception as exc:
            self._translate(exc)
            raise AssertionError("unreachable") from None


OptimiseErrorTypes = (AuthenticationError, CannotConnect)
