# Release checks — 0.2.0a6

This prerelease fixes tariff availability when optional renewable-percentage
metadata is outside its supported range. It also adds a separate sensor for the
battery level shown by the official app, preserving the existing live EV value.

## Changes

- A finite renewable percentage outside 0–100 becomes unknown for that interval
  without discarding valid tariff quotes. Price amounts, signs, interval bounds,
  duplicate conflicts, malformed types and non-finite numbers remain validated.
- **Battery level shown in app** follows the provider timeline interval covering
  the current time and applies the app's floor/clamp display rule. Attributes
  retain its source, raw percentage, interval bounds and rounding method.
- **State of charge** remains the unmodified live EV field. The companion is a
  prediction/display value, has no measurement state class and makes no extra
  request. Future starts and timeline gaps do not supply a current value.
- Each EV gains one entity. Existing entity identities and parent/child devices
  are preserved. No authentication, query, credential-storage or control changes.

## Validation

The build is tested against Home Assistant 2026.10.4. The offline suite covers
JWT verification, token rotation, concurrency, shutdown, partial failures,
units, bounds, privacy, discovery, stale data and the new regressions. Network
access is disabled apart from asyncio's internal Unix socket pair.

The candidate passed 534 tests and 18 subtests, with Ruff lint and formatting
checks passing. One upstream pycognito `utcnow` deprecation warning remains;
the production dependency stays pinned to `pycognito==2024.5.1`.

A bounded read-only diagnostic identified a rejected production response:
forecast renewable metadata contained the finite value −100. The candidate
parser accepted that same cached response and recovered both price channels,
without making another provider request or exporting account credentials or
the complete response. Battery source/rounding behavior was traced in the
Google Play app's signed code; synthetic tests preserve the independent live
value while checking the app-display projection.

The repository's **Validate** workflow runs the pinned tests/Ruff checks,
official Home Assistant Hassfest, and the HACS integration validator. Releases
must refer to passing checks for their exact source commit. A core restart is
required to load changed Python modules; config-entry reload alone does not
replace already imported code. Final live deployment and app comparisons are
recorded separately after the running Home Assistant instance is updated.

## Availability and interpretation

Optional data still depends on provider enrolment and device support. Missing
or stale readings remain unavailable; unsupported metadata is not replaced by
zero. Partial status success does not imply complete battery settings.

The app's timeline gauge and its live vehicle endpoint may report different
percentages. The two HA sensors make those sources explicit. Fetch timestamps
describe retrieval time, not a proven physical measurement timestamp.

Forecasts retain at most 100 explicit intervals within 48 hours and flag
truncation. History values retain their reported date and are not assumed to
mean today. History/interval energy and cost readings are not lifetime counters.

## Distribution and provenance

Repository: <https://github.com/tmccc/ha-eon-next-optimise>.
Release: `v0.2.0a6` (prerelease; enable beta versions in HACS).

HACS uses the standard tagged source layout. The attached ZIP is an alternative
for manual installation; `zip_release` is not enabled. Original MIT notices,
upstream provenance and artwork attribution are retained. Public source and
assets exclude credentials, customer identities, private infrastructure, app
binaries and personal screenshots. This is an unofficial community integration.
