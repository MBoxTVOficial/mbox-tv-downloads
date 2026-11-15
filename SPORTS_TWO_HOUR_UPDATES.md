# Public football refresh every two hours

GENERAL (`update-sports-today.yml`) now refreshes the public agenda and then invokes
the existing grouped result updater once per run. The expected ART windows are
00:00, 02:00, 04:00, 06:00, 08:00, 10:00, 12:00, 14:00, 16:00, 18:00, 20:00 and
22:00. GitHub stores cron in UTC, so the expression is `0 1-23/2 * * *`.
GitHub may delay or skip scheduled executions; this is a cadence, not a delivery SLA.

The separate `update-sports-match-updates.yml` keeps `workflow_dispatch` but its
old five-minute schedule is commented to avoid duplicate result requests. Its prior
expression remains documented in the file. The suspended API-Football LIVE workflow
and the Cloudflare Worker are not changed or reactivated.

No paid sports APIs are selected by GENERAL. The maximum public agenda request
budget remains **6 per invocation**. Twelve windows allow at most **72 public page
requests per day** before cache savings. The result generator makes a grouped
calendar request rather than one request per fixture, normally at most twelve such
requests per day at this cadence when there are eligible matches. Its existing
separate daily safety budget and spacing guard remain in place. Manual runs, if
deliberately invoked later, still count against those guards.

A missing source or an unfinished match is not evidence of a final result.
The result engine must match the exact competition, ordered teams and ART kickoff
and require explicit final status with a valid score for informative cards. Existing
confirmed cards retain their safety rule that an explicit final closes playback
even when its score is missing; no score is manufactured. Unverifiable cards keep their
previous safe data until a later refresh. Broadcaster identity/confirmation remains
separate: a score must not create channel assignments. The agenda can stay partial.

## Football display and access

The Android presentation groups existing fixtures as Partidos de Argentina,
Amistosos, Nations League and Fútbol internacional. For Libertadores/Sudamericana,
an official Argentine participant moves the fixture to the start of the Argentina
group, before Primera División, Copa Argentina, the remaining domestic cups and
the lower divisions. Other fixtures retain separate Libertadores/Sudamericana rows.
No competition or empty category is invented by this presentation layer.

CONMEBOL's official homepage already fetched by the agenda contains team cards
with countries and crest IDs. The adapter joins those IDs to the fixture's ordered
home/away badges and optionally publishes `homeCountry` / `awayCountry` ISO2 codes.
These are schemaVersion 1 additions: old clients can ignore them, old feeds remain
valid, and unknown/conflicting countries stay omitted. Names alone do not establish
Argentina membership; missing country evidence keeps the fixture in its general
competition row. Countries never confirm a broadcaster or add channels.

Card access opens at kickoff minus **15 minutes** in ART, retaining the existing
terminal, stale and superseded-signal protections. This differs from the updater's
T-minus-10 request-eligibility gate, which does not grant playback. Scores are
currently verified for national friendlies, UEFA Nations League, Copa Argentina
and Primera Nacional; unsupported or unmatched competitions stay informative.
Ongoing informative cards remain unchanged. Final results persist for their ART
day, and missing results can be retried throughout that day at the next cadence.

Cloudflare is unnecessary for this schedule: GitHub executes GENERAL directly.
The old Worker may be retained as a paused backup. Its local `wrangler.toml` still
contains the former cron, which does not prove the deployed trigger is enabled;
redeploying that old configuration requires a deliberate review. No Worker code,
configuration, credentials or infrastructure were modified for this cadence.

For rollback, the previous GENERAL windows were 00:00, 08:00, 12:00, 17:00 and
20:00 ART (`0 3`, `0 11`, `0 15`, `0 20`, `0 23`, with the remaining cron fields
`* * *`). Restore cadence only after checking the intended request budget. The old
five-minute sidecar schedule must not be restored at the same time accidentally.
