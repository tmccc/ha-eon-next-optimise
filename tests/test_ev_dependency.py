"""Live-fleet dependency blocks wrong cars and does not hide optional entities."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eon_next_optimise import diagnostics, ev_sensor
from custom_components.eon_next_optimise.const import (
    CONF_REFRESH_TOKEN,
    CONF_SITE_IDS,
    DEFAULT_INTERVALS,
    DOMAIN,
    MAX_AGES,
    MIN_INTERVALS,
)
from custom_components.eon_next_optimise.exceptions import CannotConnect

VEHICLE = {"entity_id": "PRIVATE_CAR", "make": "Tesla", "model": "Model 3", "state_of_charge": 61}


async def setup(hass, mock_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: "synthetic@example.test",
            CONF_REFRESH_TOKEN: mock_client.refresh_token,
            CONF_SITE_IDS: ["site_alpha"],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def fill_modules(section_data):
    now = dt_util.utcnow()
    meta = {"query_success": True, "reported": True, "fetched_at": now.isoformat()}
    section_data["ev"].update(vehicles=[dict(VEHICLE)], vehicle_count=1)
    section_data["ev_plan"] = {
        "vehicles": [
            {
                **VEHICLE,
                "timeline": {**meta, "intervals": []},
                "ready_by": {
                    **meta,
                    "configuration_reported": True,
                    "target_soc": 80,
                    "target_time": (now + timedelta(hours=2)).isoformat(),
                    "active": False,
                    "recurring": True,
                },
            }
        ],
        "vehicle_count": 1,
    }
    section_data["ev_history"] = {
        "vehicles": [
            {
                **VEHICLE,
                "history": {**meta, "days": [{"date": "2026-10-29", "home_charge_kwh": 1.25}]},
            }
        ],
        "vehicle_count": 1,
    }


async def test_initial_live_ev_refresh_precedes_dependent_reads(hass, mock_client, section_data):
    fill_modules(section_data)
    events = []

    async def live(section, site_id):
        assert site_id == "site_alpha"
        if section == "ev":
            events.append("live_ready")
        return deepcopy(section_data[section])

    async def dependent(section, site_id, vehicles):
        assert events and events[0] == "live_ready"
        assert vehicles == section_data["ev"]["vehicles"]
        events.append(section)
        return deepcopy(section_data[section])

    mock_client.async_fetch_section = AsyncMock(side_effect=live)
    mock_client.async_fetch_ev_fleet_section = AsyncMock(side_effect=dependent)
    entry = await setup(hass, mock_client)
    bundle = entry.runtime_data.sites["site_alpha"]
    assert events[0] == "live_ready"
    assert set(events[1:]) == {"ev_plan", "ev_history"}
    assert bundle.ev_plan.fleet_coordinator is bundle.ev
    assert bundle.ev_history.fleet_coordinator is bundle.ev
    assert bundle.ev_plan.update_interval == timedelta(seconds=300)
    assert bundle.ev_history.update_interval == timedelta(seconds=900)
    assert DEFAULT_INTERVALS["ev_plan"] == 300 and MIN_INTERVALS["ev_plan"] == 60
    assert DEFAULT_INTERVALS["ev_history"] == 900 and MIN_INTERVALS["ev_history"] == 300
    assert bundle.ev_plan.max_age == MAX_AGES["ev_plan"] == timedelta(minutes=15)
    assert bundle.ev_history.max_age == MAX_AGES["ev_history"] == timedelta(minutes=45)


@pytest.mark.parametrize("fleet", [None, {"vehicles": [], "vehicle_count": None}])
async def test_failed_or_unknown_initial_fleet_never_calls_optional_client(
    hass, mock_client, section_data, fleet
):
    async def fetch(section, _site):
        if section == "ev":
            if fleet is None:
                raise CannotConnect("Synthetic unavailable fleet")
            return fleet
        return deepcopy(section_data[section])

    mock_client.async_fetch_section = AsyncMock(side_effect=fetch)
    entry = await setup(hass, mock_client)
    mock_client.async_fetch_ev_fleet_section.assert_not_awaited()
    bundle = entry.runtime_data.sites["site_alpha"]
    assert bundle.prices.last_update_success
    assert not bundle.ev_plan.last_update_success
    assert not bundle.ev_history.last_update_success
    assert bundle.ev_plan.last_error_class == bundle.ev_history.last_error_class == "dependency"
    result = diagnostics._section_diagnostics(bundle.ev_plan, "ev_plan", dt_util.utcnow())
    assert result["error_category"] == "dependency"
    assert result["dependency_current"] is False
    assert result["all_fields_current"] is False
    assert result["forecast_periods_reported"] is None


async def test_stale_fleet_blocks_later_refresh_without_requesting_old_vehicle(
    hass, mock_client, section_data
):
    fill_modules(section_data)
    entry = await setup(hass, mock_client)
    bundle = entry.runtime_data.sites["site_alpha"]
    mock_client.async_fetch_ev_fleet_section.reset_mock()
    bundle.ev.last_success -= bundle.ev.max_age
    await bundle.ev_plan.async_refresh()
    await bundle.ev_history.async_refresh()
    mock_client.async_fetch_ev_fleet_section.assert_not_awaited()
    assert bundle.ev_plan.last_error_class == bundle.ev_history.last_error_class == "dependency"


async def test_known_live_vehicle_registers_all_optional_entities_when_queries_fail(
    hass, mock_client, section_data
):
    section_data["ev"].update(vehicles=[dict(VEHICLE)], vehicle_count=1)
    mock_client.async_fetch_ev_fleet_section.side_effect = CannotConnect(
        "Synthetic optional unavailable"
    )
    entry = await setup(hass, mock_client)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 72
    assert sum(entity.domain == "sensor" for entity in entities) == 60
    assert sum(entity.domain == "binary_sensor" for entity in entities) == 12
    optional = [
        entity
        for entity in entities
        if ":ev:" in entity.unique_id
        and entity.unique_id.rsplit(":", 1)[1].startswith(("plan_", "history_"))
    ]
    assert len(optional) == 14
    assert all(hass.states.get(entity.entity_id).state == "unavailable" for entity in optional)
    live_soc = next(entity for entity in entities if entity.unique_id.endswith(":soc"))
    assert hass.states.get(live_soc.entity_id).state == "61"


async def test_removed_live_vehicle_invalidates_optional_states_immediately(
    hass, mock_client, section_data
):
    fill_modules(section_data)
    entry = await setup(hass, mock_client)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    target = next(entity for entity in entities if entity.unique_id.endswith(":plan_target_soc"))
    history = next(
        entity for entity in entities if entity.unique_id.endswith(":history_home_charge")
    )
    assert hass.states.get(target.entity_id).state == "80"
    assert hass.states.get(history.entity_id).state == "1.25"
    section_data["ev"].update(vehicles=[], vehicle_count=0)
    await entry.runtime_data.sites["site_alpha"].ev.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(target.entity_id).state == "unavailable"
    assert hass.states.get(history.entity_id).state == "unavailable"
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 72
    fleet = entry.runtime_data.sites["site_alpha"].ev
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not fleet._listeners


async def test_late_live_update_during_sdk_shutdown_cannot_recreate_vehicle_entities(
    hass, mock_client, section_data
):
    entry = await setup(hass, mock_client)
    fleet = entry.runtime_data.sites["site_alpha"].ev
    registry = er.async_get(hass)
    before = {
        entity.entity_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert len(before) == 53  # Known empty fleet before shutdown.
    shutdown_started = asyncio.Event()
    release_shutdown = asyncio.Event()

    async def drain_sdk():
        shutdown_started.set()
        await release_shutdown.wait()

    mock_client.async_shutdown.side_effect = drain_sdk
    unloading = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    try:
        await shutdown_started.wait()
        assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
        with patch.object(
            ev_sensor, "OptimiseEVSensor", wraps=ev_sensor.OptimiseEVSensor
        ) as create:
            # Simulate an in-flight read completing after platform removal but
            # before async_shutdown drains and entry unload callbacks are run.
            fleet.async_set_updated_data({"vehicles": [dict(VEHICLE)], "vehicle_count": 1})
            create.assert_not_called()
    finally:
        release_shutdown.set()
        assert await unloading
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not fleet._listeners
    after = {
        entity.entity_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before
    # HA may restore unloaded registry entities as unavailable placeholders.
    assert all(
        (state := hass.states.get(identity)) is None or state.state == "unavailable"
        for identity in after
    )


def test_dynamic_discovery_blocks_unloaded_entry_but_allows_initial_setup():
    callbacks = []
    fleet = SimpleNamespace(
        data={"vehicles": [dict(VEHICLE)], "vehicle_count": 1},
        section="ev",
        last_update_success=True,
        last_success=dt_util.utcnow(),
        max_age=timedelta(minutes=3),
        async_add_listener=lambda callback: callbacks.append(callback) or Mock(),
    )
    entry = SimpleNamespace(state=ConfigEntryState.NOT_LOADED, async_on_unload=Mock())
    factory = Mock(return_value=[object()])
    add = Mock()
    ev_sensor.setup_dynamic_vehicles(entry, fleet, factory, add)
    factory.assert_not_called()
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    callbacks[0]()
    assert factory.call_count == add.call_count == 1
    fleet.data = {"vehicles": [{**VEHICLE, "entity_id": "PRIVATE_SECOND"}], "vehicle_count": 1}
    entry.state = ConfigEntryState.UNLOAD_IN_PROGRESS
    callbacks[0]()
    assert factory.call_count == 1
    entry.state = ConfigEntryState.LOADED
    callbacks[0]()
    assert factory.call_count == add.call_count == 2
