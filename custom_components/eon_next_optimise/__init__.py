"""Read-only Optimise telemetry; no account or energy-control services."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OptimiseClient
from .const import CONF_REFRESH_TOKEN, CONF_SITE_IDS, SECTIONS
from .coordinator import OptimiseRuntimeData, SectionCoordinator, SiteBundle
from .exceptions import AuthenticationError, CannotConnect

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Validate accessible sites and start each telemetry stream independently."""
    last_saved_refresh = entry.data.get(CONF_REFRESH_TOKEN)

    async def save_token(refresh_token: str) -> None:
        nonlocal last_saved_refresh
        if entry.data.get(CONF_REFRESH_TOKEN) not in (last_saved_refresh, refresh_token):
            # A completed reauth owns the new session; this client is being retired.
            return
        if refresh_token != entry.data.get(CONF_REFRESH_TOKEN):
            data = {key: value for key, value in entry.data.items() if key != CONF_PASSWORD}
            hass.config_entries.async_update_entry(
                entry, data={**data, CONF_REFRESH_TOKEN: refresh_token}
            )
        last_saved_refresh = refresh_token

    client = OptimiseClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data.get(CONF_REFRESH_TOKEN),
        executor=hass.async_add_executor_job,
        token_callback=save_token,
    )
    try:
        accessible = await client.async_discover_sites()
    except AuthenticationError:
        raise ConfigEntryAuthFailed("Optimise sign-in needs attention") from None
    except CannotConnect:
        raise ConfigEntryNotReady("Optimise sites temporarily unavailable") from None
    allowed = {site["site_id"] for site in accessible}
    selected = entry.data.get(CONF_SITE_IDS, [])
    if not isinstance(selected, list) or any(not isinstance(site, str) for site in selected):
        raise ConfigEntryError("Reconfigure the selected Optimise sites")
    if not selected:
        selected = [site["site_id"] for site in accessible if site["selected"]]
        if not selected and len(accessible) == 1:
            selected = [accessible[0]["site_id"]]
        if selected:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_SITE_IDS: selected}
            )
    if not selected or not set(selected) <= allowed or len(set(selected)) != len(selected):
        raise ConfigEntryError("Reconfigure the accessible Optimise sites")
    sites = {}
    for site_id in selected:
        streams = {
            section: SectionCoordinator(hass, entry, client, site_id, section)
            for section in SECTIONS
            if section not in {"ev_plan", "ev_history"}
        }
        streams.update(
            {
                section: SectionCoordinator(
                    hass, entry, client, site_id, section, fleet_coordinator=streams["ev"]
                )
                for section in ("ev_plan", "ev_history")
            }
        )
        sites[site_id] = SiteBundle(**streams)
    entry.runtime_data = OptimiseRuntimeData(client, sites)
    semaphore = asyncio.Semaphore(2)

    fleet_ready = {site_id: asyncio.Event() for site_id in sites}

    async def first_refresh(coordinator: SectionCoordinator) -> None:
        if coordinator.section in {"ev_plan", "ev_history"}:
            await fleet_ready[coordinator.site_id].wait()
        try:
            async with semaphore:
                await coordinator.async_refresh()
        finally:
            if coordinator.section == "ev":
                fleet_ready[coordinator.site_id].set()

    coordinators = [getattr(bundle, section) for bundle in sites.values() for section in SECTIONS]
    await asyncio.gather(*(first_refresh(coordinator) for coordinator in coordinators))
    if any(
        isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
        for coordinator in coordinators
    ):
        raise ConfigEntryAuthFailed("Optimise sign-in needs attention")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload entities and drain refresh/persistence work before completing."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.client.async_shutdown()
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the original price-only integration's stored password."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        data = {key: value for key, value in entry.data.items() if key != CONF_PASSWORD}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True
