# Separate football result refresh cadences

The public agenda and result overlay have independent responsibilities. Neither result
workflow discovers channels, rewrites agenda fixtures, makes IPTV requests or uses
sports account keys. Both writers share `sports-today-main` concurrency and check out
current `main` after acquiring that group, so they serialize publication.

## Informative cards: every two hours

GENERAL (`update-sports-today.yml`) generates the agenda and then executes
`python -m scripts.generate_sports_match_updates --scope informative`.
Its ART windows are 00:00, 02:00, 04:00, 06:00, 08:00, 10:00, 12:00, 14:00, 16:00,
18:00, 20:00 and 22:00. The UTC cron is `0 1-23/2 * * *`.

Only cards without a confirmed usable signal enter this observation scope. They
receive `FINISHED` and a score only when a source explicitly reports a final with
both valid scores and the exact competition, ordered teams and ART kickoff match.
An ongoing, missing, ambiguous or contradictory observation leaves the card unchanged.
Existing confirmed-card entries in the shared overlay must be preserved.

## Confirmed-channel cards: every five minutes

The public `update-sports-match-updates.yml` executes
`python -m scripts.generate_sports_match_updates --scope confirmed` with cron
`*/5 * * * *`. It refreshes LIVE, score and FINISHED observations for confirmed-channel
fixtures only. It does not run agenda discovery or the whole backend test suite on
every tick; its offline checks focus on match-state handling.

The generator avoids source requests when no supported confirmed fixtures are pending
in the active match window. It preserves informative results in the same overlay and
uses the persisted request guards. Explicit finals stay final; source failures retain
the last-good observation without extending its freshness. A scheduling tick is not
an instruction to reopen a final, manufacture a score or assign a new channel.

GitHub may delay or skip scheduled executions. Five minutes is the configured cadence,
not a guarantee of real-time delivery. The LIVE freshness timeout remains independent
of polling: a failed/delayed request cannot keep an old LIVE label fresh indefinitely.

## Sources, identity and limits

Results are reconciled separately from broadcaster evidence. Public result adapters
must validate their actual page/data shape, source status and exact fixture identity;
unsupported pages or competitions are not treated as verified by their existence.
Using the same underlying data through two pages does not create independent evidence.
Network attempts are grouped and bounded, rather than one request per match. Failed
requests count against persisted request budgets too.

The initial enabled providers are Olé's public Opta calendar (also the data source
behind Olé Estadísticas), Canchallena and TyC Sports. Olé and Canchallena are limited
to the verified competition mappings for national friendlies, UEFA Nations League,
Copa Argentina and Primera Nacional. The verified TyC adapter covers Primera Nacional.
A source is queried only if it supports a competition with a pending fixture in the
selected scope; this does not promise results for every card or every competition.

GENERAL's existing public agenda budget remains six page requests per invocation,
or at most 72 across its twelve daily windows before cache savings. Result acquisition
has separate persisted scope guards: at most 288 confirmed-channel cycles and 12
informative cycles per ART day, with one grouped request to each selected known
provider in a cycle. With all three providers selected in every possible cycle, the
theoretical result ceiling is 900 public requests/day (300 cycles times three), plus
the agenda ceiling of 72. Actual consumption is lower when no match is pending, a
provider lacks competition coverage or a final is already settled. Source failures
consume a reserved attempt too; repeating the same slot does not create more work.
These limits do not borrow from or reactivate API-Football, football-data or
FootballSoccer account limits.

During migration, the existing same-day result ledger is retained in full. Its
previous attempts still count against the Olé provider ceiling, its last slot and
four-minute spacing guard remain effective, and settled final identities are kept.
Upgrading the ledger does not reset consumption or permit an immediate duplicate
query. A new ART day resets only the corresponding daily counters.

Neither workflow reads Google search results. Cross-source conflicts must keep the
last-good result, rather than choosing an arbitrary score. Scores never create a
broadcaster assignment or a channel mapping.

## Football display and access

The existing Android presentation groups fixtures as Partidos de Argentina, Amistosos,
Nations League and Fútbol internacional. Officially identified Argentine participants
in Libertadores/Sudamericana go first in Partidos de Argentina, then Primera División,
Copa Argentina, remaining national cups and the lower divisions. Other Libertadores
and Sudamericana fixtures retain separate competition rows. Unknown countries are
not guessed from club names.

Card access opens at kickoff minus 15 minutes in ART, retaining terminal, stale and
superseded-signal protections. Result acquisition does not change that playback window.
No APK, player, TV EN VIVO or focus behavior is modified by this scheduling split.

## Legacy infrastructure and rollback

The API-Football `update-sports-live.yml` automatic schedule remains paused. Cloudflare
code/configuration and its deployed trigger were not modified or reactivated.

To pause frequent public results, comment only the `schedule` block in
`update-sports-match-updates.yml`; GENERAL can continue publishing agenda and informative
finals every two hours. To restore the former shared result behavior deliberately,
replace GENERAL's scope with `all` and first pause the separate confirmed workflow.
Do not enable both an all-card cadence and an overlapping confirmed-card cadence by
accident. Never restore legacy API or Cloudflare schedules as part of this rollback.
