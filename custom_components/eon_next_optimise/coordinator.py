"""Independent read-only telemetry coordinators sharing one authenticated client."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OptimiseClient
from .const import DEFAULT_INTERVALS, MAX_AGES, MAX_INTERVAL, MIN_INTERVALS, NAME
from .exceptions import AuthenticationError, CannotConnect, RateLimited
from .models import DataError

_LOGGER = logging.getLogger(__name__)


class SectionCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch one site section; failures do not overwrite other section data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OptimiseClient,
        site_id: str,
        section: str,
        *,
        fleet_coordinator=None,
    ) -> None:
        interval = entry.options.get(f"{section}_interval", DEFAULT_INTERVALS[section])
        if isinstance(interval, bool) or not isinstance(interval, int):
            interval = DEFAULT_INTERVALS[section]
        interval = max(MIN_INTERVALS[section], min(interval, MAX_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{NAME} {section}",
            update_interval=timedelta(seconds=interval),
            # Retrieval timestamps and freshness timers also change on equal data.
            always_update=True,
        )
        self.entry = entry
        self.client = client
        self.site_id = site_id
        self.section = section
        self.fleet_coordinator = fleet_coordinator
        self.max_age = max(MAX_AGES[section], timedelta(seconds=interval * 3))
        self.last_success: datetime | None = None
        self.last_error_class: str | None = None

    def current_fleet(self):
        """Only a current, explicitly known live fleet may scope optional reads."""
        fleet = self.fleet_coordinator
        if fleet is None or not fleet.last_update_success:
            return None
        success = fleet.last_success
        if (
            not isinstance(success, datetime)
            or success.tzinfo is None
            or success.utcoffset() is None
        ):
            return None
        if not timedelta(0) <= dt_util.utcnow() - success < fleet.max_age:
            return None
        source = fleet.data if isinstance(fleet.data, dict) else {}
        rows, count = source.get("vehicles"), source.get("vehicle_count")
        if not isinstance(rows, list) or type(count) is not int or count != len(rows):
            return None
        identities = [row.get("entity_id") if isinstance(row, dict) else None for row in rows]
        if any(
            not isinstance(identity, str) or not identity or len(identity) > 256
            for identity in identities
        ) or len(set(identities)) != len(identities):
            return None
        return rows

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if self.section in {"ev_plan", "ev_history"}:
                fleet = self.current_fleet()
                if fleet is None:
                    self.last_error_class = "dependency"
                    raise UpdateFailed("Current EV live telemetry is required")
                data = await self.client.async_fetch_ev_fleet_section(
                    self.section, self.site_id, fleet
                )
            else:
                data = await self.client.async_fetch_section(self.section, self.site_id)
        except AuthenticationError:
            self.last_error_class = "authentication"
            raise ConfigEntryAuthFailed("Optimise sign-in needs attention") from None
        except RateLimited as err:
            self.last_error_class = "rate_limited"
            raise UpdateFailed(
                "Optimise request limit reached", retry_after=err.retry_after
            ) from None
        except CannotConnect:
            self.last_error_class = "connection"
            raise UpdateFailed("Optimise telemetry temporarily unavailable") from None
        except DataError:
            self.last_error_class = "invalid_data"
            raise UpdateFailed("Optimise telemetry format is unavailable") from None
        self.last_success = dt_util.utcnow()
        self.last_error_class = None
        return data


@dataclass(slots=True)
class SiteBundle:
    """Independent update streams for one selected site."""

    prices: SectionCoordinator
    live: SectionCoordinator
    plan: SectionCoordinator
    status: SectionCoordinator
    ev: SectionCoordinator
    ev_plan: SectionCoordinator
    ev_history: SectionCoordinator


@dataclass(slots=True)
class OptimiseRuntimeData:
    """Runtime clients are separate from the private config-entry data."""

    client: OptimiseClient
    sites: dict[str, SiteBundle]
