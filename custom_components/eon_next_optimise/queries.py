"""Fixed read-only operations reduced from the official Optimise app queries.

The signed-in app uses /graphql and its Cognito ID JWT without a Bearer prefix.
No account profile, billing, payment, authorisation URL or token query is used.
"""

DISCOVERY_QUERY = """query OptimiseSites {
  settings {
    selectedSiteId
    siteIds
    sites { siteId siteStatus }
  }
}"""

PRICES_QUERY = """query OptimisePrices($siteId: String) {
  sitePricing(siteId: $siteId) {
    meterWindows {
      usageType
      currentPeriod {
        start kwhPriceInCents estimate renewablePercentage indicator demandWindow
        kwhPriceRange { minInCents maxInCents }
      }
      previousPeriods {
        start kwhPriceInCents estimate renewablePercentage indicator demandWindow
        kwhPriceRange { minInCents maxInCents }
      }
      forecastPeriods {
        start kwhPriceInCents estimate renewablePercentage indicator demandWindow
        kwhPriceRange { minInCents maxInCents }
      }
    }
  }
}"""

LIVE_QUERY = """query OptimiseLive($siteId: String) {
  smartshift {
    live(siteId: $siteId) {
      sitePowerW solarPowerW fixedLoadPowerW batteryPowerW
      batteryEnergyWh batteryMaxEnergyWh stateOfChargePercentage
      powerState powerStateDescription powerStateAction
      powerStateActionTitle powerStateActionDescription
      currentGeneralUsagePrice currentFeedInPrice currentRenewablePercentage
      isSolarCurtailmentActive
      liveMetrics {
        __typename
        ... on SmartShiftMetricsWithInterval {
          intervalStart intervalEnd
          importCostsCents exportEarningsCents totalEarningsCents
          siteImportKWh siteExportKWh siteNetKWh
          batteryImportKWh batteryExportKWh batteryNetKWh
          solarExportKWh solarImportKWh fixedLoadNetKWh
        }
        ... on SmartShiftDataError { type }
      }
    }
  }
}"""

PLAN_QUERY = """query OptimisePlan($siteId: String) {
  smartshift {
    plan(siteId: $siteId) {
      forecastData {
        __typename
        ... on PastAndForecastData {
          forecast {
            periodStart periodEnd generalUsagePriceCents feedInPriceCents
            batteryEnergyWh solarPowerW fixedLoadPowerW actionDescriptions
            actionDecisions {
              __typename
              ... on SmartShiftBatteryActionDecision { batteryAction: action reason }
              ... on SmartShiftSolarActionDecision { solarAction: action reason }
            }
          }
          forecastError { type }
        }
        ... on SmartShiftDataError { type }
      }
    }
  }
}"""

STATUS_QUERY = """query OptimiseStatus($siteId: String) {
  unifiedDevicesSummary(siteId: $siteId) {
    hasError
    devices {
      deviceTypeEnum hasError isOnboarding
      displayValues { deviceType brand model status }
    }
  }
}"""

STATUS_EXPERIENCE_QUERY = """query OptimiseStatusExperience($siteId: String) {
  smartshiftExperience(siteId: $siteId) {
    batteryEnrolled solarEnrolled evEnrolled evDevicesEnrolled
  }
}"""

STATUS_STRATEGY_QUERY = """query OptimiseStatusStrategy($siteId: String) {
  smartshiftBatteryStrategyConfig(siteId: $siteId) {
    status globalKillswitchEnabled
  }
}"""

STATUS_SETTINGS_QUERY = """query OptimiseStatusSettings($siteId: String) {
  smartshiftUserSettings(siteId: $siteId) {
    solarCurtailment { enabled supported }
    battery { minReserveLevel { value } }
  }
}"""

EV_QUERY = """query OptimiseEv($siteId: String!) {
  evLive(siteId: $siteId) {
    status
    vehicles {
      entityId batteryLevel liveState connectivity connectivityLabel
      chargingSpeedLabel makeId modelName readyByEligible
    }
    siteInformation {
      generalPriceCents feedInPriceCents renewablePercentage isInDemandWindow
    }
  }
}"""

EV_TIMELINE_QUERY = """query OptimiseEvTimeline($siteId: String!, $entityId: String!) {
  evTimeline(siteId: $siteId, entityId: $entityId) {
    currentInterval {
      intervalStartTimeIso intervalStartSocPercent intervalLengthMinutes
      generalPriceCents chargeEnergySourceEnum
    }
    futureIntervals {
      intervalStartTimeIso intervalStartSocPercent intervalLengthMinutes
      generalPriceCents chargeEnergySourceEnum
    }
  }
}"""

EV_READY_BY_QUERY = """query OptimiseEvReadyBy($siteId: String!, $entityId: String!) {
  evReadyByState(siteId: $siteId, entityId: $entityId) {
    oneShotConfig {
      targetSocPercent targetTime createdAt isActive cancelledAt isRecurring
    }
  }
}"""

EV_HISTORY_QUERY = """query OptimiseEvHistory($entityId: String!) {
  evDailySummary(entityId: $entityId) {
    nominalBatteryCapacityWh nominalEconomyKmPerKWh
    dailySummary {
      date totalHomeChargeAddedKWh totalAwayChargeAddedKWh
      grid { totalAddedKWh totalCostCents avgPriceCentsPerKWh }
      solar { totalAddedKWh }
      battery { totalAddedKWh }
    }
  }
}"""

OPERATIONS = {
    "sites": {"operation_name": "OptimiseSites", "query": DISCOVERY_QUERY},
    "prices": {"operation_name": "OptimisePrices", "query": PRICES_QUERY},
    "live": {"operation_name": "OptimiseLive", "query": LIVE_QUERY},
    "plan": {"operation_name": "OptimisePlan", "query": PLAN_QUERY},
    "status": {"operation_name": "OptimiseStatus", "query": STATUS_QUERY},
    "status_experience": {
        "operation_name": "OptimiseStatusExperience",
        "query": STATUS_EXPERIENCE_QUERY,
    },
    "status_strategy": {
        "operation_name": "OptimiseStatusStrategy",
        "query": STATUS_STRATEGY_QUERY,
    },
    "status_settings": {
        "operation_name": "OptimiseStatusSettings",
        "query": STATUS_SETTINGS_QUERY,
    },
    "ev": {"operation_name": "OptimiseEv", "query": EV_QUERY},
    "ev_timeline": {"operation_name": "OptimiseEvTimeline", "query": EV_TIMELINE_QUERY},
    "ev_ready_by": {"operation_name": "OptimiseEvReadyBy", "query": EV_READY_BY_QUERY},
    "ev_history": {"operation_name": "OptimiseEvHistory", "query": EV_HISTORY_QUERY},
}

STATUS_ROOTS = {
    "status": "unifiedDevicesSummary",
    "status_experience": "smartshiftExperience",
    "status_strategy": "smartshiftBatteryStrategyConfig",
    "status_settings": "smartshiftUserSettings",
}

EV_FLEET_ROOTS = {
    "ev_timeline": "evTimeline",
    "ev_ready_by": "evReadyByState",
    "ev_history": "evDailySummary",
}

OPERATION_NAMES = frozenset(item["operation_name"] for item in OPERATIONS.values())
