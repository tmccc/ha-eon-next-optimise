"""A first-install HACS metadata cache must allow the reviewed local overlay."""

import json
from pathlib import Path

import pytest
from homeassistant import loader
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er

from custom_components.eon_next_optimise.const import DOMAIN, NAME

UPSTREAM_TOP_LEVEL_FILES = {
    "__init__.py",
    "api.py",
    "brand",
    "config_flow.py",
    "const.py",
    "coordinator.py",
    "diagnostics.py",
    "manifest.json",
    "rates.py",
    "sensor.py",
    "strings.json",
    "translations",
}


@pytest.mark.parametrize("seed_binary_filename", [False, True])
async def test_hacs_beta_metadata_then_fresh_overlay_flow_setup_and_unload(
    hass, mock_client, seed_binary_filename
):
    """Do not hot-reload code: only the beta's discovery metadata existed first.

    HACS2.0.5's first-install post-hook scans manifests without importing the new
    component. The downloaded beta is replaced before opening its config flow.
    This reproduces that cached Integration object using the actual HA loader;
    all flow/platform code that subsequently executes is the reviewed overlay.
    """
    directory = Path(__file__).parents[1] / "custom_components" / DOMAIN
    installed_manifest = json.loads((directory / "manifest.json").read_text())
    legacy_manifest = {
        **installed_manifest,
        "name": "E.ON Next Optimise Prices",
        "version": "0.1.0b1",
        "requirements": ["pycognito==2024.5.1"],
    }
    # These fields affect initial import/dependency processing. A future
    # dependency change must not silently reuse this deployment technique.
    for key in ("domain", "requirements", "config_flow", "dependencies", "after_dependencies"):
        assert installed_manifest.get(key) == legacy_manifest.get(key)

    top_files = UPSTREAM_TOP_LEVEL_FILES.copy()
    if seed_binary_filename:
        # A harmless placeholder is staged before the FIRST native HACS
        # extraction, which retains unlisted files when installed=False.
        top_files.add("binary_sensor.py")
    cached = loader.Integration(
        hass,
        f"custom_components.{DOMAIN}",
        directory,
        legacy_manifest,
        top_level_files=top_files,
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {DOMAIN: cached}
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = cached
    assert DOMAIN not in hass.data[loader.DATA_COMPONENTS]
    assert f"{DOMAIN}.config_flow" not in hass.data[loader.DATA_COMPONENTS]
    assert ("binary_sensor.py" in cached._top_level_files) is seed_binary_filename
    assert "binary_sensor" not in loader.BASE_PRELOAD_PLATFORMS
    assert "sensor" not in loader.BASE_PRELOAD_PLATFORMS

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "synthetic@example.test", CONF_PASSWORD: "synthetic-only"},
    )
    assert result["type"] == "create_entry"
    assert result["title"] == NAME
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.version == 2  # New MFA/site-selection flow, not the old beta.
    assert CONF_PASSWORD not in entry.data

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == (53 if seed_binary_filename else 44)
    assert {entity.domain for entity in entities} == (
        {"sensor", "binary_sensor"} if seed_binary_filename else {"sensor"}
    )
    assert len([entity for entity in entities if entity.domain == "sensor"]) == 44
    assert len([entity for entity in entities if entity.domain == "binary_sensor"]) == (
        9 if seed_binary_filename else 0
    )
    assert len({entity.device_id for entity in entities}) == 1
    import_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}:site_alpha:import_price"
    )
    assert hass.states.get(import_id).state == "12.5"
    mock_client.async_fetch_section.assert_awaited()

    # The advisory metadata remains upstream until an ordinary later HA
    # restart. It must not be represented as the overlay's published release.
    assert (await loader.async_get_integration(hass, DOMAIN)) is cached
    assert cached.name == "E.ON Next Optimise Prices"
    assert str(cached.version) == "0.1.0b1"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert all(
        hass.states.get(entity.entity_id) is None
        or hass.states.get(entity.entity_id).state == "unavailable"
        for entity in entities
    )
