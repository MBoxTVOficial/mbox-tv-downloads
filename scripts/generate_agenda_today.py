"""Validated agenda providers only. API-Football is never queried here."""
import argparse
import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from urllib.error import HTTPError

from . import generate_sports_today as generator
from .broadcaster_discovery import read_json, atomic_json, discover_from_root
from .competition_source_router import route, degraded_feed, football_soccer_snapshot
from .football_data_provider import FootballDataProvider, ProviderError, NoRedirects
from .lpf_official_provider import LpfOfficialProvider
from .broadcaster_sources import PublicTransport, RequestBudget
from .sports_fixture_identity import IdentityStore
from .sports_daily_broadcasts import resolve_broadcasts, apply_daily_assignments

MAX_FOOTBALL_SOCCER_API_CALLS = 40
MAX_GENERAL_PUBLIC_REQUESTS = 6


def safe_reason(error):
    """Never print exception text, credentials, URLs or response bodies."""
    allowed = {'TOKEN_NOT_AVAILABLE', 'ARGENTINA_TOKEN_NOT_AVAILABLE',
               'DAILY_BUDGET_EXHAUSTED', 'REQUEST_BUDGET_EXHAUSTED',
               'ARGENTINA_REQUEST_FAILED', 'REQUEST_FAILED', 'INVALID_RESPONSE',
               'INVALID_MATCH', 'RESPONSE_TOO_LARGE', 'CONFLICTING_DUPLICATE',
               'LPF_OFFICIAL_UNAVAILABLE', 'LPF_OFFICIAL_AMBIGUOUS', 'LPF_OFFICIAL_REQUEST_LIMIT'}
    code = str(error) if isinstance(error, ProviderError) else ''
    if code in allowed or re.fullmatch(r'HTTP_(?:401|403|404|422|429|5\d\d)', code):
        return code
    return type(error).__name__


def validated_config(root):
    config = read_json(root / 'sports_agenda_providers.json')
    evidence = read_json(root / 'tests/data/football_data_validation.json')
    if config.get('schemaVersion') != 1 or config.get('apiFootballLiveEnabled') is not False:
        raise ValueError('Invalid agenda configuration')
    expected = {'Liga Profesional Argentina': 'lg_22EEYKR', 'LaLiga': 'PD',
                'Premier League': 'PL', 'Serie A': 'SA', 'Ligue 1': 'FL1'}
    for name, item in config['competitions'].items():
        if item.get('enabled'):
            if name == 'Liga Profesional Argentina' and (item.get('provider'), item.get('competitionId'), item.get('confidence')) == ('lpf_official', 'lpf-primera', 'OFFICIAL'):
                continue
            if name not in expected or item['competitionId'] != expected[name] or item['confidence'] != 'LIMITED_AUXILIARY':
                raise ValueError('Competition not validated')
            if item.get('provider') != ('football_soccer_argentina' if name == 'Liga Profesional Argentina' else 'football_data'):
                raise ValueError('Provider not validated for competition')
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


def acquire(root, day, config, clients=None, diagnostics=None, public_transport=None):
    clients = clients or {}
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.setdefault('sources', [])
    diagnostics.setdefault('requests', {})
    rows = []
    fd = clients.get('football_data')
    unavailable = {}
    for name, item in config['competitions'].items():
        if not item['enabled'] or not config['providers'][item['provider']]['enabled']:
            continue
        provider = item['provider']
        try:
            if provider in unavailable:
                raise ProviderError(unavailable[provider])
            if provider == 'lpf_official':
                if provider in clients:
                    received = clients[provider].matches(item['competitionId'], day)
                else:
                    transport = public_transport or RequestBudget(PublicTransport(8), MAX_GENERAL_PUBLIC_REQUESTS)
                    before = transport.requests
                    try:
                        received = LpfOfficialProvider(transport=transport).matches(item['competitionId'], day)
                    finally:
                        diagnostics['requests']['lpf_official'] = transport.requests - before
            elif provider == 'football_data':
                if fd is None:
                    fd = FootballDataProvider(os.environ.get('FOOTBALL_DATA_TOKEN', ''))
                    original = fd._fetch
                    def bounded_fetch(path):
                        reserve(root, 'football_data', datetime.now(timezone.utc).date().isoformat(), 32)
                        diagnostics['requests']['football_data'] = diagnostics['requests'].get('football_data', 0) + 1
                        return original(path)
                    fd._fetch = bounded_fetch
                received = fd.matches(item['competitionId'], day)
            elif provider in clients:
                received = clients[provider].matches(item['competitionId'], day)
            else:
                token = os.environ.get('FOOTBALL_SOCCER_API_KEY', '')
                if not token.strip():
                    raise ProviderError('ARGENTINA_TOKEN_NOT_AVAILABLE')
                reserve(root, provider, datetime.now(timezone.utc).date().isoformat(), MAX_FOOTBALL_SOCCER_API_CALLS)
                diagnostics['requests'][provider] = diagnostics['requests'].get(provider, 0) + 1
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
                    received = football_soccer_snapshot(payload['data'])
                except HTTPError as error:
                    raise ProviderError(f'HTTP_{error.code}') from None
                except Exception:
                    raise ProviderError('ARGENTINA_REQUEST_FAILED') from None
            # A provider cannot introduce another competition through its response.
            if any(r.get('competition') != name or r.get('competitionId') != item['competitionId'] for r in received):
                raise ProviderError('INVALID_RESPONSE')
            rows.extend(received)
            diagnostics['sources'].append({'provider': provider, 'competition': name,
                                           'state': 'OK', 'fixtures': len(received)})
        except (ProviderError, ValueError, KeyError, TypeError) as error:
            reason = safe_reason(error)
            if reason in {'TOKEN_NOT_AVAILABLE', 'ARGENTINA_TOKEN_NOT_AVAILABLE', 'HTTP_401', 'HTTP_429', 'DAILY_BUDGET_EXHAUSTED', 'REQUEST_BUDGET_EXHAUSTED'}:
                unavailable[provider] = reason
            diagnostics['sources'].append({'provider': provider, 'competition': name,
                                           'state': 'ERROR', 'reason': reason, 'fixtures': 0})
    return rows


def generate_agenda(root, output, day, now, provider_rows=None, official_rows=(), discover=False, clients=None):
    config = validated_config(root)
    previous = generator.previous_feed(output)
    diagnostics = {'sources': [], 'requests': {}}
    public_transport = RequestBudget(PublicTransport(8), MAX_GENERAL_PUBLIC_REQUESTS)
    stage = 'ACQUIRE'
    try:
        rows = acquire(root, day, config, clients, diagnostics=diagnostics, public_transport=public_transport) if provider_rows is None else provider_rows
        stage = 'NORMALIZE'
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
            # Keep real same-day cards on coverage loss; an old month's file is
            # not today's last-good agenda. A successful source can establish an
            # empty list of available fixtures without inventing missing matches.
            previous_today = previous and previous['date'] == day.isoformat() and any(s['events'] for s in previous['sections'])
            source_available = any(s['state'] == 'OK' for s in diagnostics['sources'])
            reason = 'PROVIDERS_UNAVAILABLE' if any(s['state'] == 'ERROR' for s in diagnostics['sources']) else 'EMPTY_AGENDA'
            if previous_today or not source_available:
                return previous, {'state': 'KEEP_LAST_GOOD', 'reason': reason, 'conflicts': conflicts, **diagnostics}, False
        if previous and previous['date'] == day.isoformat():
            new_ids = {e['id'] for e in events}
            for section in previous['sections']:
                for old in section['events']:
                    if old.get('channels') and old['id'] not in new_ids and old['status'] in ('SCHEDULED', 'LIVE'):
                        return previous, {'state': 'KEEP_LAST_GOOD', 'reason': 'CONFIRMED_IDENTITY_RECONCILIATION_REQUIRED', **diagnostics}, False
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
        stage = 'BROADCASTS'
        daily, groups = generator.daily_context(day)
        ids = {int(e['id'].removeprefix('fixture-')) for e in events}
        scoped = {**daily, 'broadcasts': [b for b in daily['broadcasts'] if b['fixtureId'] in ids - moved]} if daily else None
        remaining_public = MAX_GENERAL_PUBLIC_REQUESTS - public_transport.requests
        if discover and remaining_public > 0:
            safe = {k: v for k, v in feed.items() if k != 'sportsDataMode'}
            try:
                scoped, cache, report, groups = discover_from_root(safe, root, previous=scoped, now=now,
                    request_limit=remaining_public, transport=public_transport)
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
        stage = 'VALIDATE'
        generator.validate_feed(feed)
        stage = 'WRITE'
        atomic_json(root / 'sports_fixture_identity.json', identity.value)
        changed = generator.write_feed_atomic(feed, output)
        if scoped and discover:
            atomic_json(root / 'sports_broadcasts_today.json', scoped)
        return feed, {'state': 'DEGRADED', 'conflicts': conflicts, 'fixtures': normalized, **diagnostics}, changed
    except (ProviderError, ValueError, KeyError, generator.GenerationError) as error:
        return previous, {'state': 'KEEP_LAST_GOOD', 'reason': safe_reason(error), 'stage': stage, **diagnostics}, False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--discover-broadcasts', action='store_true')
    parser.add_argument('--official-sites', action='store_true', help='Official sites and Argentina guides; no sports API requests.')
    args = parser.parse_args(argv)
    root = generator.ROOT
    now = datetime.now(generator.argentina_timezone())
    if args.official_sites:
        from .generate_official_agenda import generate_official_agenda
        feed, report, changed = generate_official_agenda(root, args.output or generator.REAL_OUTPUT, now.date(), now)
    else:
        feed, report, changed = generate_agenda(root, args.output or generator.REAL_OUTPUT, now.date(), now, discover=args.discover_broadcasts)
    available = feed.get('date', 'none') if feed else 'none'
    events = sum(len(s.get('events', [])) for s in (feed or {}).get('sections', []))
    # Only whitelisted codes and fixed config names enter diagnostics/annotations.
    for source in report.get('sources', []):
        print('SPORTS_AGENDA SOURCE ' + json.dumps(source, ensure_ascii=True))
        if source['state'] in {'ERROR', 'ERROR_USING_CACHE', 'REVIEW'}:
            print('::warning title=Sports agenda source::provider=' + source.get('provider', source.get('source', 'unknown')) +
                  ' reason=' + source.get('reason', source['state']))
    for guide in report.get('guides', []):
        print('SPORTS_AGENDA GUIDE ' + json.dumps(guide, ensure_ascii=True))
    if args.official_sites:
        print('SPORTS_AGENDA PUBLIC requests=' + str(report.get('publicRequests', 0)) +
              ' sportsApiRequests=' + str(report.get('sportsApiRequests', 0)))
    print('SPORTS_AGENDA state=' + report['state'] + ' changed=' + str(changed).lower() +
          ' requestedDate=' + now.date().isoformat() + ' availableDate=' + available +
          ' reason=' + report.get('reason', 'OK') + ' stage=' + report.get('stage', 'COMPLETE') +
          ' events=' + str(events) + ' requests=' + json.dumps(report.get('requests', {}), sort_keys=True))
    if available != now.date().isoformat():
        print('::error title=Sports agenda unavailable::requestedDate=' + now.date().isoformat() +
              ' availableDate=' + available + ' reason=' + report.get('reason', 'NO_CURRENT_FEED'))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
