"""Count-only diagnostics distinguish partial status retrieval from full freshness."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.eon_next_optimise import api, diagnostics
from custom_components.eon_next_optimise.exceptions import STATUS_ROOTS, ProviderErrorInfo


def coordinator(info=None):
    client = api.OptimiseClient(object(), "PRIVATE_EMAIL", "PRIVATE_REFRESH")
    if info is not None:
        client._record_error("status", "PRIVATE_SITE", info)
    now = dt_util.utcnow()
    return SimpleNamespace(
        client=client,
        site_id="PRIVATE_SITE",
        section="status",
        last_success=now,
        last_update_success=True,
        last_error_class=None,
        last_exception=None,
        max_age=timedelta(minutes=45),
        data={
            "battery_enrolled": True,
            "strategy_status": None,
            "devices": [{"id": "PRIVATE_DEVICE", "model": "PRIVATE_MODEL"}],
            "minimum_reserve": [],
            "error_count": 0,
        },
    ), now


def test_partial_success_is_fresh_but_not_all_fields_current_and_keeps_only_counts():
    section, now = coordinator(
        ProviderErrorInfo(
            "partial",
            http_status=200,
            graphql_codes=("INTERNAL_SERVER_ERROR",),
            failed_roots=(
                "smartshiftBatteryStrategyConfig",
                "smartshiftUserSettings",
                "PRIVATE_ID",
            ),
        )
    )
    result = diagnostics._section_diagnostics(section, "status", now)
    assert result["last_update_success"] is True
    assert result["fresh"] is True
    assert result["all_fields_current"] is False
    assert result["partial_data"] is True
    assert result["failed_root_count"] == 2
    assert result["devices_reported"] == 1
    assert result["device_error_count"] == 0
    assert result["provider_error"]["failed_roots"] == [
        "smartshiftBatteryStrategyConfig",
        "smartshiftUserSettings",
    ]
    assert "PRIVATE" not in json.dumps(result)
    assert "battery_enrolled" not in result
    assert "strategy_status" not in result


def test_all_roots_success_clear_partial_status_even_with_nullable_optional_values():
    section, now = coordinator()
    result = diagnostics._section_diagnostics(section, "status", now)
    assert result["all_fields_current"] is True
    assert result["partial_data"] is False
    assert result["failed_root_count"] == 0
    assert result["provider_error"] is None
    # Completeness describes successful root retrieval, not invented null values.
    assert section.data["strategy_status"] is None


def test_failed_or_stale_section_never_reports_all_fields_current():
    section, now = coordinator()
    section.last_update_success = False
    assert diagnostics._section_diagnostics(section, "status", now)["all_fields_current"] is False
    section.last_update_success = True
    section.last_success -= section.max_age
    result = diagnostics._section_diagnostics(section, "status", now)
    assert result["fresh"] is False
    assert result["all_fields_current"] is False


def test_all_root_failures_report_four_without_partial_success_claim():
    section, now = coordinator(
        ProviderErrorInfo("graphql", failed_roots=tuple(STATUS_ROOTS.values()))
    )
    section.last_update_success = False
    result = diagnostics._section_diagnostics(section, "status", now)
    assert result["all_fields_current"] is False
    assert result["partial_data"] is False
    assert result["failed_root_count"] == 4


def test_non_root_error_does_not_invent_a_failed_root_count():
    section, now = coordinator(ProviderErrorInfo("network"))
    section.last_update_success = False
    result = diagnostics._section_diagnostics(section, "status", now)
    assert result["all_fields_current"] is False
    assert result["failed_root_count"] is None


def test_optional_plan_diagnostics_count_modules_without_reading_targets_or_car_ids():
    section, now = coordinator(
        ProviderErrorInfo("partial", failed_roots=("evTimeline",), failed_request_count=1)
    )
    section.section = "ev_plan"
    section.client._record_error(
        "ev_plan",
        "PRIVATE_SITE",
        ProviderErrorInfo("partial", failed_roots=("evTimeline",), failed_request_count=1),
    )
    section.current_fleet = lambda: [{"entity_id": "PRIVATE_CAR"}]
    section.data = {
        "vehicle_count": 1,
        "vehicles": [
            {
                "entity_id": "PRIVATE_CAR",
                "timeline": {"query_success": False, "reported": False, "intervals": []},
                "ready_by": {
                    "query_success": True,
                    "reported": True,
                    "target_soc": 85,
                    "target_time": "PRIVATE_TIME",
                },
            }
        ],
    }
    result = diagnostics._section_diagnostics(section, "ev_plan", now)
    assert result["dependency_current"] is True
    assert result["partial_data"] is True
    assert result["all_fields_current"] is False
    assert result["vehicle_count"] == 1
    assert result["expected_module_count"] == 2
    assert result["successful_module_count"] == result["reported_module_count"] == 1
    assert result["forecast_periods_reported"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert "target_soc" not in json.dumps(result)


def test_unknown_optional_modules_do_not_report_zero_or_completeness():
    section, now = coordinator()
    section.section = "ev_history"
    section.current_fleet = lambda: [{"entity_id": "PRIVATE_CAR"}]
    section.data = {"vehicles": [], "vehicle_count": None}
    result = diagnostics._section_diagnostics(section, "ev_history", now)
    assert result["all_fields_current"] is False
    assert result["vehicle_count"] is None
    assert result["successful_module_count"] is None
    assert result["history_day_rows_reported"] is None


def optional_scope(section_name, response_ids, live_ids):
    section, now = coordinator()
    section.section = section_name
    section.current_fleet = lambda: [{"entity_id": identity} for identity in live_ids]
    meta = {"query_success": True, "reported": True, "fetched_at": now.isoformat()}
    section.data = {
        "vehicle_count": len(response_ids),
        "vehicles": [
            {
                "entity_id": identity,
                "timeline": {**meta, "intervals": [{"PRIVATE_FORECAST": 85}]},
                "ready_by": {**meta, "target_time": "PRIVATE_TIME"},
                "history": {**meta, "days": [{"PRIVATE_HISTORY": 999}]},
            }
            for identity in response_ids
        ],
    }
    return diagnostics._section_diagnostics(section, section_name, now)


@pytest.mark.parametrize("section_name", ["ev_plan", "ev_history"])
@pytest.mark.parametrize(
    ("response_ids", "live_ids"),
    [
        (["PRIVATE_OLD"], ["PRIVATE_NEW"]),
        (["PRIVATE_OLD"], ["PRIVATE_OLD", "PRIVATE_NEW"]),
        (["PRIVATE_OLD"], []),
        (["PRIVATE_OLD", "PRIVATE_OLD"], ["PRIVATE_OLD"]),
    ],
)
def test_changed_or_duplicate_fleet_scope_is_not_complete_and_counts_are_unknown(
    section_name, response_ids, live_ids
):
    result = optional_scope(section_name, response_ids, live_ids)
    assert result["dependency_current"] is True  # Live fleet itself remains current.
    assert result["last_update_success"] is True
    assert result["fresh"] is True
    assert result["all_fields_current"] is False
    for field in (
        "vehicle_count",
        "expected_module_count",
        "successful_module_count",
        "reported_module_count",
    ):
        assert result[field] is None
    field = (
        "forecast_periods_reported" if section_name == "ev_plan" else "history_day_rows_reported"
    )
    assert result[field] is None
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("section_name", ["ev_plan", "ev_history"])
def test_identical_reordered_fleet_scope_remains_complete_without_identifiers(section_name):
    result = optional_scope(section_name, ["PRIVATE_A", "PRIVATE_B"], ["PRIVATE_B", "PRIVATE_A"])
    assert result["all_fields_current"] is True
    assert result["vehicle_count"] == 2
    assert result["expected_module_count"] == (4 if section_name == "ev_plan" else 2)
    assert result["successful_module_count"] == result["expected_module_count"]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("section_name", ["ev_plan", "ev_history"])
def test_matching_known_empty_fleet_preserves_real_zero_counts(section_name):
    result = optional_scope(section_name, [], [])
    assert result["all_fields_current"] is True
    assert result["vehicle_count"] == result["successful_module_count"] == 0
    field = (
        "forecast_periods_reported" if section_name == "ev_plan" else "history_day_rows_reported"
    )
    assert result[field] == 0
