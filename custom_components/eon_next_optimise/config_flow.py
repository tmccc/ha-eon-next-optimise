"""Set up read-only account/site telemetry without persisting passwords."""

import hashlib

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OptimiseClient
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_SITE_IDS,
    DEFAULT_INTERVALS,
    DOMAIN,
    MIN_INTERVALS,
    NAME,
    SECTIONS,
)
from .coordinator import MAX_INTERVAL
from .exceptions import AuthenticationError, CannotConnect, ChallengeRequired


def account_id(username: str) -> str:
    """A stable account identity that does not expose an email in entity IDs."""
    return hashlib.sha256(username.strip().casefold().encode()).hexdigest()


def login_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class OptimiseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._sites = []
        self._pending_data = {}
        self._updating_entry = None
        self._mfa_client = None
        self._mfa_username = None
        self._mfa_verified = False

    def _client(
        self, username: str, refresh_token: str | None = None, *, token_callback=None
    ) -> OptimiseClient:
        return OptimiseClient(
            async_get_clientsession(self.hass),
            username,
            refresh_token,
            executor=self.hass.async_add_executor_job,
            token_callback=token_callback,
        )

    async def _validate_login(self, user_input: dict) -> str | None:
        username = user_input[CONF_USERNAME].strip()
        client = self._client(username)
        try:
            await client.async_sign_in(user_input[CONF_PASSWORD])
        except ChallengeRequired as err:
            if err.challenge.casefold() in {"sms_mfa", "totp_mfa"}:
                self._mfa_client = client
                self._mfa_username = username
                self._mfa_verified = False
                return "mfa_required"
            if err.challenge.casefold() == "new_password_required":
                return "new_password_required"
            return "verification_required"
        except AuthenticationError:
            return "invalid_auth"
        except CannotConnect:
            return "cannot_connect"
        return await self._discover(client, username)

    async def _discover(self, client, username):
        try:
            self._sites = await client.async_discover_sites()
        except AuthenticationError:
            return "invalid_auth"
        except CannotConnect:
            return "cannot_connect"
        if not self._sites:
            return "no_sites"
        if not client.refresh_token:
            return "invalid_auth"
        self._pending_data = {CONF_USERNAME: username, CONF_REFRESH_TOKEN: client.refresh_token}
        return None

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(account_id(user_input[CONF_USERNAME]))
            self._abort_if_unique_id_configured()
            error = await self._validate_login(user_input)
            if error == "mfa_required":
                return await self.async_step_mfa()
            if error:
                errors["base"] = error
            else:
                return await self._choose_sites()
        return self.async_show_form(step_id="user", data_schema=login_schema(), errors=errors)

    async def _choose_sites(self):
        if self._updating_entry is not None:
            previous = self._updating_entry.data.get(CONF_SITE_IDS, [])
            allowed = {site["site_id"] for site in self._sites}
            if previous and set(previous) <= allowed:
                return self._finish(previous)
        if len(self._sites) == 1:
            return self._finish([self._sites[0]["site_id"]])
        return await self.async_step_sites()

    async def async_step_mfa(self, user_input=None):
        """Complete a manually entered one-time code without saving it."""
        errors = {}
        if self._mfa_client is None:
            return self.async_abort(reason="reauth_required")
        if user_input is not None:
            code = user_input.get(CONF_CODE)
            if (
                not isinstance(code, str)
                or len(code) != 6
                or not code.isascii()
                or not code.isdecimal()
            ):
                errors["base"] = "invalid_code"
            else:
                try:
                    if not self._mfa_verified:
                        await self._mfa_client.async_complete_mfa(code)
                        self._mfa_verified = True
                except AuthenticationError:
                    errors["base"] = "invalid_code"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    error = await self._discover(self._mfa_client, self._mfa_username)
                    if error:
                        errors["base"] = error
                    else:
                        self._mfa_client = None
                        self._mfa_username = None
                        return await self._choose_sites()
        schema = vol.Schema(
            {
                vol.Required(CONF_CODE): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            }
        )
        return self.async_show_form(step_id="mfa", data_schema=schema, errors=errors)

    def _finish(self, site_ids):
        data = {**self._pending_data, CONF_SITE_IDS: site_ids}
        if self._updating_entry is not None:
            if self.context.get("source") == config_entries.SOURCE_RECONFIGURE:
                # A loaded client may rotate while this site-selection form is open.
                data[CONF_REFRESH_TOKEN] = self._updating_entry.data.get(
                    CONF_REFRESH_TOKEN, data[CONF_REFRESH_TOKEN]
                )
            return self.async_update_reload_and_abort(self._updating_entry, data=data)
        return self.async_create_entry(title=NAME, data=data)

    async def async_step_sites(self, user_input=None):
        errors = {}
        available = {site["site_id"] for site in self._sites}
        if user_input is not None:
            selected = user_input.get(CONF_SITE_IDS)
            if (
                isinstance(selected, list)
                and selected
                and all(isinstance(site, str) for site in selected)
                and len(set(selected)) == len(selected)
                and set(selected) <= available
            ):
                return self._finish(selected)
            errors["base"] = "invalid_sites"
        defaults = (
            self._updating_entry.data.get(CONF_SITE_IDS, [])
            if self._updating_entry
            else [site["site_id"] for site in self._sites if site["selected"]]
        )
        defaults = [site for site in defaults if site in available]
        options = [
            {"value": site["site_id"], "label": f"Site {index + 1} ({site['site_id'][-6:]})"}
            for index, site in enumerate(self._sites)
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_SITE_IDS, default=defaults): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="sites", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data):
        self._updating_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            self._updating_entry = entry
            if account_id(user_input[CONF_USERNAME]) != entry.unique_id:
                return self.async_abort(reason="wrong_account")
            error = await self._validate_login(user_input)
            if error == "mfa_required":
                return await self.async_step_mfa()
            if error:
                errors["base"] = error
            else:
                return await self._choose_sites()
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=login_schema(), errors=errors
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        self._updating_entry = entry
        runtime = (
            getattr(entry, "runtime_data", None) if entry.state is ConfigEntryState.LOADED else None
        )
        last_saved_refresh = entry.data.get(CONF_REFRESH_TOKEN)

        async def save_token(refresh_token: str) -> None:
            nonlocal last_saved_refresh
            if entry.data.get(CONF_REFRESH_TOKEN) not in (last_saved_refresh, refresh_token):
                return
            data = {key: value for key, value in entry.data.items() if key != CONF_PASSWORD}
            self.hass.config_entries.async_update_entry(
                entry, data={**data, CONF_REFRESH_TOKEN: refresh_token}
            )
            last_saved_refresh = refresh_token

        client = (
            runtime.client
            if runtime is not None
            else self._client(
                entry.data[CONF_USERNAME],
                entry.data.get(CONF_REFRESH_TOKEN),
                token_callback=save_token,
            )
        )
        try:
            self._sites = await client.async_discover_sites()
        except AuthenticationError:
            return self.async_abort(reason="reauth_required")
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        if not self._sites:
            return self.async_abort(reason="no_sites")
        self._pending_data = {
            CONF_USERNAME: entry.data[CONF_USERNAME],
            CONF_REFRESH_TOKEN: client.refresh_token,
        }
        return await self.async_step_sites(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptimiseOptionsFlow()


class OptimiseOptionsFlow(config_entries.OptionsFlowWithReload):
    """Local polling choices; no backend energy preferences are changed."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            if all(
                isinstance(user_input.get(f"{section}_interval"), int)
                and not isinstance(user_input[f"{section}_interval"], bool)
                and MIN_INTERVALS[section] <= user_input[f"{section}_interval"] <= MAX_INTERVAL
                for section in SECTIONS
            ):
                return self.async_create_entry(title="", data=user_input)
            errors["base"] = "invalid_interval"
        schema = vol.Schema(
            {
                vol.Required(
                    f"{section}_interval",
                    default=self.config_entry.options.get(
                        f"{section}_interval", DEFAULT_INTERVALS[section]
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_INTERVALS[section], max=MAX_INTERVAL))
                for section in SECTIONS
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
