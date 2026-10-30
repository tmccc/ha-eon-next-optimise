"""HA setup, site selection, reauth and section isolation with fake transports."""

from copy import deepcopy

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.eon_next_optimise import async_migrate_entry
from custom_components.eon_next_optimise.config_flow import account_id
from custom_components.eon_next_optimise.const import (
    CONF_REFRESH_TOKEN,
    CONF_SITE_IDS,
    DEFAULT_INTERVALS,
    DOMAIN,
    NAME,
    SECTIONS,
)
from custom_components.eon_next_optimise.exceptions import (
    AuthenticationError,
    CannotConnect,
    ChallengeRequired,
    RateLimited,
)

LOGIN = {CONF_USERNAME: "synthetic@example.test", CONF_PASSWORD: "synthetic-test-only"}


async def setup(hass, mock_client, site_ids=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=account_id(LOGIN[CONF_USERNAME]),
        version=2,
        data={
            CONF_USERNAME: LOGIN[CONF_USERNAME],
            CONF_REFRESH_TOKEN: mock_client.refresh_token,
            CONF_SITE_IDS: site_ids or ["site_alpha"],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_user_flow_never_persists_password(hass, mock_client):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_USERNAME: LOGIN[CONF_USERNAME],
        CONF_REFRESH_TOKEN: mock_client.refresh_token,
        CONF_SITE_IDS: ["site_alpha"],
    }
    assert CONF_PASSWORD not in result["data"]
    mock_client.async_sign_in.assert_awaited_once_with(LOGIN[CONF_PASSWORD])
    await hass.async_block_till_done()


async def test_multiple_sites_require_explicit_choice(hass, mock_client):
    mock_client.async_discover_sites.return_value.append(
        {"site_id": "site_beta", "selected": False, "status": "ACTIVE"}
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["step_id"] == "sites"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_IDS: ["site_alpha", "site_beta"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SITE_IDS] == ["site_alpha", "site_beta"]
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "error,key",
    [
        (AuthenticationError("private-secret"), "invalid_auth"),
        (CannotConnect("private-secret"), "cannot_connect"),
        (ChallengeRequired("mfa_required"), "verification_required"),
        (ChallengeRequired("NEW_PASSWORD_REQUIRED"), "new_password_required"),
    ],
)
async def test_flow_errors_are_generic(hass, mock_client, error, key):
    mock_client.async_sign_in.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["errors"] == {"base": key}
    assert "private-secret" not in str(result)


async def test_no_sites_keeps_setup_form(hass, mock_client):
    mock_client.async_discover_sites.return_value = []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["errors"] == {"base": "no_sites"}


async def test_duplicate_account_does_not_sign_in(hass, mock_client):
    await setup(hass, mock_client)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["reason"] == "already_configured"
    mock_client.async_sign_in.assert_not_awaited()


async def test_setup_uses_seven_shared_client_sections(hass, mock_client):
    entry = await setup(hass, mock_client)
    assert set(entry.runtime_data.sites) == {"site_alpha"}
    bundle = entry.runtime_data.sites["site_alpha"]
    assert {call.args for call in mock_client.async_fetch_section.await_args_list} == {
        (section, "site_alpha") for section in SECTIONS if section not in {"ev_plan", "ev_history"}
    }
    assert {call.args[:2] for call in mock_client.async_fetch_ev_fleet_section.await_args_list} == {
        (section, "site_alpha") for section in ("ev_plan", "ev_history")
    }
    for section in SECTIONS:
        coordinator = getattr(bundle, section)
        assert coordinator.client is mock_client
        assert coordinator.section == section
        assert coordinator.last_success.tzinfo is not None
        assert coordinator.last_update_success
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert entities
    assert {entity.domain for entity in entities} <= {"sensor", "binary_sensor"}


async def test_partial_first_refresh_failure_does_not_suppress_prices(
    hass, mock_client, section_data
):
    async def fetch(section, site_id):
        if section == "plan":
            raise CannotConnect("private-provider-error")
        return deepcopy(section_data[section])

    mock_client.async_fetch_section.side_effect = fetch
    entry = await setup(hass, mock_client)
    bundle = entry.runtime_data.sites["site_alpha"]
    assert bundle.prices.last_update_success
    assert not bundle.plan.last_update_success
    assert bundle.plan.last_success is None
    assert bundle.live.last_update_success


async def test_token_callback_persists_only_refresh_token(hass, mock_client):
    entry = await setup(hass, mock_client)
    await mock_client.token_callback("synthetic-rotated-refresh")
    assert entry.data[CONF_REFRESH_TOKEN] == "synthetic-rotated-refresh"
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_SITE_IDS] == ["site_alpha"]


async def test_retiring_runtime_cannot_overwrite_a_new_sign_in_session(hass, mock_client):
    entry = await setup(hass, mock_client)
    save_token = mock_client.token_callback
    await save_token("synthetic-first-rotation")
    await save_token("synthetic-second-rotation")
    assert entry.data[CONF_REFRESH_TOKEN] == "synthetic-second-rotation"

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_REFRESH_TOKEN: "synthetic-new-sign-in-session"}
    )
    await save_token("synthetic-late-old-session-rotation")
    assert entry.data[CONF_REFRESH_TOKEN] == "synthetic-new-sign-in-session"
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_SITE_IDS] == ["site_alpha"]


async def test_equal_telemetry_still_notifies_freshness_listeners(hass, mock_client):
    entry = await setup(hass, mock_client)
    coordinator = entry.runtime_data.sites["site_alpha"].live
    successes = []
    unsubscribe = coordinator.async_add_listener(lambda: successes.append(coordinator.last_success))
    await coordinator.async_refresh()
    assert successes == [coordinator.last_success]
    unsubscribe()


async def test_reauth_preserves_account_sites_and_entity_ids(hass, mock_client):
    entry = await setup(hass, mock_client)
    before = {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.unique_id == account_id(LOGIN[CONF_USERNAME])
    assert entry.data[CONF_SITE_IDS] == ["site_alpha"]
    assert CONF_PASSWORD not in entry.data
    assert {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    } == before


async def test_reauth_cannot_change_accounts(hass, mock_client):
    entry = await setup(hass, mock_client)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**LOGIN, CONF_USERNAME: "another@example.test"}
    )
    assert result["reason"] == "wrong_account"
    mock_client.async_sign_in.assert_not_awaited()


async def test_runtime_auth_failure_starts_reauth(hass, mock_client):
    entry = await setup(hass, mock_client)
    mock_client.async_fetch_section.side_effect = AuthenticationError("private-secret")
    await entry.runtime_data.sites["site_alpha"].live.async_refresh()
    await hass.async_block_till_done()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_rate_limit_keeps_other_sections_available(hass, mock_client):
    entry = await setup(hass, mock_client)
    mock_client.async_fetch_section.side_effect = RateLimited(120)
    await entry.runtime_data.sites["site_alpha"].status.async_refresh()
    assert not entry.runtime_data.sites["site_alpha"].status.last_update_success
    assert entry.runtime_data.sites["site_alpha"].prices.last_update_success


async def test_options_change_only_local_polling(hass, mock_client):
    entry = await setup(hass, mock_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    options = {f"{section}_interval": seconds * 2 for section, seconds in DEFAULT_INTERVALS.items()}
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert dict(entry.options) == options
    assert entry.data[CONF_SITE_IDS] == ["site_alpha"]
    mock_client.async_sign_in.assert_not_awaited()


async def test_reconfigure_site_selection_is_local(hass, mock_client):
    entry = await setup(hass, mock_client)
    mock_client.async_discover_sites.return_value.append(
        {"site_id": "site_beta", "selected": False, "status": "ACTIVE"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "sites"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_IDS: ["site_beta"]}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_SITE_IDS] == ["site_beta"]
    assert set(entry.runtime_data.sites) == {"site_beta"}


async def test_migration_removes_legacy_password(hass, mock_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        version=1,
        data={**LOGIN, CONF_REFRESH_TOKEN: mock_client.refresh_token},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_REFRESH_TOKEN] == mock_client.refresh_token


async def test_unload_stops_polling(hass, mock_client):
    entry = await setup(hass, mock_client)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    count = mock_client.async_fetch_section.await_count
    from datetime import timedelta

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await hass.async_block_till_done()
    assert mock_client.async_fetch_section.await_count == count


@pytest.mark.parametrize("challenge", ["sms_mfa", "totp_mfa"])
async def test_manual_mfa_creates_entry_without_code_or_password(hass, mock_client, challenge):
    mock_client.async_sign_in.side_effect = ChallengeRequired(challenge)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["step_id"] == "mfa"
    assert LOGIN[CONF_PASSWORD] not in str(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] == "create_entry"
    assert CONF_PASSWORD not in result["data"]
    assert "code" not in result["data"]
    assert "123456" not in str(result["data"])
    mock_client.async_complete_mfa.assert_awaited_once_with("123456")
    await hass.async_block_till_done()


async def test_rejected_mfa_can_retry_without_a_new_password(hass, mock_client):
    mock_client.async_sign_in.side_effect = ChallengeRequired("totp_mfa")
    mock_client.async_complete_mfa.side_effect = [AuthenticationError("private-secret"), None]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_code"}
    assert "private-secret" not in str(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "654321"})
    assert result["type"] == "create_entry"
    assert mock_client.async_sign_in.await_count == 1
    assert mock_client.async_complete_mfa.await_count == 2
    await hass.async_block_till_done()


async def test_reauth_mfa_preserves_selected_site(hass, mock_client):
    entry = await setup(hass, mock_client)
    mock_client.async_sign_in.side_effect = ChallengeRequired("sms_mfa")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["step_id"] == "mfa"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_SITE_IDS] == ["site_alpha"]
    assert CONF_PASSWORD not in entry.data and "code" not in entry.data


async def test_unload_awaits_refresh_persistence_drain(hass, mock_client):
    import asyncio

    entry = await setup(hass, mock_client)
    draining = asyncio.Event()
    release = asyncio.Event()

    async def shutdown():
        draining.set()
        await release.wait()
        await mock_client.token_callback("synthetic-rotated-on-shutdown")

    mock_client.async_shutdown.side_effect = shutdown
    task = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await draining.wait()
    assert not task.done()
    release.set()
    assert await task
    assert entry.data[CONF_REFRESH_TOKEN] == "synthetic-rotated-on-shutdown"
    mock_client.async_shutdown.assert_awaited_once()


async def test_reconfigure_fallback_persists_rotation_even_when_discovery_fails(hass, mock_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=account_id(LOGIN[CONF_USERNAME]),
        version=2,
        data={**LOGIN, CONF_REFRESH_TOKEN: "synthetic-old-refresh", CONF_SITE_IDS: ["site_alpha"]},
    )
    entry.add_to_hass(hass)

    async def discover():
        await mock_client.token_callback("synthetic-rotated-before-failure")
        raise CannotConnect("synthetic-private-provider-detail")

    mock_client.async_discover_sites.side_effect = discover
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["reason"] == "cannot_connect"
    assert "data" not in result
    assert "synthetic-private" not in str(result)
    assert entry.data[CONF_REFRESH_TOKEN] == "synthetic-rotated-before-failure"
    assert entry.data[CONF_SITE_IDS] == ["site_alpha"]
    assert CONF_PASSWORD not in entry.data
    mock_client.async_fetch_section.assert_not_awaited()


async def test_reconfigure_preserves_rotation_while_site_form_is_open(hass, mock_client):
    entry = await setup(hass, mock_client)
    mock_client.async_discover_sites.return_value.append(
        {"site_id": "site_beta", "selected": False, "status": "ACTIVE"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "sites"
    await mock_client.token_callback("synthetic-rotated-while-form-open")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_IDS: ["site_beta"]}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "synthetic-rotated-while-form-open"
    await hass.async_block_till_done()
