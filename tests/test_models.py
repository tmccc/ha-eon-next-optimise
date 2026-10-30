"""Offline contract tests with synthetic telemetry, independent of HA and AWS."""

import importlib.util
import json
import unittest
from datetime import timedelta
from pathlib import Path


def load(name):
    path = Path(__file__).parents[1] / "custom_components/eon_next_optimise" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"optimise_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


models = load("models")
queries = load("queries")


def prices(rows, usage="GENERAL"):
    return {
        "data": {"sitePricing": {"meterWindows": [{"usageType": usage, "forecastPeriods": rows}]}}
    }


def period(start="2026-10-30T12:00:00Z", value=20, **kwargs):
    return {"start": start, "kwhPriceInCents": value, **kwargs}


def plan(rows):
    return {
        "data": {
            "smartshift": {
                "plan": {"forecastData": {"__typename": "PastAndForecastData", "forecast": rows}}
            }
        }
    }


class ModelsTest(unittest.TestCase):
    def test_discovery_does_not_select_inaccessible_site(self):
        result = models.parse_sites(
            {
                "data": {
                    "settings": {
                        "selectedSiteId": "unavailable",
                        "siteIds": ["site_alpha", "site_beta"],
                        "sites": [{"siteId": "site_beta", "siteStatus": "ACTIVE"}],
                    }
                }
            }
        )
        self.assertEqual([item["selected"] for item in result], [False, False])
        self.assertEqual(result[1]["status"], "ACTIVE")

    def test_duplicate_site_rejected(self):
        with self.assertRaises(models.DataError):
            models.parse_sites({"data": {"settings": {"siteIds": ["site_alpha", "site_alpha"]}}})

    def test_missing_direction_is_independent(self):
        result = models.parse_prices(prices([period(value=0)]))
        self.assertEqual(result["export"], [])
        self.assertEqual(result["import"][0]["value"], 0)

    def test_negative_export_and_zero_import_preserved(self):
        payload = prices([period(value=0)])
        payload["data"]["sitePricing"]["meterWindows"].append(
            {"usageType": "FEED_IN", "currentPeriod": period(value=3)}
        )
        result = models.parse_prices(payload)
        self.assertEqual(result["import"][0]["value"], 0)
        self.assertEqual(result["export"][0]["value"], -3)

    def test_graphql_partial_price_data_is_rejected(self):
        payload = prices([period()])
        payload["errors"] = [{"message": "synthetic-private-provider-detail"}]
        with self.assertRaises(models.DataError) as caught:
            models.parse_prices(payload)
        self.assertNotIn("synthetic-private", str(caught.exception))

    def test_export_sign_and_range_match_official_display(self):
        result = models.parse_prices(
            prices(
                [period(value=-8, kwhPriceRange={"minInCents": -10, "maxInCents": -5})], "FEED_IN"
            )
        )["export"][0]
        self.assertEqual((result["value"], result["raw_value"]), (8, -8))
        self.assertEqual((result["range_min"], result["range_max"]), (5, 10))
        self.assertNotIn("value_inc_vat", result)

    def test_null_quote_does_not_become_zero(self):
        result = models.parse_prices(prices([period(value=None)]))["import"][0]
        self.assertIsNone(result["value"])

    def test_invalid_numbers_rejected(self):
        for value in (True, "12", float("nan"), float("inf"), 10**1000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(models.DataError):
                models.parse_prices(prices([period(value=value)]))

    def test_price_gaps_are_not_stretched(self):
        rows = models.parse_prices(prices([period(), period("2026-10-30T13:00:00Z")]))["import"]
        self.assertEqual(rows[0]["valid_to"], "2026-10-30T12:30:00+00:00")
        self.assertEqual(
            models.coverage(rows, models.parse_time("2026-10-30T12:45:00Z")), (None, None)
        )

    def test_conflicting_duplicate_and_short_spacing_rejected(self):
        for rows in (
            [period(value=1), period(value=2)],
            [period(), period("2026-10-30T12:05:00Z")],
        ):
            with self.subTest(rows=rows), self.assertRaises(models.DataError):
                models.parse_prices(prices(rows))

    def test_explicit_end_preserved(self):
        row = models.parse_prices(prices([period(end="2026-10-30T12:15:00Z")]))["import"][0]
        self.assertEqual(row["valid_to"], "2026-10-30T12:15:00+00:00")
        self.assertEqual(row["interval_basis"], "provider_end")

    def test_revised_duplicate_keeps_current_provenance(self):
        payload = prices([period(value=20)])
        payload["data"]["sitePricing"]["meterWindows"][0]["currentPeriod"] = period(
            value=20, estimate=False
        )
        row = models.parse_prices(payload)["import"][0]
        self.assertEqual(row["quality"], "current")
        self.assertIs(row["estimate"], False)

    def test_dst_fall_back_instants_remain_distinct(self):
        rows = [period("2026-10-25T01:00:00+01:00"), period("2026-10-25T01:00:00+00:00")]
        result = models.parse_prices(prices(rows))["import"]
        self.assertEqual(result[0]["valid_from"], "2026-10-25T00:00:00+00:00")
        self.assertEqual(result[1]["valid_from"], "2026-10-25T01:00:00+00:00")

    def test_dst_days_allow_46_or_50_periods(self):
        for start, count in (("2026-03-29T00:00:00Z", 46), ("2026-10-24T23:00:00Z", 50)):
            instant = models.parse_time(start)
            rows = [period((instant + timedelta(minutes=30 * i)).isoformat()) for i in range(count)]
            self.assertEqual(len(models.parse_prices(prices(rows))["import"]), count)

    def test_half_open_boundary(self):
        rows = models.parse_prices(prices([period()]))["import"]
        self.assertIsNotNone(models.coverage(rows, models.parse_time("2026-10-30T12:29:59Z"))[0])
        self.assertIsNone(models.coverage(rows, models.parse_time("2026-10-30T12:30:00Z"))[0])

    def test_naive_and_malformed_dates_rejected(self):
        for value in ("2026-10-30T12:00:00", "not-a-time", None):
            with self.subTest(value=value), self.assertRaises(models.DataError):
                models.parse_time(value)

    def test_live_direction_and_missing_are_distinct(self):
        result = models.parse_live(
            {
                "data": {
                    "smartshift": {
                        "live": {
                            "sitePowerW": -1200,
                            "batteryPowerW": 400,
                            "solarPowerW": 0,
                            "stateOfChargePercentage": 0,
                        }
                    }
                }
            }
        )
        self.assertEqual(result["gridExportPowerW"], 1200)
        self.assertEqual(result["gridImportPowerW"], 0)
        self.assertEqual(result["batteryDischargePowerW"], 400)
        self.assertEqual(result["batteryChargePowerW"], 0)
        self.assertEqual(result["stateOfChargePercentage"], 0)
        self.assertIsNone(result["batteryMaxEnergyWh"])
        absent = models.parse_live({"data": {"smartshift": None}})
        self.assertIsNone(absent["gridImportPowerW"])
        self.assertIsNone(absent["isSolarCurtailmentActive"])

    def test_invalid_soc_does_not_get_clamped(self):
        with self.assertRaises(models.DataError):
            models.parse_live({"data": {"smartshift": {"live": {"stateOfChargePercentage": 101}}}})

    def test_live_interval_metrics_preserve_units_and_utc_bounds(self):
        metrics = {
            "__typename": "SmartShiftMetricsWithInterval",
            "intervalStart": "2026-07-01T13:00:00+01:00",
            "intervalEnd": "2026-07-01T13:30:00+01:00",
            "importCostsCents": 125,
            "exportEarningsCents": 35,
            "totalEarningsCents": -90,
            "siteImportKWh": 2.5,
            "siteNetKWh": -1.25,
            "batteryImportKWh": 0,
        }
        result = models.parse_live(
            {
                "data": {
                    "smartshift": {"live": {"stateOfChargePercentage": 50, "liveMetrics": metrics}}
                }
            }
        )
        self.assertEqual(result["metrics_interval_start"], "2026-07-01T12:00:00+00:00")
        self.assertEqual(result["metrics_interval_end"], "2026-07-01T12:30:00+00:00")
        self.assertEqual(result["importCostsCents"], 125)
        self.assertEqual(result["totalEarningsCents"], -90)
        self.assertEqual(result["siteNetKWh"], -1.25)
        self.assertEqual(result["batteryImportKWh"], 0)
        self.assertIsNone(result["solarExportKWh"])

    def test_missing_or_error_metrics_preserve_power_and_soc(self):
        for metrics, expected_error in (
            (None, None),
            (
                {
                    "__typename": "SmartShiftDataError",
                    "type": "NOT_READY",
                    "message": "private-secret",
                },
                "NOT_READY",
            ),
            ({"__typename": "FutureMetricsType"}, "unsupported_data_type"),
        ):
            with self.subTest(metrics=metrics):
                result = models.parse_live(
                    {
                        "data": {
                            "smartshift": {
                                "live": {
                                    "sitePowerW": 500,
                                    "stateOfChargePercentage": 50,
                                    "liveMetrics": metrics,
                                }
                            }
                        }
                    }
                )
                self.assertEqual(result["sitePowerW"], 500)
                self.assertEqual(result["stateOfChargePercentage"], 50)
                self.assertEqual(result["metrics_error_type"], expected_error)
                self.assertIsNone(result["siteImportKWh"])
                self.assertNotIn("private-secret", json.dumps(result))

    def test_metrics_invalid_time_bounds_are_isolated(self):
        for start, end in (
            ("2026-10-30T12:00:00", "2026-10-30T12:30:00Z"),
            ("2026-10-30T12:00:00Z", "2026-10-30T11:30:00Z"),
        ):
            metrics = {
                "__typename": "SmartShiftMetricsWithInterval",
                "intervalStart": start,
                "intervalEnd": end,
                "siteImportKWh": 2,
            }
            result = models.parse_live(
                {
                    "data": {
                        "smartshift": {
                            "live": {"stateOfChargePercentage": 50, "liveMetrics": metrics}
                        }
                    }
                }
            )
            self.assertEqual(result["stateOfChargePercentage"], 50)
            self.assertEqual(result["metrics_error_type"], "invalid_metrics")
            self.assertIsNone(result["siteImportKWh"])

    def test_nullable_metrics_and_nonfinite_metrics_do_not_invent_values(self):
        metrics = {
            "__typename": "SmartShiftMetricsWithInterval",
            "intervalStart": None,
            "intervalEnd": None,
            "importCostsCents": None,
        }
        result = models.parse_live({"data": {"smartshift": {"live": {"liveMetrics": metrics}}}})
        self.assertIsNone(result["metrics_interval_start"])
        self.assertIsNone(result["metrics_interval_end"])
        self.assertIsNone(result["importCostsCents"])
        for value in (True, float("nan"), float("inf"), "2.5"):
            metrics["importCostsCents"] = value
            result = models.parse_live(
                {
                    "data": {
                        "smartshift": {
                            "live": {"stateOfChargePercentage": 50, "liveMetrics": metrics}
                        }
                    }
                }
            )
            self.assertEqual(result["stateOfChargePercentage"], 50)
            self.assertEqual(result["metrics_error_type"], "invalid_metrics")
            self.assertIsNone(result["importCostsCents"])

    def test_plan_uses_explicit_interval_and_export_sign(self):
        result = models.parse_plan(
            plan(
                [
                    {
                        "periodStart": "2026-10-30T12:00:00Z",
                        "periodEnd": "2026-10-30T12:15:00Z",
                        "feedInPriceCents": 2,
                        "actionDescriptions": ["Synthetic charge plan"],
                    }
                ]
            )
        )
        self.assertEqual(result["forecast"][0]["feed_in_price"], -2)
        self.assertEqual(result["forecast"][0]["valid_to"], "2026-10-30T12:15:00+00:00")
        self.assertEqual(result["forecast"][0]["actions"], ["Synthetic charge plan"])
        self.assertIsNone(result["forecast"][0]["battery_energy_wh"])

    def test_plan_error_union_does_not_publish_messages(self):
        payload = {
            "data": {
                "smartshift": {
                    "plan": {
                        "forecastData": {
                            "__typename": "SmartShiftDataError",
                            "type": "NOT_READY",
                            "message": "private-secret",
                        }
                    }
                }
            }
        }
        result = models.parse_plan(payload)
        self.assertEqual(result, {"forecast": [], "error_type": "NOT_READY"})
        self.assertNotIn("private", json.dumps(result))

    def test_null_plan_and_status_are_unknown(self):
        self.assertEqual(
            models.parse_plan({"data": {"smartshift": None}}), {"forecast": [], "error_type": None}
        )
        status = models.parse_status({"data": {}})
        self.assertIsNone(status["device_count"])
        self.assertIsNone(status["has_error"])

    def test_multi_battery_reserve_never_aggregated(self):
        result = models.parse_status(
            {
                "data": {
                    "smartshiftExperience": {"evEnrolled": True},
                    "smartshiftUserSettings": {
                        "battery": [
                            {"minReserveLevel": {"value": 20}},
                            {"minReserveLevel": {"value": 40}},
                            {"minReserveLevel": None},
                        ]
                    },
                    "unifiedDevicesSummary": {"hasError": False, "devices": []},
                }
            }
        )
        self.assertEqual(result["minimum_reserve"], [20, 40, None])
        self.assertIs(result["ev_enrolled"], True)
        self.assertEqual((result["device_count"], result["error_count"]), (0, 0))

    def test_device_counts_and_unknown_error(self):
        payload = {
            "data": {
                "unifiedDevicesSummary": {"devices": [{"hasError": True}, {"hasError": False}]}
            }
        }
        result = models.parse_status(payload)
        self.assertEqual((result["device_count"], result["error_count"]), (2, 1))
        payload["data"]["unifiedDevicesSummary"]["devices"].append({})
        self.assertIsNone(models.parse_status(payload)["error_count"])

    def test_bounded_and_malformed_payloads(self):
        for payload in ({"data": None}, {"data": {}, "errors": [{"message": "secret"}]}):
            with self.assertRaises(models.DataError) as caught:
                models.parse_live(payload)
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(models.DataError):
            models.parse_prices(prices([period()] * (models.MAX_PERIODS + 1)))

    def test_malformed_nested_types_raise_only_data_error(self):
        cases = (
            (models.parse_prices, {"data": {"sitePricing": {"meterWindows": [{"usageType": []}]}}}),
            (models.parse_live, {"data": {"smartshift": []}}),
            (models.parse_status, {"data": {"smartshiftUserSettings": {"battery": {}}}}),
            (models.parse_plan, {"data": {"smartshift": {"plan": {"forecastData": []}}}}),
            (models.parse_sites, {"data": {"settings": {"siteIds": [{}]}}}),
        )
        for parser, payload in cases:
            with self.subTest(parser=parser.__name__), self.assertRaises(models.DataError):
                parser(payload)

    def test_all_output_is_json_serializable(self):
        for value in (
            models.parse_prices(prices([period()])),
            models.parse_live({"data": {}}),
            models.parse_status({"data": {}}),
            models.parse_plan({"data": {}}),
        ):
            json.dumps(value, allow_nan=False)

    def test_queries_are_fixed_read_operations_with_no_private_fields(self):
        self.assertEqual(
            set(queries.OPERATIONS),
            {
                "sites",
                "prices",
                "live",
                "plan",
                "status",
                "ev",
                "status_experience",
                "status_strategy",
                "status_settings",
                "ev_timeline",
                "ev_ready_by",
                "ev_history",
            },
        )
        for item in queries.OPERATIONS.values():
            self.assertTrue(item["query"].startswith("query " + item["operation_name"]))
            for forbidden in (
                "mutation ",
                "subscription ",
                "customerInfo",
                "billing",
                "payment",
                "LorikeetToken",
                "AuthorisationUrl",
                "commandHistory",
                "vehicleLocation",
                "vin",
            ):
                self.assertNotIn(forbidden, item["query"])


if __name__ == "__main__":
    unittest.main()
