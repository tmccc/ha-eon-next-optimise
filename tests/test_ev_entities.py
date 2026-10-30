"""EV freshness, dynamic child-device discovery and identifier privacy."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eon_next_optimise import binary_sensor, ev_sensor
from custom_components.eon_next_optimise.const import (
    CONF_REFRESH_TOKEN,
    CONF_SITE_IDS,
    DEFAULT_INTERVALS,
    DOMAIN,
    MAX_AGES,
    MIN_INTERVALS,
)


def vehicle(identity="private-vehicle-one", **extra):
    return {
        "entity_id": identity,
        "state_of_charge": 61,
        "live_state": "Pending next charge",
        "connectivity": "ONLINE",
        "connectivity_label": "Connected",
        "charging_speed_label": "No charge rate reported",
        "ready_by_eligible": False,
        "make": "Tesla",
        "model": "Model 3",
        "vin": "private-vin-never-exposed",
        **extra,
    }


def coordinator(rows):
    return SimpleNamespace(
        entry=SimpleNamespace(entry_id="private-entry-id"),
        site_id="private-site-id",
        section="ev",
        data={"vehicles": rows, "vehicle_count": len(rows)},
        max_age=MAX_AGES["ev"],
        last_success=dt_util.utcnow(),
        last_update_success=True,
        async_add_listener=Mock(return_value=Mock()),
        async_request_refresh=AsyncMock(),
    )


def ev_sensor_for(section, key="soc"):
    return ev_sensor.OptimiseEVSensor(
        section,
        "Optimise site 1",
        section.data["vehicles"][0],
        1,
        next(spec for spec in ev_sensor.EV_SENSORS if spec.key == key),
    )


@pytest.mark.parametrize("soc", [0, 61, 100])
def test_soc_is_measurement_and_zero_preserved(soc):
    entity = ev_sensor_for(coordinator([vehicle(state_of_charge=soc)]))
    assert entity.available and entity.native_value == soc
    assert entity.native_unit_of_measurement == "%"
    assert entity.device_class == SensorDeviceClass.BATTERY
    assert entity.state_class == SensorStateClass.MEASUREMENT


@pytest.mark.parametrize("soc", [None, True, "61", -1, 101, float("nan"), float("inf")])
def test_unknown_or_invalid_soc_never_becomes_zero(soc):
    entity = ev_sensor_for(coordinator([vehicle(state_of_charge=soc)]))
    assert not entity.available
    assert entity.native_value is None


def test_missing_vehicle_stale_or_failed_ev_data_never_reuses_old_soc():
    section = coordinator([vehicle()])
    entity = ev_sensor_for(section)
    assert entity.available
    section.data["vehicles"] = [vehicle("private-other-vehicle", state_of_charge=90)]
    assert not entity.available and entity.native_value is None
    section.data["vehicles"] = [vehicle()]
    section.last_success -= section.max_age
    assert not entity.available and entity.native_value is None
    section.last_success = dt_util.utcnow()
    section.last_update_success = False
    assert not entity.available and entity.native_value is None


def test_state_connectivity_and_charging_speed_preserve_provider_words():
    section = coordinator([vehicle()])
    assert ev_sensor_for(section, "state").native_value == "Pending next charge"
    assert ev_sensor_for(section, "connectivity").native_value == "Connected"
    speed = ev_sensor_for(section, "charging_speed")
    assert speed.native_value == "No charge rate reported"
    assert speed.native_unit_of_measurement is None
    assert speed.state_class is None
    assert not any(spec.key in {"plugged", "charging", "enrolled"} for spec in ev_sensor.EV_SENSORS)


def test_ready_by_eligibility_is_only_explicit_boolean():
    section = coordinator([vehicle(ready_by_eligible=False)])
    entity = binary_sensor.OptimiseEVReadyBySensor(section, "Optimise site 1", vehicle(), 1)
    assert entity.available and entity.is_on is False
    section.data["vehicles"][0]["ready_by_eligible"] = True
    assert entity.available and entity.is_on is True
    section.data["vehicles"][0]["ready_by_eligible"] = None
    assert not entity.available and entity.is_on is None
    section.data["vehicles"] = []
    assert not entity.available and entity.is_on is None


def test_vehicle_registry_identity_is_hashed_and_parented_without_public_ids():
    section = coordinator([vehicle()])
    entity = ev_sensor_for(section)
    assert ":ev:" + ev_sensor.vehicle_digest("private-vehicle-one") + ":soc" in entity.unique_id
    assert "private-vehicle-one" not in str(entity.device_info)
    assert "private-vehicle-one" not in entity.unique_id
    assert "private-vin" not in str(entity.device_info)
    assert entity.device_info["name"] == "Optimise EV 1"
    assert entity.device_info["model"] == "Model 3"
    assert entity.device_info["via_device_id"] is None  # Pure fixture has no HA registry.
    assert entity.extra_state_attributes is None
    assert not any(
        hasattr(entity, method)
        for method in ("async_turn_on", "async_set_native_value", "async_press")
    )


async def setup(hass, mock_client, section_data):
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


async def test_real_platform_setup_creates_53_site_plus_nineteen_vehicle_entities(
    hass, mock_client, section_data
):
    section_data["ev"].update(vehicles=[vehicle()], vehicle_count=1)
    entry = await setup(hass, mock_client, section_data)
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 72
    ev_entries = [item for item in entities if ":ev:" in item.unique_id]
    assert len(ev_entries) == 19
    assert {item.domain for item in ev_entries} == {"sensor", "binary_sensor"}
    for item in ev_entries:
        assert "private-vehicle" not in item.unique_id
        assert "private-vehicle" not in str(hass.states.get(item.entity_id).as_dict())
        assert "private-vin" not in str(hass.states.get(item.entity_id).as_dict())
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 2
    child = next(
        device
        for device in devices
        if any(":ev:" in identity for _, identity in device.identifiers)
    )
    parent = next(device for device in devices if device.id != child.id)
    assert child.via_device_id == parent.id
    assert DEFAULT_INTERVALS["ev"] == MIN_INTERVALS["ev"] == 60
    assert entry.runtime_data.sites["site_alpha"].ev.max_age == timedelta(minutes=3)


async def test_new_vehicle_added_once_missing_vehicle_unavailable_and_unload_cleans_listeners(
    hass, mock_client, section_data
):
    entry = await setup(hass, mock_client, section_data)
    registry = er.async_get(hass)
    section = entry.runtime_data.sites["site_alpha"].ev
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 53
    section_data["ev"].update(vehicles=[vehicle()], vehicle_count=1)
    await section.async_refresh()
    await hass.async_block_till_done()
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 72
    first_soc = next(item.entity_id for item in entities if item.unique_id.endswith(":soc"))
    assert hass.states.get(first_soc).state == "61"
    await section.async_refresh()
    await hass.async_block_till_done()
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 72
    section_data["ev"].update(
        vehicles=[vehicle("private-vehicle-two", state_of_charge=72)], vehicle_count=1
    )
    await section.async_refresh()
    await hass.async_block_till_done()
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 91
    assert hass.states.get(first_soc).state == "unavailable"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_client.async_shutdown.assert_awaited_once()
    assert not section._listeners
