"""Offline phase-one router. No network, credentials or automatic activation."""
import copy
import hashlib
from datetime import datetime, timezone
from .sports_auxiliary_policy import local_start
from . import generate_sports_today as generator

POLICIES = {
    'Liga Profesional Argentina': ('lg_22EEYKR', 'LIMITED_AUXILIARY', 'football_soccer_argentina'),
    'LaLiga': ('PD', 'LIMITED_AUXILIARY', 'football_data'),
    'Premier League': ('PL', 'LIMITED_AUXILIARY', 'football_data'),
    'Serie A': ('SA', 'LIMITED_AUXILIARY', 'football_data'),
    'Ligue 1': ('FL1', 'LIMITED_AUXILIARY', 'football_data'),
    'UEFA Champions League': ('CL', 'UNVALIDATED', 'football_data'),
}
AUTHORITY = {'official': 1, 'official_broadcaster': 2, 'api_football': 3,
             'auxiliary': 4, 'editorial': 5}


class PrimaryUnavailable(Exception):
    """Explicit availability failure, not a catch-all for programmer/config errors."""


def football_soccer_snapshot(rows):
    """Whitelist fields from a cached response; never request anything here."""
    result = []
    for raw in rows:
        policy = next(((name, item) for name, item in POLICIES.items()
                       if item[0] == raw.get('league_id')), None)
        if policy is None:
            continue
        kickoff = raw.get('kickoff_utc')
        if type(kickoff) in (int, float):
            kickoff = datetime.fromtimestamp(kickoff, timezone.utc).isoformat()
        result.append({'competition': policy[0], 'competitionId': policy[1][0],
                       'providerFixtureId': raw.get('match_id'),
                       'homeTeam': raw.get('home_team_name'), 'awayTeam': raw.get('away_team_name'),
                       'startTime': local_start(kickoff).isoformat()})
    return result


def team_key(name, aliases):
    key = generator.normalize(name)
    targets = {generator.normalize(canonical) for canonical, names in aliases.items()
               if key in {generator.normalize(n) for n in [canonical] + names}}
    if len(targets) > 1:
        raise ValueError('Ambiguous team alias')
    return next(iter(targets), key)


def normalize_row(row, competition, day, source, confidence, aliases=None):
    aliases = aliases or {}
    start = local_start(row['startTime'])
    if start.date() != day:
        return None
    home, away = row['homeTeam'], row['awayTeam']
    if not all(isinstance(x, str) and x.strip() for x in (home, away)):
        raise ValueError('Invalid team')
    identity = '|'.join((competition, team_key(home, aliases), team_key(away, aliases), start.isoformat()))
    # Stable internal namespace, never presented as an API-Football fixture ID.
    identifier = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:6], 'big') + 10**15
    return {'fixtureId': identifier, 'providerFixtureId': None, 'competition': competition,
            'homeTeam': home, 'awayTeam': away, 'startTime': start.isoformat(),
            'timezone': 'America/Argentina/Buenos_Aires', 'source': source,
            'sourceConfidence': confidence, 'provenance': [{'source': source}],
            '_identity': identity}


def route(day, provider_rows=(), official_rows=(), aliases=None):
    """Official rows define covered competitions. Missing provider rows never erase them.

    Input rows are normalized snapshots, not raw response dumps. An official
    fixture's optional integer fixtureId remains authoritative when present.
    """
    aliases = aliases or {}
    result, conflicts = {}, []
    covered = {r['competition'] for r in official_rows}
    for raw in official_rows:
        competition = raw['competition']
        if competition not in POLICIES:
            continue  # Phase one never widens the competition scope.
        item = normalize_row(raw, competition, day, 'official', 'OFFICIAL', aliases)
        if item:
            if type(raw.get('fixtureId')) is int and raw['fixtureId'] > 0:
                item['fixtureId'] = raw['fixtureId']
            result[item['_identity']] = item
    for raw in provider_rows:
        competition = raw.get('competition')
        policy = POLICIES.get(competition)
        if not policy or policy[1] not in ('TRUSTED_AUXILIARY', 'LIMITED_AUXILIARY'):
            continue
        if raw.get('competitionId') != policy[0]:
            continue
        item = normalize_row(raw, competition, day, policy[2], policy[1], aliases)
        if not item:
            continue
        provider_id = raw.get('providerFixtureId')
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError('Invalid providerFixtureId')
        official = result.get(item['_identity'])
        if official and official['source'] == 'official':
            official['providerFixtureId'] = provider_id
            official['provenance'].append({'source': policy[2], 'providerFixtureId': provider_id,
                                          'match': 'EXACT', 'toleranceMinutes': 0})
        elif competition in covered:
            # Full-name aliases, both teams and orientation; no fuzzy identity.
            same_teams = [r for r in result.values() if r['competition'] == competition
                          and team_key(r['homeTeam'], aliases) == team_key(item['homeTeam'], aliases)
                          and team_key(r['awayTeam'], aliases) == team_key(item['awayTeam'], aliases)]
            conflicts.append({'providerFixtureId': provider_id, 'competition': competition,
                              'reason': 'KICKOFF_CONFLICT' if same_teams else 'NOT_IN_OFFICIAL_SNAPSHOT'})
        else:
            item['providerFixtureId'] = provider_id
            item['provenance'][0]['providerFixtureId'] = provider_id
            result.setdefault(item['_identity'], item)
    rows = sorted(result.values(), key=lambda r: (r['startTime'], r['fixtureId']))
    for row in rows:
        row.pop('_identity')
    ids = [r['fixtureId'] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError('Internal fixture identity collision')
    return rows, conflicts


def degraded_feed(day, now, rows, discover=None):
    """Future agenda only: SCHEDULED is a UI projection, never provider status.

    Past kickoffs are omitted because their actual state is unknown. Existing
    broadcaster discovery may be injected, and still requires its own evidence.
    Auxiliary fields never supply scores, logos, states, venues or channels.
    """
    now = local_start(now)
    payload = {'errors': [], 'response': []}
    countries = {'Liga Profesional Argentina': 'Argentina', 'LaLiga': 'Spain',
                 'Premier League': 'England', 'Serie A': 'Italy', 'Ligue 1': 'France'}
    for row in rows:
        start = local_start(row['startTime'])
        if start.date() != day or start < now:
            continue
        payload['response'].append({'fixture': {'id': row['fixtureId'], 'date': start.isoformat(),
                                               'status': {'short': 'NS'}},
            'league': {'name': 'La Liga' if row['competition'] == 'LaLiga' else row['competition'],
                       'country': countries.get(row['competition'], '')},
            'teams': {'home': {'name': row['homeTeam']}, 'away': {'name': row['awayTeam']}}})
    payload['results'] = len(payload['response'])
    feed, _ = generator.build_feed(payload, day, now)
    original = {r['fixtureId']: r['competition'] for r in rows}
    for section in feed['sections']:
        for event in section['events']:
            event['competition'] = original[int(event['id'].removeprefix('fixture-'))]
    # Existing fixture/competition channel rules are not proof for a fallback ID.
    for section in feed['sections']:
        for event in section['events']:
            event.pop('channels', None)
    if discover is not None:
        feed = discover(copy.deepcopy(feed))
    generator.validate_feed(feed)
    feed['sportsDataMode'] = 'DEGRADED'
    return feed


def generate_with_fallback(day, now, primary, provider_rows=(), official_rows=(), aliases=None, discover=None):
    """Primary callable uses the existing pipeline; fallback only on explicit outage.

    Never connected to scheduled workflows in phase one.
    """
    try:
        feed = primary()
        generator.validate_feed(feed)
        return {**feed, 'sportsDataMode': 'NORMAL'}, {'conflicts': [], 'fixtures': []}
    except PrimaryUnavailable:
        rows, conflicts = route(day, provider_rows, official_rows, aliases)
        return degraded_feed(day, now, rows, discover), {'conflicts': conflicts, 'fixtures': rows}


if __name__ == '__main__':
    import argparse
    import json
    from datetime import date
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Offline multi-provider preview; no network.')
    parser.add_argument('--snapshot', type=Path, required=True,
                        help='JSON: providerRows, officialRows and optional teamAliases')
    parser.add_argument('--date', type=date.fromisoformat, required=True)
    parser.add_argument('--now', required=True, help='ISO timestamp with explicit offset')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == generator.REAL_OUTPUT.resolve():
        parser.error('Preview must not overwrite sports_today.json')
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    rows, conflicts = route(args.date, snapshot.get('providerRows', []),
                           snapshot.get('officialRows', []), snapshot.get('teamAliases', {}))
    feed = degraded_feed(args.date, args.now, rows)
    generator.validate_feed(feed)
    generator.write_feed_atomic(feed, args.output)
    print(json.dumps({'sportsDataMode': 'DEGRADED', 'fixtures': len(rows),
                      'conflicts': conflicts, 'providerRequests': 0}, ensure_ascii=False))
