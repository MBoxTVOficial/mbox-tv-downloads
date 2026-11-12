"""Public agenda builder and isolated preview CLI. GENERAL writes through its validated wrapper."""
import argparse
import copy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from . import generate_sports_today as generator
from .broadcaster_sources import DiscoveryError, Evidence, Fixture, PublicTransport, RequestBudget, ZONE, instant, safe_text
from .broadcaster_discovery import atomic_json, confidence, read_json
from .lpf_expanded_source import article_links, parse_lpf_fixtures
from .sports_channel_rules import load_clean_groups
from .sports_daily_broadcasts import apply_daily_assignments, resolve_broadcasts, validate_broadcasts
from .sports_fixture_identity import IdentityStore
from .sports_playback_policy import apply_playback_policy

REGISTRY = 'sports_official_sources.json'
HOSTS = {'lpf_official': 'www.ligaprofesional.ar', 'afa_official': 'www.afa.com.ar',
         'conmebol_official': 'gol.conmebol.com'}


def validate_registry(config):
    """Strictly local prototype registry; a config edit cannot enable an API or new league."""
    if not isinstance(config, dict) or config.get('schemaVersion') != 1 or config.get('mode') != 'LOCAL_PREVIEW_ONLY':
        raise ValueError('Invalid official-bot registry')
    if config.get('limits') != {'maxRequests': 6, 'timeoutSeconds': 8, 'cacheHours': 6}:
        raise ValueError('Official-bot limits must remain 6 GETs, 8 seconds, 6 hour cache')
    expected = {
        'lpf-primera': ('Liga Profesional Argentina', 'lpf_official', 'argentina', 'lpf', 'https://www.ligaprofesional.ar/categoria/primera/', 2, 3),
        'lpf-proyeccion': ('Torneo Proyección', 'lpf_official', 'argentina', 'lpf', 'https://www.ligaprofesional.ar/categoria/proyeccion/', 1, 2),
        'primera-nacional': ('Primera Nacional', 'afa_official', 'argentina', 'afa', 'https://www.afa.com.ar/es/', 1, 2),
        'libertadores': ('CONMEBOL Libertadores', 'conmebol_official', 'conmebol', 'conmebol', 'https://gol.conmebol.com/libertadores/es', 0, 1),
        'sudamericana': ('CONMEBOL Sudamericana', 'conmebol_official', 'conmebol', 'conmebol', 'https://gol.conmebol.com/sudamericana/es', 0, 1),
    }
    fields = ('competition', 'provider', 'section', 'adapter', 'url', 'articles', 'requestCeiling')
    sources = config.get('sources')
    if not isinstance(sources, list) or len(sources) != len(expected) or \
            {source.get('id') for source in sources if isinstance(source, dict)} != set(expected):
        raise ValueError('Invalid official-bot sources')
    for source in sources:
        if set(source) != {'id', *fields} or tuple(source.get(field) for field in fields) != expected[source['id']]:
            raise ValueError('Unsupported official source')
    return config


class PublicPages:
    """One attempt per URL per invocation, including failures. No retries or redirects."""
    def __init__(self, budget):
        self.budget, self.pages, self.failed = budget, {}, set()

    def fetch(self, url):
        if url in self.failed:
            raise DiscoveryError('PUBLIC_PAGE_FAILED')
        if url not in self.pages:
            try:
                self.pages[url] = self.budget.fetch(url)
            except (DiscoveryError, OSError, TimeoutError, UnicodeError):
                self.failed.add(url)
                raise
        return self.pages[url]


def source_rows(source, day, pages):
    html = pages.fetch(source['url'])
    if source['adapter'] == 'lpf':
        links = article_links(html, source['competition'], source['url'], day)[:source['articles']]
        if not links:
            raise DiscoveryError('OFFICIAL_AGENDA_NOT_FOUND')
        result, recognized = [], 0
        for link in links:
            try:
                result.extend(parse_lpf_fixtures(pages.fetch(link), source['competition'], link, day))
                recognized += 1
            except (DiscoveryError, OSError, TimeoutError, UnicodeError):
                continue
        if not recognized:
            raise DiscoveryError('OFFICIAL_AGENDA_UNRECOGNIZED')
        return result
    if source['adapter'] == 'conmebol':
        from .conmebol_official_source import parse_conmebol_fixtures
        return parse_conmebol_fixtures(html, source['competition'], source['url'])
    if source['adapter'] == 'afa':
        from .afa_official_source import afa_article_links, parse_afa_fixtures
        links = afa_article_links(html, source['url'])[:source['articles']]
        if not links:
            raise DiscoveryError('OFFICIAL_AGENDA_NOT_FOUND')
        return parse_afa_fixtures(pages.fetch(links[0]), source['competition'], links[0])
    raise DiscoveryError('OFFICIAL_ADAPTER_UNSUPPORTED')


def checked_rows(rows, source, day):
    """Source adapters/cache cannot inject score/state/channel fields into preview."""
    result = {}
    for raw in rows:
        if not isinstance(raw, dict) or raw.get('competition') != source['competition'] or \
                raw.get('competitionId') != source['id'] or raw.get('source') != source['provider'] or \
                not all(safe_text(raw.get(key)) for key in ('homeTeam', 'awayTeam', 'providerFixtureId')):
            raise DiscoveryError('OFFICIAL_ROW_INVALID')
        start = raw.get('startTime')
        start = instant(start.isoformat() if isinstance(start, datetime) else start)
        if not day <= start.date() <= day + timedelta(days=31):
            continue
        url = raw.get('sourceUrl')
        from .broadcaster_sources import public_url
        public_url(url)
        hosts = ('www.futbolenvivoargentina.com', 'www.ole.com.ar', 'www.tycsports.com') \
            if source['provider'] == 'argentina_agendas' else (HOSTS[source['provider']],)
        if urlsplit(url).hostname not in hosts:
            raise DiscoveryError('OFFICIAL_ROW_SOURCE_INVALID')
        broadcasters = raw.get('broadcasters', [])
        if not isinstance(broadcasters, list) or not all(safe_text(name, 100) for name in broadcasters):
            raise DiscoveryError('OFFICIAL_BROADCAST_INVALID')
        row = {key: raw[key] for key in ('providerFixtureId', 'competition', 'competitionId',
                                       'homeTeam', 'awayTeam', 'source', 'sourceUrl')}
        row.update(startTime=start.isoformat(), broadcasters=list(dict.fromkeys(broadcasters)))
        previous = result.get(row['providerFixtureId'])
        if previous and any(previous[key] != row[key] for key in ('homeTeam', 'awayTeam', 'startTime')):
            raise DiscoveryError('OFFICIAL_FIXTURE_CONFLICT')
        if previous:
            if previous['broadcasters'] and row['broadcasters'] and set(previous['broadcasters']) != set(row['broadcasters']):
                raise DiscoveryError('OFFICIAL_FIXTURE_CONFLICT')
            row['broadcasters'] = list(dict.fromkeys(previous['broadcasters'] + row['broadcasters']))
        result[row['providerFixtureId']] = row
    return sorted(result.values(), key=lambda row: (row['startTime'], row['providerFixtureId']))


def acquire(config, day, now, cache=None, transport=None, offline=False, budget=None):
    config = validate_registry(config)
    now = instant(now.isoformat())
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    # Discovery cache is date-scoped. Yesterday's assignments never become today's.
    same_registry = isinstance(cache, dict) and cache.get('registryHash') == fingerprint
    previous = cache if same_registry and cache.get('date') == day.isoformat() else {}
    entries = copy.deepcopy(previous.get('sources', {}))
    if not previous and same_registry and isinstance(cache.get('sources'), dict):
        # Carry scheduling metadata only, never yesterday's fixtures/assignments.
        entries = {key: {'attemptedAt': entry['attemptedAt']} for key, entry in cache['sources'].items()
                   if isinstance(entry, dict) and isinstance(entry.get('attemptedAt'), str)}
    if not isinstance(entries, dict):
        entries = {}
    entries = {key: value for key, value in entries.items() if isinstance(key, str) and isinstance(value, dict)
               and isinstance(value.get('attemptedAt', ''), str)}
    budget = budget if budget is not None else RequestBudget(transport or PublicTransport(8), 6)
    if budget.maximum != 6:
        raise ValueError('Official and guide sources must share the six-request ceiling')
    pages, rows, reports = PublicPages(budget), [], []
    # Unvisited/deferred sources get priority next invocation; no starvation by Primera.
    ordered = sorted(config['sources'], key=lambda s: entries.get(s['id'], {}).get('attemptedAt', ''))
    for source in ordered:
        entry = entries.get(source['id'], {})
        cached, fresh = [], False
        try:
            if 'fetchedAt' in entry:
                age = (now - instant(entry['fetchedAt'])).total_seconds()
                cached = checked_rows(entry['rows'], source, day)
                fresh = 0 <= age < 6 * 3600
        except (KeyError, TypeError, ValueError):
            cached = []
        before, state, reason = budget.requests, '', None
        if fresh:
            selected, state = cached, 'CACHE_HIT'
        elif offline:
            selected, state = cached, 'CACHE_STALE' if cached else 'OFFLINE_MISS'
        elif budget.maximum - budget.requests < source['requestCeiling']:
            selected, state = cached, 'DEFERRED_BUDGET'
        else:
            try:
                selected = checked_rows(source_rows(source, day, pages), source, day)
                state = 'OK'
                entries[source['id']] = {'fetchedAt': now.isoformat(), 'attemptedAt': now.isoformat(), 'rows': selected}
            except (DiscoveryError, OSError, TimeoutError, UnicodeError, KeyError, TypeError, ValueError) as error:
                selected, state, reason = cached, 'ERROR_USING_CACHE' if cached else 'ERROR', type(error).__name__
                if isinstance(error, DiscoveryError) and str(error) == 'OFFICIAL_FIXTURE_CONFLICT':
                    # Conflicting documents never keep an old broadcaster active.
                    selected = [{**row, 'broadcasters': []} for row in cached]
                    state, reason = 'REVIEW', 'OFFICIAL_FIXTURE_CONFLICT'
                    entry = {**entry, 'rows': selected, 'quarantined': True}
                entries[source['id']] = {**entry, 'attemptedAt': now.isoformat()}
        if entries.get(source['id'], {}).get('quarantined'):
            selected = [{**row, 'broadcasters': []} for row in selected]
            state, reason = 'REVIEW', 'OFFICIAL_FIXTURE_CONFLICT'
        rows.extend(selected)
        reports.append({'source': source['id'], 'competition': source['competition'], 'state': state,
                        'requests': budget.requests - before, 'fixturesWindow': len(selected),
                        'fixturesToday': sum(instant(row['startTime']).date() == day for row in selected),
                        **({'reason': reason} if reason else {})})
    return rows, {'schemaVersion': 1, 'date': day.isoformat(), 'registryHash': fingerprint, 'sources': entries}, \
        {'sources': reports, 'publicRequests': budget.requests, 'sportsApiRequests': 0}


def preview(rows, day, now, config, groups, mapping, identity=None, daily=None, guide_records=None, guide_config=None,
            blocked_competitions=()):
    store = IdentityStore(copy.deepcopy(identity))
    sections = [{'id': sid, 'title': title, 'priority': priority, 'events': []}
                for sid, title, priority, _ in generator.SECTIONS]
    buckets = {section['id']: section['events'] for section in sections}
    feed = {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': generator.TIMEZONE,
            'updatedAt': instant(now.isoformat()).isoformat(timespec='seconds'), 'demo': False,
            'sportsDataMode': 'DEGRADED', 'sections': sections}
    assignments, details, seen = [], [], set()
    specifications = {source['id']: source for source in validate_registry(config)['sources']}
    if guide_config is not None:
        from .prioritized_agendas import validate_guides
        validate_guides(guide_config)
        specifications.update({entry['id']: {'id': entry['id'], 'competition': name, 'provider': 'argentina_agendas',
                                             'section': entry['section']} for name, entry in guide_config['agendaCompetitions'].items()})
    for raw in rows:
        spec = specifications.get(raw.get('competitionId'))
        if spec is None:
            raise DiscoveryError('OFFICIAL_ROW_INVALID')
        if raw.get('source') == 'argentina_agendas':
            spec = {**spec, 'provider': 'argentina_agendas'}
        clean = checked_rows([raw], spec, day)
        if not clean or instant(clean[0]['startTime']).date() != day:
            continue
        row = clean[0]
        identifier = store.resolve(row['source'], row['providerFixtureId'])
        if identifier in seen:
            raise DiscoveryError('OFFICIAL_FIXTURE_DUPLICATE')
        seen.add(identifier)
        start = instant(row['startTime'])
        event = {'id': f'fixture-{identifier}', 'sport': 'football', 'competition': row['competition'],
                 'homeTeam': row['homeTeam'], 'awayTeam': row['awayTeam'], 'homeLogo': '', 'awayLogo': '',
                 'startTime': start.strftime('%H:%M'), 'status': 'SCHEDULED'}
        section = spec['section']
        if row['competition'] == 'Amistosos de selecciones':
            from .sports_national_agenda import confederation
            if any(confederation(name) == 'CONMEBOL' for name in (row['homeTeam'], row['awayTeam'])):
                section = 'south_america_national_teams'
        buckets[section].append(event)
        fixture = Fixture(identifier, row['homeTeam'], row['awayTeam'], row['competition'], start,
                          section, 'Argentina' if section == 'argentina' else '')
        # Only the LPF/AFA fixture row explicitly provides Argentina-market TV.
        # CONMEBOL cards observed so far do not publish territory-specific TV.
        evidence = [Evidence(identifier, start.isoformat(), name, 'Argentina', row['source'],
                    row['source'], True, True, row['sourceUrl'], 'official_league')
                    for name in row['broadcasters']] if spec['section'] == 'argentina' and row['source'] in {'lpf_official', 'afa_official'} else []
        if guide_records is not None:
            from .prioritized_agendas import evidence_for, validate_guides
            validate_guides(guide_config)
            evidence += evidence_for(row, identifier, guide_records, guide_config)
        decision = confidence(fixture, evidence, mapping, groups)
        if guide_records is not None:
            from .prioritized_agendas import guide_conflict, signal_conflict
            if guide_conflict(row, guide_records, guide_config) or signal_conflict(row, guide_records, guide_config, mapping):
                decision.update(state='REVIEW', channelGroups=[], evidenceLevel='UNKNOWN')
        if row['competition'] in blocked_competitions:
            decision.update(state='REVIEW', channelGroups=[], evidenceLevel='UNKNOWN')
        if decision['state'] == 'CONFIRMED_MBOX':
            assignments.append({'fixtureId': identifier, 'channelGroups': decision['channelGroups'],
                                'confidence': 'confirmed', 'source': 'auto',
                                'broadcaster': decision['broadcaster'], 'evidenceCount': decision['evidenceCount']})
        details.append({'fixtureId': identifier, **row, **decision,
                        'sourceConfidence': 'EDITORIAL_AGENDA' if row['source'] == 'argentina_agendas' else 'OFFICIAL'})
    if daily is not None:
        validate_broadcasts(daily)
        if daily['date'] == day.isoformat():
            manual = [item for item in daily['broadcasts'] if item.get('source', 'manual') == 'manual'
                      and item['confidence'] == 'confirmed' and item['fixtureId'] in seen]
            manual_ids = {item['fixtureId'] for item in manual}
            assignments = [item for item in assignments if item['fixtureId'] not in manual_ids] + manual
    broadcasts = {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': generator.TIMEZONE,
                  'broadcasts': assignments}
    apply_daily_assignments(feed, resolve_broadcasts(broadcasts, feed, groups, generator.normalize))
    apply_playback_policy(feed, now, ZONE, groups, log=lambda message: None)
    for section in sections:
        section['events'].sort(key=generator.event_sort_key)
    generator.validate_feed(feed)
    return feed, broadcasts, details, store.value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=generator.ROOT)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    root, output = args.root.resolve(), args.output_dir.resolve()
    # A local diagnostic command cannot overwrite any repository/production artifact.
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        parser.error('--output-dir must be an isolated directory outside the repository')
    config = validate_registry(read_json(root / REGISTRY))
    now = datetime.now(ZONE)
    groups = load_clean_groups(root / 'mbox_sports_channels_clean.json', generator.normalize)
    mapping = read_json(root / 'sports_broadcaster_mapping.json')
    from .prioritized_agendas import acquire_all
    guide_config = read_json(root / 'sports_agenda_guides.json')
    rows, cache, guide_cache, records, report = acquire_all(config, guide_config, now.date(), now, groups, mapping,
        read_json(output / 'official_source_cache.json', optional=True),
        read_json(output / 'argentina_guide_cache.json', optional=True), offline=args.offline)
    identity = read_json(output / 'sports_fixture_identity.preview.json', optional=True) or \
        read_json(root / 'sports_fixture_identity.json', optional=True)
    feed, broadcasts, details, identity = preview(rows, now.date(), now, config, groups, mapping,
        identity=identity, daily=read_json(root / 'sports_broadcasts_today.json', optional=True),
        guide_records=records, guide_config=guide_config, blocked_competitions=report.get('blockedCompetitions', ()))
    report.update(mode='LOCAL_PREVIEW_ONLY', date=now.date().isoformat(), fixtures=details,
                  totalFixtures=sum(len(section['events']) for section in feed['sections']),
                  withChannels=sum(bool(event.get('channels')) for section in feed['sections'] for event in section['events']))
    for name, value in [('official_source_cache.json', cache), ('argentina_guide_cache.json', guide_cache), ('sports_today.preview.json', feed),
                        ('sports_broadcasts_today.preview.json', broadcasts), ('official_bot_report.json', report),
                        ('sports_fixture_identity.preview.json', identity)]:
        atomic_json(output / name, value)
    print(json.dumps({key: report[key] for key in ('mode', 'date', 'publicRequests', 'sportsApiRequests',
                                                 'totalFixtures', 'withChannels', 'guides', 'sources')}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
