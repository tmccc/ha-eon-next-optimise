"""Synthetic Home Assistant fixtures: no provider or production HA requests."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def prices():
    now = dt_util.utcnow()
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    return {
        direction: [
            {
                "valid_from": (start + timedelta(minutes=30 * i)).isoformat(),
                "valid_to": (start + timedelta(minutes=30 * (i + 1))).isoformat(),
                "value": value + i,
                "raw_value": value + i if direction == "import" else -value - i,
                "quality": "current" if i == 0 else "forecast",
                "estimate": i != 0,
                "range_min": None,
                "range_max": None,
                "interval_basis": "provisional_30m",
            }
            for i in range(3)
        ]
        for direction, value in (("import", 12.5), ("export", 7.0))
    }


@pytest.fixture
def section_data(prices):
    return {
        "prices": prices,
        "ev": {"status": None, "vehicles": [], "vehicle_count": 0, "site": {}},
        "ev_plan": {"vehicles": [], "vehicle_count": 0},
        "ev_history": {"vehicles": [], "vehicle_count": 0},
        "live": {
            "sitePowerW": -200,
            "solarPowerW": 800,
            "fixedLoadPowerW": 500,
            "batteryPowerW": -100,
            "batteryEnergyWh": 5000,
            "batteryMaxEnergyWh": 10000,
            "stateOfChargePercentage": 50,
            "powerState": "PRESERVE",
            "powerStateDescription": "Synthetic telemetry",
            "powerStateAction": "PRESERVE",
            "currentGeneralUsagePrice": 12.5,
            "currentFeedInPrice": -7,
            "currentRenewablePercentage": 55,
            "isSolarCurtailmentActive": False,
            "gridImportPowerW": 0,
            "gridExportPowerW": 200,
            "batteryChargePowerW": 100,
            "batteryDischargePowerW": 0,
            "metrics_interval_start": prices["import"][0]["valid_from"],
            "metrics_interval_end": prices["import"][0]["valid_to"],
            "metrics_error_type": None,
            "importCostsCents": 125,
            "exportEarningsCents": 35,
            "totalEarningsCents": -90,
            "siteImportKWh": 2.5,
            "siteExportKWh": 1.25,
            "siteNetKWh": 1.25,
            "batteryImportKWh": 1,
            "batteryExportKWh": 0,
            "batteryNetKWh": 1,
            "solarExportKWh": 3,
            "solarImportKWh": 0,
            "fixedLoadNetKWh": 3.25,
        },
        "plan": {
            "forecast": [
                {
                    "valid_from": prices["import"][0]["valid_from"],
                    "valid_to": prices["import"][0]["valid_to"],
                    "general_price": 12.5,
                    "feed_in_price": 7,
                    "battery_energy_wh": 5000,
                    "solar_power_w": 800,
                    "load_power_w": 500,
                    "actions": ["Synthetic preserve plan"],
                    "decisions": [],
                }
            ],
            "error_type": None,
        },
        "status": {
            "battery_enrolled": True,
            "solar_enrolled": True,
            "ev_enrolled": False,
            "ev_devices_enrolled": False,
            "strategy_status": "ACTIVE",
            "global_killswitch": False,
            "curtailment_enabled": False,
            "curtailment_supported": True,
            "minimum_reserve": [20],
            "has_error": False,
            "devices": [
                {
                    "type": "BATTERY",
                    "brand": "Synthetic",
                    "model": "Test fixture",
                    "status": "Connected",
                    "has_error": False,
                    "onboarding": False,
                }
            ],
            "device_count": 1,
            "error_count": 0,
        },
    }


@pytest.fixture
def mock_client(section_data):
    client = SimpleNamespace(
        refresh_token="synthetic-refresh-v1",
        account_subject="synthetic-subject",
        token_callback=None,
    )
    client.async_sign_in = AsyncMock()
    client.async_complete_mfa = AsyncMock()
    client.async_shutdown = AsyncMock()
    client.async_discover_sites = AsyncMock(
        return_value=[{"site_id": "site_alpha", "selected": True, "status": "ACTIVE"}]
    )

    async def fetch_section(section, site_id):
        return deepcopy(section_data[section])

    client.async_fetch_section = AsyncMock(side_effect=fetch_section)

    async def fetch_fleet_section(section, site_id, vehicles):
        return deepcopy(section_data[section])

    client.async_fetch_ev_fleet_section = AsyncMock(side_effect=fetch_fleet_section)

    def create(*args, **kwargs):
        if kwargs.get("token_callback") is not None:
            client.token_callback = kwargs["token_callback"]
        return client

    factory = MagicMock(side_effect=create)
    with (
        patch("custom_components.eon_next_optimise.config_flow.OptimiseClient", factory),
        patch("custom_components.eon_next_optimise.OptimiseClient", factory),
    ):
        yield client
