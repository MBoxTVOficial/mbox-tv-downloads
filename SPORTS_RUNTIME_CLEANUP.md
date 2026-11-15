# Public sports runtime and retained API compatibility

GENERAL executes `python -m scripts.generate_public_agenda`. This entry point always uses
the existing official-sites/Argentina-guides publication engine, with its same request
budget, last-good-feed policy, identities and broadcaster evidence rules. It has no API
provider selection option and does not import football-data, FootballSoccer's router or
the compatibility agenda CLI. It retains the same safe per-source annotations and exits
with failure when the available feed is not for today's ART date.

The daily cache restores `official_source_cache.json` and `argentina_guide_cache.json`.
The retired API/discovery cache paths were removed from GENERAL's restore list; their
files were not deleted. Cache keys and restore prefixes, permissions, concurrency,
token bindings and the match-update step were preserved. The later two-hour cadence
is documented in `SPORTS_TWO_HOUR_UPDATES.md`.

## Retained for a deliberate future migration

- `generate_agenda_today.py` and the football-data/FootballSoccer adapters remain as
  compatibility code, outside GENERAL's production entry point. Existing offline tests
  still exercise their limits and normalization contracts.
- `generate_sports_today.py` cannot be deleted wholesale: the public engine uses its
  shared schema, timezone, sorting, validation and atomic-write helpers.
- `sports_agenda_providers.json` remains in place. The disabled API-Football LIVE path
  currently relies on this file's `apiFootballLiveEnabled=false` guard.
- The old LIVE workflow retains manual dispatch and its automatic schedules remain
  paused. Informative final results run within GENERAL every two hours;
  the separate public match-update workflow refreshes confirmed-channel match
  states every five minutes, with pending-match and request-budget guards. See
  `SPORTS_TWO_HOUR_UPDATES.md` for the current cadence.
- No secrets, credentials, feed artifacts or Cloudflare configuration were changed.

## Recovery reference

Commit `1c261fb94a37119a9abfae6fbd3c74a222b53a97` preserves the preceding workflow and
compatibility entry point. For review, `git show
1c261fb94a37119a9abfae6fbd3c74a222b53a97:.github/workflows/update-sports-today.yml`
shows the old command/cache paths. A future API migration should restore only the
intended provider path after checking account access, limits and validations; it must
not blindly restore old schedules or Cloudflare cron configuration.

This cleanup reduces coupling and prevents GENERAL from accidentally selecting a paid
API path. It is not an optimization of Android's IPTV catalog loading: that requires
the independently measured menu/model work in the app.
