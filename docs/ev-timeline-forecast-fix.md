# EV timeline forecast correction — 0.2.0a7 candidate

A finite future battery projection slightly above 100% could make the entire
EV timeline unavailable. The provider can return a valid current interval and
future values such as 100.09% in the same response; rejecting every interval
also removes the valid app-display estimate and next charging forecast.

The parser now keeps the raw numeric projection separately from a bounded
0–100% state-of-charge field. Out-of-range projections leave only that bounded
value unknown. The app-display sensor uses the raw value for its existing
floor-and-clamp display rule, retaining that raw value as an attribute.
Live vehicle SOC is unchanged. Invalid numeric types, non-finite values,
malformed times/durations, overlaps and conflicting duplicates remain errors.

Regression coverage includes a synthetic 289-row plan with a valid current
interval and 189 later projections above 100%, including rows beyond the
100-row output limit. Tests also cover duplicate raw values, missing/zero/
negative SOC, gauge clipping, unavailable individual forecast points and API
error-cache recovery.

Price parsing, authentication, query documents, polling, entity identities and
Powerwall controls are outside this change. This candidate requires a normal
Home Assistant restart to load after installation; reloading an entry does not
re-import its Python modules.
