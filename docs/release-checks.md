# Release checks — 0.2.0a5

This prerelease gives the expanded read-only integration its own repository and
HACS update source. The component's executable Python files are unchanged from
the validated 0.2.0a4 build. Component changes are limited to the manifest's
version, maintainer, documentation and issue-tracker URLs.

## Scope

- Twelve fixed GraphQL queries and seven independently scheduled feeds.
- Prices, site/battery telemetry, enrolment/device status, EV telemetry,
  provider charging forecasts, ready-by configuration and dated EV history.
- Sensors and binary sensors only; no provider mutations or energy controls.
- Normal account authentication, interactive SMS/TOTP handling and renewable
  sessions, without saving passwords or MFA codes.
- A public source snapshot with GitHub noreply authorship. Original MIT notices,
  upstream provenance and artwork attribution are retained.

## Validation

The expanded build has been tested on Home Assistant 2026.10.4. Its offline
suite covers authentication/JWT verification, refresh rotation, concurrency,
shutdown, partial failures, units, bounds, privacy, discovery and stale data.
Network access is disabled during tests, apart from asyncio's internal Unix
socket pair. Ruff lint and formatting checks are required.

The publication candidate passed 455 tests and 18 subtests, with Ruff lint and
formatting checks passing. One upstream pycognito `utcnow` deprecation warning
remains. The production dependency stays pinned to `pycognito==2024.5.1`.

The repository's **Validate** workflow runs:

1. The pinned Home Assistant test suite, Ruff and formatting checks.
2. Official Home Assistant Hassfest.
3. The HACS integration repository validator.

Published releases should refer to passing checks for their exact source commit.
GitHub prereleases require beta versions to be enabled for this repository in
HACS. The standard `custom_components/eon_next_optimise` layout is used; HACS
downloads the tagged source. The attached ZIP is a manual-install alternative,
and `zip_release` is not enabled.

## Availability limits

Optional data depends on the account's provider enrolment and device support.
Unavailable or missing readings are preserved as unavailable, not replaced with
zero. Successful partial status queries do not imply complete battery settings.
Configured ready-by values can be absent when no configuration is reported.

Forecasts contain at most 100 explicit intervals within 48 hours and flag
truncation. Charging history preserves each reported date; its latest values
are not assumed to mean today. History/interval energy and cost readings have
no statistical counter class until reset semantics are verified.

An app/API difference of one percentage point in EV state of charge was observed
during validation. No adjustment is applied to the provider value, and no cause
is asserted. Successful retrieval timestamps describe fetch time, not a proven
physical measurement timestamp.

## Distribution

Repository: <https://github.com/tmccc/ha-eon-next-optimise>.
Initial expanded-fork release: `v0.2.0a5`.

Source and release assets contain no account credentials, user account/device
identifiers, private lab infrastructure, app binaries or personal screenshots.
This is an unofficial community integration. HACS default-catalogue inclusion
is separate from use as a custom repository and has not been requested.
