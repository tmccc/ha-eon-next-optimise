# E.ON Next Optimise

A comprehensive, initially read-only Home Assistant integration for your own
Next Optimise account. This development fork builds on Mark Graham's MIT-licensed
[Optimise Prices beta](https://github.com/markgraham924/ha-eon-optimise-prices),
with queries checked against the Google Play Android app's signed code.

This integrates the Optimise app's Amber-backed GraphQL service and Cognito
sign-in. It is separate from E.ON supplier billing and Smart Drive integrations.

## What it provides

Each selected site gets a parent device and a child device for each reported EV,
with sensors and binary sensors for:

- Current and next import/export prices, with bounded timestamped forecasts.
- Battery state of charge, stored energy and reported capacity.
- Grid, solar, household and battery power, including separate import/export
  and charge/discharge readings.
- SmartShift power state, strategy, curtailment and enrolment status.
- Supplier-provided battery, solar and load forecasts and plan availability.
- Device counts, reported device errors and battery reserve settings.
- Energy and cost totals for the API's explicitly timestamped metrics interval.
- Separate vehicle devices with EV charge level, reported vehicle state,
  connectivity, charging-speed text and ready-by eligibility.
- A separate battery level shown in the app, derived from the current provider
  timeline interval using the app's display rounding, with its source and raw
  percentage retained as attributes.
- Provider EV charging forecasts, configured ready-by targets and activity,
  and the latest explicitly dated charging-energy and grid-cost summary.
- Reported nominal vehicle battery capacity and economy.
- Independent last-success timestamps and diagnostic freshness for each feed.

Missing, unsupported or stale readings are unavailable. They are not replaced
with zero. A failure in optional plan data does not remove otherwise valid live
power or prices. New accounts may have prices before device telemetry is ready.

The integration exposes no switches, writable numbers, selects, battery commands
or services. Its client sends only twelve fixed GraphQL queries. It does not enrol
devices, authorise battery access, change reserves or alter SmartShift settings.
Its login is a normal app-account session; it is not a provider-issued account
credential restricted to read-only access.

## Requirements and installation

- Home Assistant **2026.10.4 or later**; tested with that exact core version.
- An activated Next Optimise app account and access to its cloud services.
- HACS for the normal repository installation workflow, or a manual component
  installation using this repository's `custom_components/eon_next_optimise`.

This repository maintains the expanded fork, starting with prerelease
**0.2.0a5**. Its HACS source is
[`tmccc/ha-eon-next-optimise`](https://github.com/tmccc/ha-eon-next-optimise).
The original price-only beta remains a separate upstream project.

In **HACS → Custom repositories**, add
`https://github.com/tmccc/ha-eon-next-optimise` with category **Integration**.
Enable beta versions for this repository and download the desired tagged
release. Restart Home Assistant after installation, then add **E.ON Next
Optimise** under Devices & services.

The integration domain remains `eon_next_optimise`. Use one repository source
for that domain. When switching from the original price-only project, retain
the existing Home Assistant configuration entry and credentials; changing the
repository source does not require deleting that entry.

For a manual installation, copy the integration directory directly to
`config/custom_components/eon_next_optimise`, then reload the custom component
inventory through the installation workflow or use your next planned HA restart.
Do not replace an already imported Python module and assume a config-entry
reload loads its new source. Versioned GitHub releases provide HACS updates for
this fork; the release ZIP is also suitable for manual installation.

Open **Settings → Devices & services → Add integration**, search for **Optimise**,
and enter the login used by the Optimise app. If SMS or authenticator MFA is
required, enter its six-digit code in the next form. New-password and other
account verification requirements must be completed through the official app.
Choose the sites to include when the account contains several sites.

Passwords and MFA codes are used only during sign-in and are not saved. HA stores
the username, renewable refresh token and selected site IDs in its private
config-entry storage. A revoked or expired session opens HA's reauthentication
flow; the integration does not keep a password for silent future sign-ins.

## Polling, freshness and recovery

One account client shares authentication and limits simultaneous requests to two.
Each site has independently scheduled feeds:

| Feed | Default interval | Default freshness limit |
| --- | --- | --- |
| Live power and metrics | 60 seconds | 3 minutes |
| EV telemetry | 60 seconds | 3 minutes |
| EV forecasts and ready-by configuration | 5 minutes | 15 minutes |
| EV dated charging history | 15 minutes | 45 minutes |
| Prices | 5 minutes | 15 minutes |
| Forecast plan | 5 minutes | 15 minutes |
| Status and devices | 15 minutes | 45 minutes |

Options allow bounded polling intervals. Freshness limits scale up for longer
intervals. A provider rate limit pauses requests according to a bounded
`Retry-After`; transient failures do not become invalid-password errors.
Concurrent token renewal is serialized and rotated refresh tokens are saved.

Status retrieval isolates device summary, enrolment, strategy and user settings
into four fixed queries. A provider error in an optional battery setting leaves
that setting unavailable while valid device-summary data remains usable.
Diagnostics distinguish successful partial retrieval from complete status data.
Legacy EV enrolment and the newer EV-device enrolment flag remain separate.
Optional per-vehicle queries use the current validated EV fleet; stale discovery
prevents those requests. A failure for one vehicle or optional module preserves
other valid readings. All vehicle entities are discovered from the live fleet,
including those whose optional readings are initially unavailable.

Prices advance at the actual next period boundary without another API request.
Intervals are timezone-aware and half-open. Gaps remain gaps; DST days are not
assumed to contain 48 periods. When no explicit price end is supplied, the
current UK 30-minute period is used and marked as an assumption in rate metadata.
An out-of-range finite renewable percentage is treated as unknown optional
metadata for that interval. It does not discard otherwise valid tariff quotes.
Price amounts, interval bounds, malformed types and non-finite values remain
validated.

## Units and interpretation

- Prices use **p/kWh**. Export rates are earnings: positive means payment for
  export, negative means a cost. The API's raw export price is negated to match
  the app's display. Negative and zero values remain valid.
- The app describes import prices as including VAT where applicable and export
  prices as excluding VAT. The integration adds no tax, booster or tariff uplift.
- Raw positive grid power means import; positive battery power means discharge.
  Separate derived direction sensors preserve a missing source as unavailable.
- Battery stored energy and capacity are energy-storage measurements in Wh,
  not lifetime increasing consumption counters.
- Interval metrics retain their actual `interval_start`/`interval_end` context.
  They are not labelled “today” unless that is actually their interval. Interval
  energy/cost sensors initially have no statistical state class; they should not
  be treated as lifetime Energy Dashboard counters without verified reset rules.
- Forecasts and estimated prices are supplier predictions, not settlement data
  or locally generated control recommendations. Expired forecasts are removed.
- EV forecasts retain at most 100 explicit intervals within 48 hours and mark
  truncation. A short forecast is not extended to imply complete coverage.
- EV history values share one `reported_date` and describe the latest reported
  day. They are not relabelled as today or combined across different dates.
  They have no statistical counter class until reset semantics are verified.
  Grid costs use the explicitly named cents field divided by 100 for GBP;
  ambiguous aggregate-cost fields are excluded.

Only a selected set of account data is requested. Profile details, payment data,
device-authorisation URLs, support tokens and every mutation are outside this
component's request allow-list.

## Privacy and diagnostics

Diagnostic downloads contain counts, freshness and error categories. They omit
email addresses, passwords, tokens, site/device IDs, price histories and complete
provider responses. Cached failure metadata distinguishes HTTP, GraphQL and
parsing failures, retaining only status codes and allowlisted query-schema
identifiers. Downloading diagnostics makes no additional provider requests.
Do not publish HA's private config entries or backups.

EV telemetry comes from Optimise's cloud service, without direct Tesla requests
or wake/charge commands. Online connectivity does not establish that a cable is
plugged in. Vehicle state remains the provider's wording: the app may display
“Resting” for its `Pending next charge` state. Charging speed remains display
text until a numeric power field and unit are verified. VIN, location, command
history and action links are excluded from the query.

The app's large battery gauge and the live EV state-of-charge field have
different sources. The gauge uses the selected timeline interval's starting SOC,
floored and clamped to 0–100; the chart retains fractional values. For example,
a timeline estimate of 79.8% is displayed as 79%, even if the separate live
endpoint reports 80%. **Battery level shown in app** mirrors the interval covering
the current time, without changing **State of charge**, which retains the live
provider value. It does not follow a future point manually selected in the app.
The companion has no measurement state class, requests no extra data, and is
unavailable when the timeline is stale or has a gap. Its attributes include the
unrounded percentage, interval bounds and display method. Fetch timestamps do
not establish when the physical vehicle was last measured.

Normal TLS certificate validation stays enabled. The fixed production endpoint
does not accept arbitrary URLs, documents or redirects. GraphQL variables carry
site IDs as data, without interpolating them into query source.

## Development and provenance

The baseline is upstream commit
`24eec96f5492feb3edbf8b531b5dbc0255b1e9dc` (`v0.1.0b1`). The original MIT licence
and artwork attribution are retained. This fork is unofficial and is not a
product or supported API commitment from E.ON, Amber, HA or HACS.

Use the pinned development dependencies with Python 3.14:

```sh
pip install -r requirements-dev.txt
pytest -q --disable-socket --allow-unix-socket
ruff check .
ruff format --check .
```

The tests use synthetic data, RSA/JWKS fixtures and mocked provider requests.
The Unix-socket exception permits asyncio's internal socketpair; IP connections
remain blocked during the offline tests. Live deployment acceptance is recorded
separately from these offline results.

MIT © 2026 Mark Graham; modifications are maintained in this development fork.

Fork maintenance and extensions: [tmccc](https://github.com/tmccc).
See [release checks](docs/release-checks.md) for validation scope and limitations.
