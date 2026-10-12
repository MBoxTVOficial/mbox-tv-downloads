# Multi-provider degraded sports agenda

## Current release scope (2026-10-12)

Enabled agenda routes: LPF, PD, PL, SA and FL1, all LIMITED_AUXILIARY.
CL remains UNVALIDATED; CONMEBOL has no auxiliary route. Auxiliary fields are
restricted to provider identity, competition, teams and ART kickoff. No provider
score, status, venue, broadcaster or channel is copied. Fixture-specific evidence
still decides channels. Levante-Sevilla remains REVIEW with no assignment.

The user repeated authenticated checks locally on October 12: catalog HTTP 200,
PD/PL/SA/FL1 present, PD season 2026 matches HTTP 200 with 380 records. Two
requests total; token removed from memory. Current PL/SA/FL1 match endpoints were
not rechecked; earlier authenticated access and five official matches per route
remain the evidence. No token or raw authenticated response is stored here.

GENERAL is wired to the new agenda generator but stays paused. Its original
commented schedules are preserved exactly; refreshTimesART is only a proposed
four-window cadence for a later approval. LIVE and Cloudflare stay paused; the
existing LIVE CLI also exits successfully with zero API-Football calls while
apiFootballLiveEnabled is false. API-Football adapter and its 90/day ceiling remain.
Workflow credentials must be available as repository secrets before any future
activation; this release neither configures secrets nor executes a workflow.

The sections below record the implementation and validation stages; historical
"local", "proposed" and "not deployed" statements refer to those earlier stages.

No workflow, schedule, secret or production feed is changed. No provider request
is performed. Run the prototype as `python -m scripts.competition_source_router`.

## Routing

- LPF: Football Soccer API `lg_22EEYKR`, LIMITED_AUXILIARY; partial coverage.
- Europe: football-data.org v4; PD/PL/SA/FL1/CL, all UNVALIDATED and blocked.
- All other competitions: no auxiliary route.
- Primary available: call the existing pipeline and retain its sports data.
- Primary unavailable: caller must explicitly raise PrimaryUnavailable. Arbitrary
  exceptions are not swallowed. The scheduled generator is not wired to this path.

Official competition fixtures have priority. Exact full-team identities (or
explicit verified aliases), competition and kickoff are required for enrichment;
time tolerance is zero. An official snapshot is authoritative for its competition
and requested day. Unknown official fixtures do not enter via the provider.
Snapshots must be complete for the day before claiming authoritative coverage.
Without an official snapshot, only the validated LPF route may supply partial
agenda records. Missing fixtures are never synthesized.

All timestamps require an offset, convert through America/Argentina/Buenos_Aires
and are filtered by ART date. Provider IDs are secondary; deterministic internal
IDs use a separate large integer namespace, never claiming API-Football identity.
An existing official integer ID is retained. Future cross-provider identity
reconciliation needs explicit mappings; existing API-Football assignments must
not be inferred by team names.

Auxiliary scores/status/venue/logos/broadcasters/channels are discarded. Only
future kickoffs are projected as SCHEDULED agenda cards; this is presentation of
a future schedule, not assertion of the provider's actual match state. Past
fixtures with unknown state are omitted. No LIVE or FINISHED is manufactured.

The normal schemaVersion remains 1; sportsDataMode is an optional NORMAL/DEGRADED
field. Provider provenance stays in the router report, outside Android events.
Existing broadcaster discovery can receive the safe feed through the injected
discovery callback; the callback must use existing confidence/group resolution,
never auxiliary broadcaster hints. Default offline preview makes no discovery
requests and produces no channels. No new realtime path is installed.

## Offline preview

Input JSON contains providerRows and officialRows (normalized records with
competition, homeTeam, awayTeam, startTime; provider rows also competitionId and
providerFixtureId), plus optional verified teamAliases. Raw cached Football Soccer
rows can be converted with football_soccer_snapshot, which whitelists fields.

`python -m scripts.competition_source_router --snapshot INPUT.json --date YYYY-MM-DD --now ISO_WITH_OFFSET --output sports_today.preview.json`

The real sports_today.json output is explicitly refused. No token is read.

European integration requires five official comparisons per competition. No
authenticated football-data.org snapshot or token is available in this session.
Account access, season and coverage remain unknown. Published football-data.org
Free limit is 10/minute; adapter application cap is 6/execution and 8/minute,
sequential with 8-second timeout, no retries and in-memory daily normalized cache.
No daily limit is asserted. API-Football budget is separate and unchanged.
FOOTBALL_DATA_TOKEN is supplied only in memory; no credential file is created.
The adapter does not print token, raw error body, headers or URLs. Its catalog
only establishes catalog visibility; match access needs its own HTTP validation.

Schedules remain paused. MAX_DAILY_API_CALLS remains 90. The existing cadence test
now validates the commented schedules retained for reactivation, not active cron.

## Validation update 2026-10-11

Authenticated catalog and five match queries were executed by the user locally.
All five codes returned HTTP success and fixtures; token was removed.
PD/PL/SA/FL1: five exact official comparisons each, LIMITED_AUXILIARY.
CL: five teams/dates confirmed, explicit timezone unresolved (UEFA labels CET
in October); remains UNVALIDATED, disabled. No provider requests added.
Evidence: tests/data/football_data_validation.json. Full response coverage not
available (only five rows printed), so missing/extras cannot be counted reliably.
No schedule activation or publication. Previous statements about all European
routes being unvalidated describe the state before this update.

## Phase 2 — proposed publication, NOT deployed

LPF gate: football_soccer_argentina / lg_22EEYKR, six historical exact matches,
three concrete records in tests/data/lpf_agenda_validation.json. Europe enabled
in local config only: PD/PL/SA/FL1, LIMITED_AUXILIARY. CL and CONMEBOL disabled.

GENERAL proposed entry point: python -m scripts.generate_agenda_today
--discover-broadcasts. LIVE keeps its old workflow/adapter, but --live exits 0
with LIVE_DISABLED and zero API calls under sports_agenda_providers.json.
No Cloudflare change. All schedules remain commented, including proposed cron:
5 3 * * *, 0 15 * * *, 0 20 * * *, 0 0 * * * (00:05/12:00/17:00/21:00 ART).
Original GENERAL slots before migration: 5 3, 0 11, 0 15, 0 20, 0 23 UTC.

Typical daily agenda requests: four FD competitions * four refreshes = 16;
one LPF request * four = 4. Discovery is separately capped at 6 per GENERAL,
24/day; thus at most 44 external requests/day in this normal four-run scenario.
Daily application ceilings: LPF 40 (MAX_FOOTBALL_SOCCER_API_CALLS), FD 32;
API-Football MAX_DAILY_API_CALLS stays 90 and is not consumed by this path.
FD is sequential at 8/minute, 8-second timeout, no retries. LPF timeout 8s,
no pagination/retry. Budget reservations count failures and use UTC dates.
Budget state is restored by Actions cache, not a guaranteed transactional
quota store; lost cache may undercount. Provider server limits remain authoritative.

Identity: sports_fixture_identity.json persists provider:providerFixtureId ->
mboxFixtureId. IDs fit a positive 32-bit integer namespace. Kickoff/team changes
do not change provider identity; collisions fail closed. No fuzzy assignments.
Unmapped same-day confirmed legacy identities retain the entire last good feed
and require explicit reconciliation instead of dropping channels.

Provider failure or empty feed leaves the old sports_today.json bytes untouched.
Discovery failure retains scoped confirmed assignments; source APIs never supply
channels. No new LIVE or FINISHED state/score is synthesized. Only future agenda
is projected as SCHEDULED. Android's existing 30-minute stale policy still hides
these cards; a future Android phase needs local kickoff-derived display status.
No Android change is included now.

Preview: C:/Users/Estudiante/AppData/Local/Temp/mbox-phase2-preview/
PREVIEW_REPORT.json. It reuses real authenticated samples from the user, not a
fresh complete agenda download. 22 future sample fixtures across 12/16/17/18 Oct,
no channels because no new discovery was run. No production feed was written.

No credentials are created here. Automated use after approval requires existing
FOOTBALL_DATA_TOKEN and FOOTBALL_SOCCER_API_KEY secrets to be configured by the
user; their presence/validity was not established in this intervention.

Rollback before activation: revert proposed changes, retain paused schedules and
Cloudflare; preserve identity and last-good feed snapshots for recovery. Do not
reactivate API-Football LIVE while the account remains suspended. Activation is
separate from publishing code and requires final approval plus real preview.
