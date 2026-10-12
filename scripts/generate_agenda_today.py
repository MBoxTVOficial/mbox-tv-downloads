"""Validated agenda providers only. API-Football is never queried here."""
import argparse
import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from . import generate_sports_today as generator
from .broadcaster_discovery import read_json, atomic_json, discover_from_root
from .competition_source_router import route, degraded_feed, football_soccer_snapshot
from .football_data_provider import FootballDataProvider, ProviderError, NoRedirects
from .sports_fixture_identity import IdentityStore
from .sports_daily_broadcasts import resolve_broadcasts, apply_daily_assignments

MAX_FOOTBALL_SOCCER_API_CALLS = 40


def validated_config(root):
    config = read_json(root / 'sports_agenda_providers.json')
    evidence = read_json(root / 'tests/data/football_data_validation.json')
    if config.get('schemaVersion') != 1 or config.get('apiFootballLiveEnabled') is not False:
        raise ValueError('Invalid agenda configuration')
    expected = {'Liga Profesional Argentina': 'lg_22EEYKR', 'LaLiga': 'PD',
                'Premier League': 'PL', 'Serie A': 'SA', 'Ligue 1': 'FL1'}
    for name, item in config['competitions'].items():
        if item.get('enabled'):
            if name not in expected or item['competitionId'] != expected[name] or item['confidence'] != 'LIMITED_AUXILIARY':
                raise ValueError('Competition not validated')
            if name == 'Liga Profesional Argentina':
                gate = read_json(root / 'tests/data/lpf_agenda_validation.json')
                if gate.get('exactMatches', 0) < 3 or gate.get('competitionId') != item['competitionId']:
                    raise ValueError('LPF validation gate failed')
            elif evidence['competitions'][item['competitionId']]['exact'] < 5:
                raise ValueError('European validation gate failed')
    return config


def reserve(root, provider, day, limit):
    path = root / 'sports_agenda_request_budget.json'
    state = read_json(path, optional=True) or {'schemaVersion': 1, 'dates': {}}
    counts = state['dates'].setdefault(day, {})
    if counts.get(provider, 0) >= limit:
        raise ProviderError('DAILY_BUDGET_EXHAUSTED')
    counts[provider] = counts.get(provider, 0) + 1
    state['dates'] = {k: v for k, v in state['dates'].items() if k >= day}
    atomic_json(path, state)  # Reserve before request; failed requests count too.


def acquire(root, day, config, clients=None):
    clients = clients or {}
    rows = []
    fd = clients.get('football_data')
    if fd is None and any(c['enabled'] and c['provider'] == 'football_data' for c in config['competitions'].values()):
        fd = FootballDataProvider(os.environ.get('FOOTBALL_DATA_TOKEN', ''))
        original = fd._fetch
        def bounded_fetch(path):
            reserve(root, 'football_data', datetime.now(timezone.utc).date().isoformat(), 32)
            return original(path)
        fd._fetch = bounded_fetch
    for name, item in config['competitions'].items():
        if not item['enabled'] or not config['providers'][item['provider']]['enabled']:
            continue
        if item['provider'] == 'football_data':
            rows.extend(fd.matches(item['competitionId'], day))
        elif item['provider'] in clients:
            rows.extend(clients[item['provider']].matches(item['competitionId'], day))
        else:
            token = os.environ.get('FOOTBALL_SOCCER_API_KEY', '')
            if not token:
                raise ProviderError('ARGENTINA_TOKEN_NOT_AVAILABLE')
            reserve(root, 'football_soccer_argentina', datetime.now(timezone.utc).date().isoformat(), MAX_FOOTBALL_SOCCER_API_CALLS)
            query = urlencode({'league_id': item['competitionId'], 'date_from': day.isoformat(),
                               'date_to': (day + timedelta(days=1)).isoformat(), 'limit': 1000})
            request = Request('https://api.footballsoccerapi.com/v1/matches?' + query,
                              headers={'X-API-Key': token, 'Accept': 'application/json'})
            try:
                with build_opener(NoRedirects()).open(request, timeout=8) as response:
                    raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise ValueError()
                payload = json.loads(raw)
                meta = payload.get('meta', {})
                if not isinstance(payload.get('data'), list) or len(payload['data']) >= 1000 or meta.get('next_page') or meta.get('next_cursor') or meta.get('total_pages', 1) != 1:
                    raise ValueError()
                rows.extend(football_soccer_snapshot(payload['data']))
            except Exception:
                raise ProviderError('ARGENTINA_REQUEST_FAILED') from None
    return rows


def generate_agenda(root, output, day, now, provider_rows=None, official_rows=(), discover=False, clients=None):
    config = validated_config(root)
    previous = generator.previous_feed(output)
    try:
        rows = acquire(root, day, config, clients) if provider_rows is None else provider_rows
        allowed = {name for name, entry in config['competitions'].items() if entry['enabled'] and config['providers'][entry['provider']]['enabled']}
        rows = [r for r in rows if r['competition'] in allowed]
        normalized, conflicts = route(day, rows, [r for r in official_rows if r['competition'] in allowed])
        identity = IdentityStore(read_json(root / 'sports_fixture_identity.json', optional=True))
        for row in normalized:
            provider = row['source'] if row['source'] != 'official' else config['competitions'][row['competition']]['provider']
            if row['providerFixtureId'] is None:
                raise ValueError('Official fixture needs explicit provider association')
            row['fixtureId'] = identity.resolve(provider, row['providerFixtureId'])
            row['mboxFixtureId'] = row['fixtureId']
            row['provider'] = provider
        feed = degraded_feed(day, now, normalized)
        events = [e for s in feed['sections'] for e in s['events']]
        if not events:
            return previous, {'state': 'KEEP_LAST_GOOD', 'reason': 'EMPTY_AGENDA', 'conflicts': conflicts}, False
        if previous and previous['date'] == day.isoformat():
            new_ids = {e['id'] for e in events}
            for section in previous['sections']:
                for old in section['events']:
                    if old.get('channels') and old['id'] not in new_ids and old['status'] in ('SCHEDULED', 'LIVE'):
                        return previous, {'state': 'KEEP_LAST_GOOD', 'reason': 'CONFIRMED_IDENTITY_RECONCILIATION_REQUIRED'}, False
        prior_events = {e['id']: e for s in (previous or {}).get('sections', []) for e in s['events']} if previous and previous['date'] == day.isoformat() else {}
        moved = set()
        for event in events:
            old = prior_events.get(event['id'])
            if old and old['startTime'] != event['startTime']:
                identifier = int(event['id'].removeprefix('fixture-'))
                before = f"{day.isoformat()}T{old['startTime']}:00-03:00"
                after = f"{day.isoformat()}T{event['startTime']}:00-03:00"
                if not identity.kickoff_reconciled(identifier, before, after):
                    moved.add(identifier)
        # Never transfer old API-Football assignments to new identities.
        daily, groups = generator.daily_context(day)
        ids = {int(e['id'].removeprefix('fixture-')) for e in events}
        scoped = {**daily, 'broadcasts': [b for b in daily['broadcasts'] if b['fixtureId'] in ids - moved]} if daily else None
        if discover:
            safe = {k: v for k, v in feed.items() if k != 'sportsDataMode'}
            try:
                scoped, cache, report, groups = discover_from_root(safe, root, previous=scoped, now=now)
                atomic_json(root / 'sports_broadcast_discovery_cache.json', cache)
                atomic_json(root / 'sports_broadcast_discovery_report.json', report)
            except Exception:
                pass  # Scoped confirmed evidence remains available on source failure.
        if scoped:
            apply_daily_assignments(feed, resolve_broadcasts(scoped, feed, groups, generator.normalize))
        if previous and previous['date'] == day.isoformat():
            old = {e['id']: e for s in previous['sections'] for e in s['events']}
            for event in events:
                prior = old.get(event['id'], {})
                # Exact identity AND kickoff required; reprogramming invalidates channels.
                if not event.get('channels') and prior.get('channels') and all(event[k] == prior.get(k) for k in ('competition', 'homeTeam', 'awayTeam', 'startTime')):
                    event['channels'] = copy.deepcopy(prior['channels'])
        generator.apply_playback_policy(feed, now, generator.argentina_timezone(), groups)
        generator.validate_feed(feed)
        atomic_json(root / 'sports_fixture_identity.json', identity.value)
        changed = generator.write_feed_atomic(feed, output)
        if scoped and discover:
            atomic_json(root / 'sports_broadcasts_today.json', scoped)
        return feed, {'state': 'DEGRADED', 'conflicts': conflicts, 'fixtures': normalized}, changed
    except (ProviderError, ValueError, KeyError, generator.GenerationError) as error:
        return previous, {'state': 'KEEP_LAST_GOOD', 'reason': type(error).__name__}, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--discover-broadcasts', action='store_true')
    args = parser.parse_args()
    root = generator.ROOT
    now = datetime.now(generator.argentina_timezone())
    feed, report, changed = generate_agenda(root, args.output or generator.REAL_OUTPUT, now.date(), now, discover=args.discover_broadcasts)
    print('SPORTS_AGENDA state=' + report['state'] + ' changed=' + str(changed).lower())
    return 0 if feed is not None else 1


if __name__ == '__main__':
    raise SystemExit(main())
