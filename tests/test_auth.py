"""Synthetic SRP/refresh/MFA and real RSA verification, without provider requests."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import jwt
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pycognito.exceptions import (
    ForceChangePasswordException,
    SMSMFAChallengeException,
    SoftwareTokenMFAChallengeException,
    TokenVerificationException,
)

from custom_components.eon_next_optimise import auth
from custom_components.eon_next_optimise.api import OptimiseClient
from custom_components.eon_next_optimise.const import (
    CLIENT_ID,
    ISSUER,
    JWKS_URL,
    REGION,
    USER_POOL_ID,
)
from custom_components.eon_next_optimise.exceptions import (
    AuthenticationError,
    CannotConnect,
    ChallengeRequired,
    RateLimited,
)


def sdk_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "synthetic-provider-private-marker"}},
        "SyntheticAuthentication",
    )


@pytest.fixture
def fake_cognito():
    """The SDK boundary has already verified these synthetic claims."""
    return SimpleNamespace(
        id_token="synthetic-id-token",
        refresh_token="synthetic-refresh-token",
        id_claims={"exp": time.time() + 900, "sub": "synthetic-subject"},
        authenticate=Mock(),
        respond_to_sms_mfa_challenge=Mock(),
        respond_to_software_token_mfa_challenge=Mock(),
        verify_token=Mock(),
        client=SimpleNamespace(get_tokens_from_refresh_token=Mock()),
    )


@pytest.fixture
def authenticator(fake_cognito):
    result = auth.OptimiseAuthenticator("  synthetic@example.invalid  ")
    result._cognito = fake_cognito
    return result


@pytest.fixture(scope="module")
def signing_keys():
    """Keys are generated for this test run, unrelated to any real account."""
    keys = {}
    for kid in ("synthetic-old", "synthetic-new", "synthetic-untrusted"):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        public.update(kid=kid, use="sig", alg="RS256")
        keys[kid] = private, public
    return keys


@pytest.fixture
def production_cognito(signing_keys):
    """Use real pycognito verification without constructing a network SDK client."""
    result = object.__new__(auth._ProductionCognito)
    result.user_pool_id = USER_POOL_ID
    result.user_pool_region = REGION
    result.client_id = CLIENT_ID
    result.pool_domain_url = None
    result.pool_jwk = {"keys": [signing_keys["synthetic-old"][1]]}
    result.id_token = result.access_token = None
    result.id_claims = result.access_claims = None
    result.refresh_token = "synthetic-original-refresh"
    result.client = SimpleNamespace(get_tokens_from_refresh_token=Mock())
    result.authenticate = Mock()
    return result


def signed_token(signing_keys, *, token_use="id", kid="synthetic-old", claims=None):
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "client_id": CLIENT_ID,
        "exp": int(time.time()) + 900,
        "iat": int(time.time()) - 10,
        "sub": "synthetic-subject",
        "token_use": token_use,
    }
    if token_use == "access":
        # Cognito access JWTs identify the app with client_id, not aud.
        payload.pop("aud")
    payload.update(claims or {})
    return jwt.encode(payload, signing_keys[kid][0], algorithm="RS256", headers={"kid": kid})


def test_sign_in_never_retains_password(authenticator, fake_cognito):
    tokens = authenticator.sign_in("synthetic-password-never-save")
    fake_cognito.authenticate.assert_called_once_with("synthetic-password-never-save")
    assert tokens.id_token == "synthetic-id-token"
    assert tokens.refresh_token == "synthetic-refresh-token"
    assert tokens.subject == "synthetic-subject"
    assert tokens.expires_at == fake_cognito.id_claims["exp"]
    assert authenticator.username == "synthetic@example.invalid"
    assert not hasattr(authenticator, "password")
    assert "synthetic-password-never-save" not in vars(authenticator).values()
    assert "synthetic-id-token" not in repr(tokens)
    assert "synthetic-refresh-token" not in repr(tokens)
    assert "synthetic-subject" not in repr(tokens)


def test_real_srp_wire_flow_never_sends_or_stores_plain_password(production_cognito, signing_keys):
    """Exercise the actual library SRP flow against a synthetic Cognito server."""
    production_cognito.username = "synthetic@example.invalid"
    production_cognito.client_secret = None
    production_cognito.authenticate = auth.Cognito.authenticate.__get__(production_cognito)
    sdk = SimpleNamespace(
        initiate_auth=Mock(
            return_value={
                "ChallengeName": "PASSWORD_VERIFIER",
                "ChallengeParameters": {
                    "USER_ID_FOR_SRP": "synthetic-subject",
                    "SALT": "deadbeef",
                    "SRP_B": "2",
                    "SECRET_BLOCK": "c3ludGhldGljLWNoYWxsZW5nZQ==",
                },
            }
        ),
        respond_to_auth_challenge=Mock(
            return_value={
                "AuthenticationResult": {
                    "AccessToken": signed_token(signing_keys, token_use="access"),
                    "IdToken": signed_token(signing_keys),
                    "RefreshToken": "synthetic-new-refresh",
                    "TokenType": "Bearer",
                },
            }
        ),
    )
    production_cognito.client = sdk
    authenticator = auth.OptimiseAuthenticator("synthetic@example.invalid")
    authenticator._cognito = production_cognito
    assert (
        authenticator.sign_in("synthetic-password-do-not-send").refresh_token
        == "synthetic-new-refresh"
    )
    initial = sdk.initiate_auth.call_args.kwargs
    proof = sdk.respond_to_auth_challenge.call_args.kwargs
    assert initial["AuthFlow"] == "USER_SRP_AUTH"
    assert initial["AuthParameters"]["SRP_A"]
    assert "PASSWORD" not in initial["AuthParameters"]
    assert proof["ChallengeName"] == "PASSWORD_VERIFIER"
    assert proof["ChallengeResponses"]["PASSWORD_CLAIM_SIGNATURE"]
    assert "synthetic-password-do-not-send" not in repr((initial, proof))
    assert not hasattr(authenticator, "password")
    assert not hasattr(production_cognito, "password")


def test_sdk_uses_fixed_pool_and_no_ambient_aws_credentials(monkeypatch, fake_cognito):
    factory = Mock(return_value=fake_cognito)
    monkeypatch.setattr(auth, "_ProductionCognito", factory)
    result = auth.OptimiseAuthenticator("synthetic@example.invalid", "synthetic-refresh")
    assert result._client() is result._client()
    factory.assert_called_once()
    args, kwargs = factory.call_args
    assert args == (USER_POOL_ID, CLIENT_ID)
    assert kwargs["user_pool_region"] == REGION
    assert kwargs["access_key"] == kwargs["secret_key"] == "unused"
    assert "endpoint_url" not in kwargs
    assert kwargs["botocore_config"].connect_timeout == 10
    assert kwargs["botocore_config"].read_timeout == 20


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (sdk_error("NotAuthorizedException"), AuthenticationError),
        (sdk_error("UserNotFoundException"), AuthenticationError),
        (sdk_error("UserNotConfirmedException"), AuthenticationError),
        (sdk_error("PasswordResetRequiredException"), AuthenticationError),
        (sdk_error("TooManyRequestsException"), RateLimited),
        (sdk_error("LimitExceededException"), RateLimited),
        (sdk_error("InternalErrorException"), CannotConnect),
        (EndpointConnectionError(endpoint_url="https://synthetic.invalid/private"), CannotConnect),
        (TokenVerificationException("synthetic-provider-private-marker"), CannotConnect),
    ],
)
def test_authentication_errors_are_classified_and_redacted(
    authenticator, fake_cognito, error, expected, caplog
):
    fake_cognito.authenticate.side_effect = error
    with pytest.raises(expected) as caught:
        authenticator.sign_in("synthetic-password-never-save")
    assert "synthetic" not in str(caught.value)
    assert "synthetic" not in caplog.text
    assert caught.value.__suppress_context__
    if isinstance(caught.value, RateLimited):
        assert caught.value.retry_after == 300


@pytest.mark.parametrize(
    ("error", "challenge"),
    [
        (ForceChangePasswordException("synthetic-private-marker"), "new_password_required"),
        (SMSMFAChallengeException("synthetic-private-marker", {}), "sms_mfa"),
        (SoftwareTokenMFAChallengeException("synthetic-private-marker", {}), "totp_mfa"),
    ],
)
def test_provider_challenges_require_user_input(authenticator, fake_cognito, error, challenge):
    fake_cognito.authenticate.side_effect = error
    with pytest.raises(ChallengeRequired) as caught:
        authenticator.sign_in("synthetic-password")
    assert caught.value.challenge == challenge
    assert "synthetic" not in str(caught.value)
    assert not hasattr(authenticator, "password")


@pytest.mark.parametrize("challenge", ["sms_mfa", "totp_mfa"])
def test_mfa_completes_existing_challenge_without_new_srp(authenticator, fake_cognito, challenge):
    exception = (
        SMSMFAChallengeException if challenge == "sms_mfa" else SoftwareTokenMFAChallengeException
    )
    fake_cognito.authenticate.side_effect = exception("synthetic-private-marker", {})
    with pytest.raises(ChallengeRequired):
        authenticator.sign_in("synthetic-password")
    tokens = authenticator.complete_mfa("123456")
    method = (
        fake_cognito.respond_to_sms_mfa_challenge
        if challenge == "sms_mfa"
        else fake_cognito.respond_to_software_token_mfa_challenge
    )
    method.assert_called_once_with("123456")
    assert fake_cognito.authenticate.call_count == 1
    assert tokens.subject == "synthetic-subject"
    assert authenticator._pending_challenge is None


@pytest.mark.parametrize(
    "code", [None, 123456, "12345", "1234567", "abcdef", "１２３４５６", "123 45"]
)
def test_invalid_mfa_code_never_constructs_or_calls_sdk(monkeypatch, code):
    factory = Mock()
    monkeypatch.setattr(auth, "_ProductionCognito", factory)
    with pytest.raises(AuthenticationError):
        auth.OptimiseAuthenticator("synthetic@example.invalid").complete_mfa(code)
    factory.assert_not_called()


def test_wrong_mfa_code_can_retry_existing_challenge(authenticator, fake_cognito):
    authenticator._pending_challenge = "sms_mfa"
    fake_cognito.respond_to_sms_mfa_challenge.side_effect = [
        sdk_error("CodeMismatchException"),
        None,
    ]
    with pytest.raises(AuthenticationError):
        authenticator.complete_mfa("000000")
    assert authenticator._pending_challenge == "sms_mfa"
    assert authenticator.complete_mfa("123456").id_token == "synthetic-id-token"
    fake_cognito.authenticate.assert_not_called()


def test_mfa_without_pending_challenge_is_rejected(authenticator, fake_cognito):
    with pytest.raises(AuthenticationError):
        authenticator.complete_mfa("123456")
    fake_cognito.respond_to_sms_mfa_challenge.assert_not_called()
    fake_cognito.respond_to_software_token_mfa_challenge.assert_not_called()


def test_refresh_without_session_never_constructs_sdk(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(auth, "_ProductionCognito", factory)
    with pytest.raises(AuthenticationError):
        auth.OptimiseAuthenticator("synthetic@example.invalid").refresh()
    factory.assert_not_called()


@pytest.mark.parametrize("rotated", [None, "synthetic-rotated-refresh"])
def test_refresh_verifies_real_tokens_and_preserves_rotation(
    production_cognito, signing_keys, rotated
):
    access = signed_token(signing_keys, token_use="access")
    identity = signed_token(signing_keys)
    result = {"AccessToken": access, "IdToken": identity, "ExpiresIn": 999999}
    if rotated:
        result["RefreshToken"] = rotated
    production_cognito.client.get_tokens_from_refresh_token.return_value = {
        "AuthenticationResult": result
    }
    authenticator = auth.OptimiseAuthenticator(
        "synthetic@example.invalid", "synthetic-original-refresh"
    )
    authenticator._cognito = production_cognito
    tokens = authenticator.refresh()
    production_cognito.client.get_tokens_from_refresh_token.assert_called_once_with(
        ClientId=CLIENT_ID, RefreshToken="synthetic-original-refresh"
    )
    assert tokens.id_token == identity
    assert production_cognito.access_token == access
    assert tokens.refresh_token == (rotated or "synthetic-original-refresh")
    assert authenticator.refresh_token == tokens.refresh_token
    assert tokens.expires_at == production_cognito.id_claims["exp"]
    production_cognito.authenticate.assert_not_called()


def test_revoked_refresh_never_falls_back_to_password_sign_in(authenticator, fake_cognito):
    authenticator.refresh_token = "synthetic-revoked-refresh"
    fake_cognito.client.get_tokens_from_refresh_token.side_effect = sdk_error(
        "NotAuthorizedException"
    )
    with pytest.raises(AuthenticationError) as caught:
        authenticator.refresh()
    fake_cognito.authenticate.assert_not_called()
    assert "synthetic" not in str(caught.value)


@pytest.mark.parametrize(
    "payload", [{}, {"AuthenticationResult": None}, {"AuthenticationResult": []}]
)
def test_malformed_refresh_is_temporary_and_sanitized(authenticator, fake_cognito, payload):
    authenticator.refresh_token = "synthetic-refresh"
    fake_cognito.client.get_tokens_from_refresh_token.return_value = payload
    with pytest.raises(CannotConnect):
        authenticator.refresh()
    fake_cognito.authenticate.assert_not_called()


@pytest.mark.parametrize(
    "claims",
    [
        None,
        {},
        {"exp": True, "sub": "x"},
        {"exp": float("inf"), "sub": "x"},
        {"exp": 0, "sub": "x"},
        {"exp": 9999999999, "sub": ""},
    ],
)
def test_unusable_verified_claims_cannot_become_a_session(authenticator, fake_cognito, claims):
    fake_cognito.id_claims = claims
    with pytest.raises(CannotConnect):
        authenticator.sign_in("synthetic-password")


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://synthetic-wrong-issuer.invalid/pool"},
        {"aud": "synthetic-wrong-client"},
        {"exp": 1},
        {"token_use": "access"},
    ],
)
def test_real_jwt_verification_rejects_wrong_claims(production_cognito, signing_keys, claims):
    token = signed_token(signing_keys, claims=claims)
    with pytest.raises(TokenVerificationException):
        production_cognito.verify_token(token, "id_token", "id")
    assert production_cognito.id_token is None


def test_real_jwt_verification_rejects_untrusted_signature(production_cognito, signing_keys):
    payload = jwt.decode(signed_token(signing_keys), options={"verify_signature": False})
    token = jwt.encode(
        payload,
        signing_keys["synthetic-untrusted"][0],
        algorithm="RS256",
        headers={"kid": "synthetic-old"},
    )
    with pytest.raises(TokenVerificationException):
        production_cognito.verify_token(token, "id_token", "id")


def test_real_verification_rejects_access_token_used_as_id(production_cognito, signing_keys):
    token = signed_token(signing_keys, token_use="access")
    with pytest.raises(TokenVerificationException):
        production_cognito.verify_token(token, "id_token", "id")


def test_invalid_refresh_token_claims_are_not_published(production_cognito, signing_keys):
    production_cognito.client.get_tokens_from_refresh_token.return_value = {
        "AuthenticationResult": {
            "AccessToken": signed_token(signing_keys, token_use="access"),
            "IdToken": signed_token(signing_keys, claims={"iss": "https://synthetic.invalid"}),
            "RefreshToken": "synthetic-untrusted-rotation",
        }
    }
    authenticator = auth.OptimiseAuthenticator(
        "synthetic@example.invalid", "synthetic-original-refresh"
    )
    authenticator._cognito = production_cognito
    with pytest.raises(CannotConnect):
        authenticator.refresh()
    # The renewable token came from the trusted HTTPS SDK response and must
    # survive even though the short-lived JWT failed identity verification.
    assert authenticator.refresh_token == "synthetic-untrusted-rotation"
    assert production_cognito.id_token is None


async def test_jwks_failure_persists_rotation_without_accepting_unverified_identity(
    monkeypatch, production_cognito, signing_keys
):
    access = signed_token(signing_keys, token_use="access", kid="synthetic-new")
    identity = signed_token(signing_keys, kid="synthetic-new")
    sdk = production_cognito.client.get_tokens_from_refresh_token
    sdk.side_effect = [
        {
            "AuthenticationResult": {
                "AccessToken": access,
                "IdToken": identity,
                "RefreshToken": "synthetic-rotated-after-jwks-failure",
            }
        },
        {"AuthenticationResult": {"AccessToken": access, "IdToken": identity}},
    ]
    fetch = Mock(side_effect=OSError("synthetic-provider-private-marker"))
    monkeypatch.setattr(auth.urllib.request, "urlopen", fetch)
    authenticator = auth.OptimiseAuthenticator(
        "synthetic@example.invalid", "synthetic-original-refresh"
    )
    authenticator._cognito = production_cognito
    callback = AsyncMock()
    session = SimpleNamespace(post=Mock())
    client = OptimiseClient(session, "synthetic@example.invalid", token_callback=callback)
    client._auth = authenticator
    old_tokens = auth.TokenSet(
        "synthetic-old-id", "synthetic-original-refresh", time.time() - 1, "synthetic-subject"
    )
    client._tokens = old_tokens
    with pytest.raises(CannotConnect) as caught:
        await client._get_tokens()
    assert "synthetic" not in str(caught.value)
    callback.assert_awaited_once_with("synthetic-rotated-after-jwks-failure")
    assert client.refresh_token == "synthetic-rotated-after-jwks-failure"
    assert client._tokens is old_tokens
    assert production_cognito.id_token is None
    assert production_cognito.access_token is None
    session.post.assert_not_called()

    production_cognito.pool_jwk = {"keys": [signing_keys["synthetic-new"][1]]}
    accepted = await client._get_tokens()
    assert accepted.id_token == identity
    assert production_cognito.access_token == access
    assert accepted.refresh_token == "synthetic-rotated-after-jwks-failure"
    assert [call.kwargs["RefreshToken"] for call in sdk.call_args_list] == [
        "synthetic-original-refresh",
        "synthetic-rotated-after-jwks-failure",
    ]
    assert callback.await_count == 2
    fetch.assert_called_once_with(JWKS_URL, timeout=15)
    session.post.assert_not_called()


def test_public_keys_ignore_environment_override(monkeypatch, production_cognito, signing_keys):
    production_cognito.pool_jwk = None
    monkeypatch.setenv(
        "COGNITO_JWKS", json.dumps({"keys": [signing_keys["synthetic-untrusted"][1]]})
    )
    payload = {"keys": [signing_keys["synthetic-old"][1]]}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    fetch = Mock(return_value=response)
    monkeypatch.setattr(auth.urllib.request, "urlopen", fetch)
    assert production_cognito.get_keys() == payload
    fetch.assert_called_once_with(JWKS_URL, timeout=15)
    response.__enter__.return_value.read.assert_called_once_with(256 * 1024)


def test_cached_key_rotation_refetches_public_keys_once(
    monkeypatch, production_cognito, signing_keys
):
    payload = {"keys": [signing_keys["synthetic-new"][1]]}
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    fetch = Mock(return_value=response)
    monkeypatch.setattr(auth.urllib.request, "urlopen", fetch)
    token = signed_token(signing_keys, kid="synthetic-new")
    production_cognito.verify_token(token, "id_token", "id")
    assert production_cognito.id_token == token
    assert production_cognito.get_key("synthetic-new") == payload["keys"][0]
    fetch.assert_called_once_with(JWKS_URL, timeout=15)


def test_unknown_key_fails_after_one_cached_rotation_lookup(
    monkeypatch, production_cognito, signing_keys
):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"keys": [signing_keys["synthetic-old"][1]]}
    ).encode()
    fetch = Mock(return_value=response)
    monkeypatch.setattr(auth.urllib.request, "urlopen", fetch)
    with pytest.raises(TokenVerificationException):
        production_cognito.get_key("synthetic-absent")
    assert fetch.call_count == 1
