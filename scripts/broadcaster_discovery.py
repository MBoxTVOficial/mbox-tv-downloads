"""Daily, conservative discovery with independent evidence and a date-scoped cache."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

if __package__:
    from .broadcaster_sources import (BroadcasterProvider, DiscoveryError, Evidence, Fixture,
        PARSERS, PublicTransport, RequestBudget, ZONE, instant, norm, public_url, safe_text)
    from .sports_competition_broadcasters import validate_registry, search_hints, territory
    from .sports_daily_broadcasts import validate_broadcasts, resolve_broadcasts, TIMEZONE
else:
    from broadcaster_sources import (BroadcasterProvider, DiscoveryError, Evidence, Fixture,
        PARSERS, PublicTransport, RequestBudget, ZONE, instant, norm, public_url, safe_text)
    from sports_competition_broadcasters import validate_registry, search_hints, territory
    from sports_daily_broadcasts import validate_broadcasts, resolve_broadcasts, TIMEZONE

STATES = ('CONFIRMED_MBOX', 'CONFIRMED_EXTERNAL', 'REVIEW', 'UNRESOLVED')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def read_json(path, optional=False):
    try:
        raw = path.read_bytes()
        if len(raw) > 4_000_000:
            raise ValueError()
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)
    except FileNotFoundError:
        if optional:
            return None
        raise DiscoveryError('Falta configuración de descubrimiento.') from None
    except (OSError, ValueError, UnicodeError):
        raise DiscoveryError('JSON de descubrimiento inválido; no se publica configuración nueva.') from None


def atomic_json(path, value):
    path = Path(path)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    if path.exists() and path.read_bytes() == encoded:
        return False
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_settings(sources, mapping, groups):
    if not isinstance(sources, dict) or type(sources.get('schemaVersion')) is not int or sources['schemaVersion'] != 1 or \
            set(sources) != {'schemaVersion', 'limits', 'providers', 'teamAliases'}:
        raise DiscoveryError('Configuración de providers inválida.')
    limits = sources['limits']
    bounds = {'maxRequests': (1, 6), 'concurrency': (1, 2), 'timeoutSeconds': (1, 8),
              'maxFixtures': (1, 500), 'retryAfterSeconds': (300, 21600)}
    if not isinstance(limits, dict) or set(limits) != set(bounds) or any(
            type(limits[key]) is not int or not low <= limits[key] <= high for key, (low, high) in bounds.items()):
        raise DiscoveryError('Límites de descubrimiento inválidos.')
    providers, seen, owners = sources['providers'], set(), {}
    if not isinstance(providers, list):
        raise DiscoveryError('providers debe ser lista.')
    for provider in providers:
        fields = {'id', 'adapter', 'enabled', 'url', 'kind', 'territory', 'independenceKey', 'ownedBroadcasters'}
        if not isinstance(provider, dict) or not fields <= set(provider) or set(provider)-fields-{'competitions'} or \
                not all(safe_text(provider[key]) for key in ('id', 'territory', 'independenceKey')) or \
                provider['id'] in seen or provider['adapter'] not in PARSERS or type(provider['enabled']) is not bool or \
                provider['kind'] not in ('official_league', 'official_club', 'official_broadcaster', 'trusted_guide') or \
                not isinstance(provider['ownedBroadcasters'], list) or any(not safe_text(name) for name in provider['ownedBroadcasters']) or \
                not isinstance(provider.get('competitions', []), list) or any(not safe_text(name) for name in provider.get('competitions', [])):
            raise DiscoveryError('Provider inválido/duplicado.')
        public_url(provider['url'])
        seen.add(provider['id'])
        # All editions/pages on one domain belong to one editorial owner, never two votes.
        from urllib.parse import urlsplit
        host = urlsplit(provider['url']).hostname.removeprefix('www.')
        if host in owners and owners[host] != provider['independenceKey']:
            raise DiscoveryError('Un dominio no puede contar como dos fuentes independientes.')
        owners[host] = provider['independenceKey']
    aliases = {}
    if not isinstance(sources['teamAliases'], dict):
        raise DiscoveryError('teamAliases inválido.')
    for canonical, alternatives in sources['teamAliases'].items():
        if not safe_text(canonical) or not isinstance(alternatives, list) or any(not safe_text(name) for name in alternatives):
            raise DiscoveryError('Alias de equipo inválido.')
        for name in [canonical]+alternatives:
            if norm(name) in aliases and aliases[norm(name)] != norm(canonical):
                raise DiscoveryError('Alias de equipo ambiguo.')
            aliases[norm(name)] = norm(canonical)
    if not isinstance(mapping, dict) or type(mapping.get('schemaVersion')) is not int or mapping['schemaVersion'] != 1 or \
            set(mapping) != {'schemaVersion', 'signals'} or not isinstance(mapping['signals'], list):
        raise DiscoveryError('Mapping de broadcasters inválido.')
    keys = set()
    for signal in mapping['signals']:
        if not isinstance(signal, dict) or set(signal) != {'name', 'territory', 'channelGroup', 'aliases', 'intrinsicRegion'} or \
                not safe_text(signal['name']) or not isinstance(signal['territory'], str) or \
                type(signal['intrinsicRegion']) is not bool or not isinstance(signal['aliases'], list) or \
                any(not safe_text(name) for name in signal['aliases']):
            raise DiscoveryError('Señal de mapping inválida.')
        if signal['channelGroup'] is not None and signal['channelGroup'] not in groups:
            raise DiscoveryError('Unknown channelGroup en mapping.')
        if signal['channelGroup'] is not None:
            actual = groups[signal['channelGroup']].get('signalRegion', '')
            if actual and territory(actual) != territory(signal['territory']):
                raise DiscoveryError('Territorio de señal no coincide con channelGroup.')
        key = (norm(signal['name']), norm(signal['territory']))
        if key in keys:
            raise DiscoveryError('Señal de mapping duplicada.')
        keys.add(key)
    return aliases


REGIONS = {'brasil': 'brazil', 'brazil': 'brazil', 'mexico': 'mexico', 'argentina': 'argentina',
           'colombia': 'colombia', 'peru': 'peru', 'chile': 'chile'}


def broadcaster_key(name):
    value = norm(name)
    value = re.sub(r'^(espn)(\d)(?= |$)', r'\1 \2', value)
    return re.sub(r'\s+(hd|fhd|sd|4k)$', '', value)


def map_signal(name, territory, mapping):
    key, region = broadcaster_key(name), REGIONS.get(norm(territory), norm(territory))
    explicit = next((canonical for word, canonical in REGIONS.items() if re.search(rf'\b{word}\b', key)), None)
    if explicit and explicit != region:
        return name, territory, None, True
    matches = []
    for signal in mapping['signals']:
        aliases = {broadcaster_key(value) for value in [signal['name']]+signal['aliases']}
        signal_region = REGIONS.get(norm(signal['territory']), norm(signal['territory']))
        if key in aliases and (signal['intrinsicRegion'] or not signal_region or signal_region == (explicit or region)):
            matches.append(signal)
    if len(matches) == 1:
        signal = matches[0]
        return signal['name'], signal['territory'], signal['channelGroup'], False
    if matches or any(key in {broadcaster_key(v) for v in [s['name']]+s['aliases']} for s in mapping['signals']):
        return name, territory, None, True
    return name, territory, None, False


def confidence(fixture, evidence, mapping, groups):
    # The organisation's explicit fixture listing wins over competing generic/guide claims.
    organisation = [item for item in evidence if isinstance(item, Evidence) and item.official and item.trusted
        and item.source_type == 'official_league' and item.fixture_id == fixture.fixture_id
        and instant(item.kickoff) == fixture.kickoff]
    if organisation:
        # Discard weaker guide claims, but retain other explicit official listings
        # so genuinely incompatible official evidence is still REVIEW.
        evidence = [item for item in evidence if isinstance(item, Evidence) and item.official is True]
    valid, owners, market_sets, ambiguous = [], {}, {}, False
    for item in evidence:
        if not isinstance(item, Evidence) or item.fixture_id != fixture.fixture_id or \
                instant(item.kickoff) != fixture.kickoff or not item.trusted or \
                not safe_text(item.broadcaster) or not safe_text(item.source_name) or not safe_text(item.owner) or \
                type(item.official) is not bool or type(item.trusted) is not bool:
            continue
        public_url(item.url)
        name, region, group, unclear = map_signal(item.broadcaster, item.territory, mapping)
        ambiguous |= unclear
        key = (name, region, group)
        valid.append((key, item))
        owners.setdefault(key, set()).add(item.owner)
        market_sets.setdefault(norm(region), {}).setdefault(item.owner, set()).add(name)
    # Different exclusive listings in one market conflict. A guide adding an
    # extra platform does not negate an agreed TV signal; that extra signal
    # must independently satisfy the same confidence threshold before use.
    conflict = any(not left.intersection(right) for sources in market_sets.values()
                   for owner, left in sources.items() for other, right in sources.items() if owner < other)
    if not valid:
        return {'state': 'UNRESOLVED', 'broadcaster': [], 'channelGroups': [], 'evidenceCount': 0, 'evidence': [], 'evidenceLevel': 'UNKNOWN'}
    strong = [key for key, independent in owners.items() if (
        any(item.official for candidate, item in valid if candidate == key) or len(independent) >= 2
    )]
    confirmed = not ambiguous and not conflict and bool(strong)
    channel_groups = list(dict.fromkeys(key[2] for key in strong if key[2] is not None))
    if any(not groups[group].get('signalRegion') or
           not any(s['enabled'] for s in groups[group]['streams']) for group in channel_groups):
        confirmed = False
    return {'state': ('CONFIRMED_MBOX' if channel_groups else 'CONFIRMED_EXTERNAL') if confirmed else 'REVIEW',
            'broadcaster': list(dict.fromkeys(key[0] for key in strong if confirmed)) if confirmed else list(dict.fromkeys(key[0] for key in owners)),
            'channelGroups': channel_groups if confirmed else [],
            'evidenceCount': len({item.owner for _, item in valid}),
            'evidence': [asdict(item) for _, item in valid],
            'evidenceLevel': ('A' if any(item.official for key, item in valid if key in strong) else 'C') if confirmed else 'UNKNOWN'}


def fixtures_from_feed(feed, countries=None):
    seen, fixtures = set(), []
    for section in feed['sections']:
        for event in section['events']:
            match = re.fullmatch(r'fixture-(\d+)', event['id'])
            if not match or int(match[1]) <= 0 or int(match[1]) in seen:
                raise DiscoveryError('Agenda con fixtureId inválido/duplicado.')
            identifier = int(match[1])
            seen.add(identifier)
            if not re.fullmatch(r'\d{2}:\d{2}', event['startTime']):
                continue  # TBD: no safe kickoff match, never auto-confirm.
            fixtures.append(Fixture(identifier, event['homeTeam'], event['awayTeam'], event['competition'],
                instant(f"{feed['date']}T{event['startTime']}:00-03:00"), section['id'],
                (countries or {}).get(identifier, '')))
    return fixtures


def priority(fixture):
    sections = ('south_america_qualifiers', 'south_america_national_teams', 'argentina', 'conmebol', 'champions',
                'spain', 'england', 'france', 'italy')
    if fixture.section in sections:
        return sections.index(fixture.section), fixture.kickoff, fixture.fixture_id
    country = norm(fixture.country)
    return (4 if country == 'brazil' else 10 if country in ('colombia', 'mexico') else 20), fixture.kickoff, fixture.fixture_id


def discover(feed, groups, sources, mapping, previous=None, cache=None, now=None, providers=None,
             transport=None, scope='all', countries=None, registry=None):
    aliases = validate_settings(sources, mapping, groups)
    if registry is not None:
        validate_registry(registry)
    now = now or datetime.now(ZONE)
    if now.tzinfo is None or scope not in ('all', 'unresolved') or now.astimezone(ZONE).date().isoformat() != feed['date']:
        raise DiscoveryError('Día/alcance de descubrimiento inválido.')
    if previous is not None:
        validate_broadcasts(previous)
    fixtures = fixtures_from_feed(feed, countries)
    all_events = {int(event['id'].removeprefix('fixture-')): event
                  for section in feed['sections'] for event in section['events']}
    identifiers = set(all_events)
    prior = {item['fixtureId']: item for item in (previous or {}).get('broadcasts', [])
             if previous['date'] == feed['date'] and item['fixtureId'] in identifiers}
    manual = {identifier: item for identifier, item in prior.items()
              if item.get('source', 'manual') == 'manual' and item['confidence'] == 'confirmed'}
    fingerprint = digest([sources, mapping, registry, groups])
    cached = cache if isinstance(cache, dict) and type(cache.get('schemaVersion')) is int and cache['schemaVersion'] == 1 and \
        cache.get('date') == feed['date'] and cache.get('configDigest') == fingerprint and isinstance(cache.get('fixtures'), dict) else {}
    records, pending, cache_hits = {}, [], 0
    for fixture in sorted(fixtures, key=priority):
        identifier = fixture.fixture_id
        if identifier in manual:
            item = manual[identifier]
            records[identifier] = {'state': 'CONFIRMED_MBOX', 'broadcaster': item.get('broadcaster', []),
                'channelGroups': item['channelGroups'], 'evidenceCount': item.get('evidenceCount', 0),
                'evidence': [], 'source': 'manual'}
            continue
        if scope == 'unresolved' and identifier in prior and prior[identifier]['confidence'] == 'confirmed':
            item = prior[identifier]
            records[identifier] = {'state': 'CONFIRMED_MBOX', 'broadcaster': item.get('broadcaster', []),
                'channelGroups': item['channelGroups'], 'evidenceCount': item.get('evidenceCount', 0),
                'evidence': [], 'preserved': True}
            continue
        entry = cached.get('fixtures', {}).get(str(identifier))
        if isinstance(entry, dict) and entry.get('fingerprint') == digest(fixture.fingerprint):
            try:
                old = entry['result']
                state = old['state']
                age = now.astimezone(ZONE)-instant(entry['timestamp'])
                if old.get('preserved') is True and state == 'CONFIRMED_MBOX' and identifier in prior and \
                        prior[identifier]['confidence'] == 'confirmed' and old['channelGroups'] == prior[identifier]['channelGroups'] and \
                        timedelta() <= age < timedelta(seconds=sources['limits']['retryAfterSeconds']):
                    records[identifier] = old
                    cache_hits += 1
                    continue
                ttl = timedelta(days=1) if state.startswith('CONFIRMED_') else timedelta(seconds=sources['limits']['retryAfterSeconds'])
                result = confidence(fixture, [Evidence(**value) for value in old['evidence']], mapping, groups)
                if state in STATES and result == old and timedelta() <= age < ttl:
                    records[identifier] = result
                    cache_hits += 1
                    continue
            except (ValueError, TypeError, KeyError):
                pass
        if all_events[identifier]['status'] not in ('SCHEDULED', 'LIVE'):
            records[identifier] = {'state': 'UNRESOLVED', 'broadcaster': [], 'channelGroups': [], 'evidenceCount': 0, 'evidence': [], 'evidenceLevel': 'UNKNOWN'}
        elif scope == 'unresolved' and fixture.kickoff <= now:
            records[identifier] = {'state': 'UNRESOLVED', 'broadcaster': [], 'channelGroups': [], 'evidenceCount': 0, 'evidence': []}
        elif len(pending) < sources['limits']['maxFixtures']:
            pending.append(fixture)
    provider_list = list(providers) if providers is not None else [BroadcasterProvider(config, aliases)
        for config in sources['providers'] if config['enabled']]
    if providers is not None and len(provider_list) > sources['limits']['maxRequests']:
        raise DiscoveryError('Providers exceden límite de requests.')
    selected = [provider for provider in provider_list if any(provider.supports(fixture) for fixture in pending)]
    def provider_priority(provider):
        config = getattr(provider, 'config', {})
        hints = {broadcaster_key(n) for f in pending for n in search_hints(registry, f.competition, f.kickoff.date(), f.country)}
        owned = {broadcaster_key(n) for n in config.get('ownedBroadcasters', [])}
        return (0 if config.get('adapter') == 'lpf_agenda' else 1 if territory(config.get('territory', '')) == 'argentina'
                and hints & owned else 2)
    selected.sort(key=provider_priority)
    transport = RequestBudget(transport or PublicTransport(sources['limits']['timeoutSeconds']), sources['limits']['maxRequests'])
    failures, ready = [], []
    def prepare(provider):
        try:
            if hasattr(provider, 'bind_fixtures'):
                provider.bind_fixtures(pending)
            provider.prepare(now.astimezone(ZONE).date(), transport)
            return provider, None
        except Exception as error:
            return provider, {'provider': provider.sourceName, 'error': type(error).__name__}
    lpf = [p for p in selected if getattr(p, 'config', {}).get('adapter') == 'lpf_agenda']
    for provider in lpf:
        prepared, failure = prepare(provider)
        if failure:
            failures.append(failure)
        else:
            ready.append(prepared)
    remaining = [p for p in selected if p not in lpf][:sources['limits']['maxRequests'] - transport.requests]
    selected = lpf + remaining
    with ThreadPoolExecutor(max_workers=sources['limits']['concurrency']) as pool:
        for provider, failure in pool.map(prepare, remaining):
            if failure:
                failures.append(failure)
            else:
                ready.append(provider)
    for fixture in pending:
        evidence = []
        for provider in ready:
            try:
                if provider.supports(fixture):
                    evidence.extend(provider.lookup(fixture))
            except Exception as error:
                failures.append({'provider': provider.sourceName, 'error': type(error).__name__})
        try:
            result = confidence(fixture, evidence, mapping, groups)
        except (DiscoveryError, ValueError, TypeError, KeyError):
            failures.append({'provider': 'evidence-validation', 'error': 'InvalidEvidence'})
            result = {'state': 'REVIEW', 'broadcaster': [], 'channelGroups': [], 'evidenceCount': 0, 'evidence': []}
        if result['state'] == 'UNRESOLVED' and fixture.fixture_id in prior:
            # Empty/unavailable sources do not disprove a same-day confirmed assignment.
            item = prior[fixture.fixture_id]
            if item['confidence'] == 'confirmed':
                result = {'state': 'CONFIRMED_MBOX', 'broadcaster': item.get('broadcaster', []),
                    'channelGroups': item['channelGroups'], 'evidenceCount': item.get('evidenceCount', 0),
                    'evidence': [], 'preserved': True}
        records[fixture.fixture_id] = result
    assignments = [item for identifier, item in manual.items()
                   if identifier not in {fixture.fixture_id for fixture in fixtures}]
    for fixture in fixtures:
        record = records.get(fixture.fixture_id, {'state': 'UNRESOLVED'})
        if fixture.fixture_id in manual:
            assignments.append(manual[fixture.fixture_id])
        elif record['state'] == 'CONFIRMED_MBOX' and all_events[fixture.fixture_id]['status'] in ('SCHEDULED', 'LIVE'):
            if record.get('preserved'):
                assignments.append(prior[fixture.fixture_id])
            else:
                assignments.append({'fixtureId': fixture.fixture_id, 'channelGroups': record['channelGroups'],
                    'confidence': 'confirmed', 'source': 'auto', 'broadcaster': record['broadcaster'],
                    'evidenceCount': record['evidenceCount'], 'notes': 'Programación pública del partido validada automáticamente.'})
    config = {'schemaVersion': 1, 'date': feed['date'], 'timezone': TIMEZONE,
              'broadcasts': sorted(assignments, key=lambda item: item['fixtureId'])}
    resolve_broadcasts(config, feed, groups, norm)
    new_cache = {'schemaVersion': 1, 'date': feed['date'], 'configDigest': fingerprint, 'fixtures': {}}
    details = []
    for fixture in fixtures:
        record = records.get(fixture.fixture_id, {'state': 'UNRESOLVED', 'broadcaster': [], 'channelGroups': [],
                                                'evidenceCount': 0, 'evidence': []})
        if fixture.fixture_id not in manual and fixture.fixture_id in records:
            old = cached.get('fixtures', {}).get(str(fixture.fixture_id), {})
            timestamp = old.get('timestamp') if fixture.fixture_id not in {f.fixture_id for f in pending} and old else now.isoformat()
            new_cache['fixtures'][str(fixture.fixture_id)] = {'fingerprint': digest(fixture.fingerprint),
                'timestamp': timestamp, 'result': record}
        details.append({'fixtureId': fixture.fixture_id, 'homeTeam': fixture.home, 'awayTeam': fixture.away,
            'competition': fixture.competition, 'startTime': fixture.kickoff.isoformat(),
            'rightsLevel': 'B' if search_hints(registry, fixture.competition, fixture.kickoff.date(), fixture.country) else 'UNKNOWN',
            'searchCandidates': search_hints(registry, fixture.competition, fixture.kickoff.date(), fixture.country), **record})
    timed_ids = {fixture.fixture_id for fixture in fixtures}
    for identifier, event in all_events.items():
        if identifier not in timed_ids:
            details.append({'fixtureId': identifier, 'homeTeam': event['homeTeam'], 'awayTeam': event['awayTeam'],
                'competition': event['competition'], 'startTime': event['startTime'],
                'state': 'CONFIRMED_MBOX' if identifier in manual else 'UNRESOLVED',
                'broadcaster': manual.get(identifier, {}).get('broadcaster', []),
                'channelGroups': manual.get(identifier, {}).get('channelGroups', []), 'evidenceCount': 0, 'evidence': []})
    counts = Counter(item['state'] for item in details)
    report = {'schemaVersion': 1, 'date': feed['date'], 'timezone': TIMEZONE,
        'totalFixtures': sum(len(section['events']) for section in feed['sections']),
        'investigated': len(pending), 'cacheHits': cache_hits, 'manualPreserved': len(manual),
        'requests': transport.requests, 'limits': sources['limits'], 'providerWarnings': failures,
        'counts': {state: counts[state] for state in STATES}, 'fixtures': details}
    return config, new_cache, report


def discover_from_root(feed, root, previous=None, now=None, scope='all', countries=None):
    """Read configuration/cache; return candidates without writing any production file."""
    if __package__:
        from .sports_channel_rules import load_clean_groups
    else:
        from sports_channel_rules import load_clean_groups
    sources = read_json(root / 'sports_broadcaster_sources.json')
    mapping = read_json(root / 'sports_broadcaster_mapping.json')
    registry = read_json(root / 'sports_competition_broadcasters.json', optional=True)
    groups = load_clean_groups(root / 'mbox_sports_channels_clean.json', norm)
    warning = None
    try:
        cache = read_json(root / 'sports_broadcast_discovery_cache.json', optional=True)
    except DiscoveryError:
        cache, warning = None, {'provider': 'cache', 'error': 'InvalidCache'}
    config, cache, report = discover(feed, groups, sources, mapping, previous, cache, now,
                                   scope=scope, countries=countries, registry=registry)
    if warning:
        report['providerWarnings'].append(warning)
    return config, cache, report, groups


def cache_key(root, day=None):
    day = day or datetime.now(ZONE).date()
    return day.isoformat() + '-' + digest([read_json(root / 'sports_broadcaster_sources.json'),
        read_json(root / 'sports_broadcaster_mapping.json'),
        read_json(root / 'sports_competition_broadcasters.json', optional=True),
        read_json(root / 'mbox_sports_channels_clean.json', optional=True)])[:16]


if __name__ == '__main__':
    # Workflow helper only: no discovery, API call or file write.
    import argparse
    parser = argparse.ArgumentParser(description='Clave diaria del cache público de broadcasters.')
    parser.add_argument('--cache-key', action='store_true', required=True)
    args = parser.parse_args()
    print(cache_key(Path(__file__).resolve().parent.parent))
