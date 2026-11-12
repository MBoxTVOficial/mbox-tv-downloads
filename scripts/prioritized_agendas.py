"""Argentina guide order and exact reconciliation; official organisations keep authority."""
import copy
import hashlib
import json
import re

from .broadcaster_sources import DiscoveryError, Evidence, Fixture, Listing, instant, norm, safe_text
from .broadcaster_discovery import confidence, map_signal
from .sports_national_agenda import national_key, senior_pair, confederation

PROVIDERS = (
    {'id': 'futbol-en-vivo-ar', 'url': 'https://www.futbolenvivoargentina.com/', 'owner': 'football-on-tv', 'adapter': 'argentina_tv'},
    {'id': 'ole-agenda', 'url': 'https://www.ole.com.ar/agenda-deportiva', 'owner': 'ole-tyc-conservative', 'adapter': 'ole'},
    {'id': 'tyc-agenda', 'url': 'https://www.tycsports.com/agenda-deportiva-hoy.html', 'owner': 'ole-tyc-conservative', 'adapter': 'tyc'},
)
OFFICIAL = {'lpf_official', 'afa_official', 'conmebol_official'}
OWNED_TYC = {'tyc sports', 'tyc sports play'}
EUROPE_AGENDA = {
    'LaLiga': {'id': 'agenda-laliga', 'section': 'spain'},
    'Premier League': {'id': 'agenda-premier', 'section': 'england'},
    'Serie A': {'id': 'agenda-serie-a-italia', 'section': 'italy'},
    'Ligue 1': {'id': 'agenda-ligue-1', 'section': 'france'},
}
SELECTION_AGENDA = {
    'UEFA Nations League': {'id': 'agenda-uefa-nations', 'section': 'uefa_nations'},
    'CONCACAF Nations League': {'id': 'agenda-concacaf-nations', 'section': 'international'},
    'Amistosos de selecciones': {'id': 'agenda-national-friendlies', 'section': 'international'},
}


def validate_guides(config):
    if not isinstance(config, dict) or config.get('schemaVersion') != 1 or config.get('territory') != 'Argentina' or \
            config.get('cacheHours') != 2 or config.get('providers') != list(PROVIDERS) or \
            config.get('agendaCompetitions') != {**EUROPE_AGENDA, **SELECTION_AGENDA} or \
            set(config) != {'schemaVersion', 'territory', 'cacheHours', 'providers', 'competitionAliases', 'argentinaTeamAliases', 'reserveTeamAliases', 'signalAliases', 'agendaCompetitions'}:
        raise DiscoveryError('ARGENTINA_GUIDE_CONFIGURATION_INVALID')
    for field in ('competitionAliases', 'argentinaTeamAliases', 'reserveTeamAliases', 'signalAliases'):
        aliases = config[field]
        if not isinstance(aliases, dict) or any(not safe_text(k) or not isinstance(v, list) or
                any(not safe_text(name) for name in v) for k, v in aliases.items()):
            raise DiscoveryError('ARGENTINA_GUIDE_ALIASES_INVALID')
        seen = {}
        for canonical, names in aliases.items():
            for label in [canonical] + names:
                key = norm(label)
                if key in seen and seen[key] != canonical:
                    raise DiscoveryError('ARGENTINA_GUIDE_ALIAS_AMBIGUOUS')
                seen[key] = canonical
    if set(config['competitionAliases']) != {'Liga Profesional Argentina', 'Torneo Proyección', 'Primera Nacional',
                                            'CONMEBOL Libertadores', 'CONMEBOL Sudamericana'} | set(EUROPE_AGENDA) | set(SELECTION_AGENDA):
        raise DiscoveryError('ARGENTINA_GUIDE_SCOPE_INVALID')
    return config


def canonical_competition(value, config):
    key = norm(value)
    if key == 'serie a':
        return None  # Unqualified Série A may refer to Brazil; require explicit Italy context.
    return next((name for name, names in config['competitionAliases'].items()
                 if key in {norm(item) for item in [name] + names}), None)


def team_key(value, competition, config):
    if competition in SELECTION_AGENDA:
        return norm(national_key(value) or value)
    # Explicit bracket seeds are metadata, not fuzzy team-name prefixes.
    key = norm(re.sub(r'\s+\([1-8][AB]\)$', '', value))
    if competition not in ('Liga Profesional Argentina', 'Torneo Proyección', 'Primera Nacional'):
        return key
    if competition == 'Torneo Proyección':
        key = re.sub(r'\s+(?:reserva|reserves)$', '', key)
    aliases_by_name = {**config['argentinaTeamAliases'],
                       **(config['reserveTeamAliases'] if competition == 'Torneo Proyección' else {})}
    for canonical, aliases in aliases_by_name.items():
        if key in {norm(item) for item in [canonical] + aliases}:
            return norm(canonical)
    return key


def signal_name(value, config):
    return next((name for name, aliases in config['signalAliases'].items()
                 if norm(value) in {norm(item) for item in [name] + aliases}), value)


def pair_key(row, config):
    return (row['competition'], team_key(row['homeTeam'], row['competition'], config),
            team_key(row['awayTeam'], row['competition'], config))


def exact_key(row, config):
    return (*pair_key(row, config), instant(row['startTime']).isoformat())


def parse_page(adapter, html, day):
    if adapter == 'argentina_tv':
        from .argentina_tv_sources import parse_argentina_tv_listings
        return parse_argentina_tv_listings(html, day)
    if adapter == 'ole':
        from .ole_agenda_source import parse_ole_listings
        return parse_ole_listings(html, day)
    if adapter == 'tyc':
        from .argentina_tv_sources import parse_tyc_listings
        return parse_tyc_listings(html, day)
    raise DiscoveryError('ARGENTINA_GUIDE_ADAPTER_INVALID')


def checked_records(listings, provider, day, config, canonicalized=False):
    records, seen = [], {}
    for item in listings:
        if not isinstance(item, Listing) or not all(safe_text(value) for value in (item.home, item.away, item.competition)):
            raise DiscoveryError('ARGENTINA_GUIDE_LISTING_INVALID')
        start = instant(item.kickoff.isoformat())
        if start.date() != day:
            continue
        competition = item.competition if canonicalized and item.competition in config['competitionAliases'] else canonical_competition(item.competition, config)
        if competition is None:
            continue  # New competitions do not become enabled through a guide page.
        if competition in SELECTION_AGENDA:
            if not senior_pair(item.home, item.away):
                continue
            expected = {'UEFA Nations League': 'UEFA', 'CONCACAF Nations League': 'CONCACAF'}.get(competition)
            if expected and any(confederation(name) != expected for name in (item.home, item.away)):
                continue
        if not isinstance(item.broadcasters, (tuple, list)) or not all(safe_text(value, 100) for value in item.broadcasters):
            raise DiscoveryError('ARGENTINA_GUIDE_SIGNALS_INVALID')
        row = {'competition': competition, 'homeTeam': item.home, 'awayTeam': item.away,
               'startTime': start.isoformat(), 'broadcasters': list(dict.fromkeys(signal_name(value, config) for value in item.broadcasters)),
               'guideId': provider['id'], 'sourceUrl': provider['url']}
        key = exact_key(row, config)
        pair = pair_key(row, config)
        if pair in seen and seen[pair] != key:
            raise DiscoveryError('ARGENTINA_GUIDE_KICKOFF_CONFLICT')
        seen[pair] = key
        previous = next((record for record in records if exact_key(record, config) == key), None)
        if previous:
            if previous['broadcasters'] != row['broadcasters']:
                raise DiscoveryError('ARGENTINA_GUIDE_SIGNALS_CONFLICT')
            continue
        records.append(row)
    return records


def cached_records(raw, provider, day, config):
    if not isinstance(raw, list):
        raise DiscoveryError('ARGENTINA_GUIDE_CACHE_INVALID')
    listings = []
    for row in raw:
        if not isinstance(row, dict) or row.get('guideId') != provider['id'] or row.get('sourceUrl') != provider['url']:
            raise DiscoveryError('ARGENTINA_GUIDE_CACHE_INVALID')
        listings.append(Listing(row['homeTeam'], row['awayTeam'], instant(row['startTime']), row['competition'], row['broadcasters']))
    return checked_records(listings, provider, day, config, canonicalized=True)


def merge_rows(official_rows, records, day, official_config, config):
    """First matching guide fills a gap; exact official fixtures never change."""
    result, conflicts = copy.deepcopy(official_rows), []
    official_pairs = {pair_key(row, config): row for row in official_rows}
    selected = {exact_key(row, config): row for row in result}
    codes = {source['competition']: source['id'] for source in official_config['sources']}
    codes.update({name: value['id'] for name, value in config['agendaCompetitions'].items()})
    rank = {provider['id']: index for index, provider in enumerate(PROVIDERS)}
    by_pair = {}
    for record in sorted(records, key=lambda row: rank[row['guideId']]):
        pair, key = pair_key(record, config), exact_key(record, config)
        prior = official_pairs.get(pair) or by_pair.get(pair)
        if prior and instant(prior['startTime']) != instant(record['startTime']):
            conflicts.append({'competition': record['competition'], 'homeTeam': record['homeTeam'],
                              'awayTeam': record['awayTeam'], 'guideId': record['guideId'],
                              'reason': 'KICKOFF_CONFLICT', 'keptSource': prior.get('source', prior.get('guideId'))})
            continue
        by_pair.setdefault(pair, record)
        if key in selected:
            continue
        # A guide cannot introduce an alias of an official opponent as a duplicate match.
        collisions = [row for row in result if row['competition'] == record['competition'] and
                      instant(row['startTime']) == instant(record['startTime']) and
                      (set(pair_key(row, config)[1:]) & set(pair[1:]))]
        if collisions:
            conflicts.append({'competition': record['competition'], 'guideId': record['guideId'],
                              'reason': 'TEAM_IDENTITY_REVIEW'})
            continue
        identity = json.dumps([record['competition'], *pair[1:], instant(record['startTime']).date().isoformat()])
        row = {'providerFixtureId': 'agenda:' + hashlib.sha256(identity.encode()).hexdigest(),
               'competitionId': codes[record['competition']], 'source': 'argentina_agendas',
               **{field: record[field] for field in ('competition', 'homeTeam', 'awayTeam', 'startTime', 'sourceUrl')},
               'broadcasters': []}  # Guide TV is evidence, never an official row claim.
        selected[key] = row
        result.append(row)
    return result, conflicts


def guide_conflict(row, records, config):
    if row['source'] in OFFICIAL:
        return False  # Exact organisation data settles the kickoff; wrong guide rows are ignored.
    pair = pair_key(row, config)
    return any(record['competition'] == row['competition'] and
        ((pair_key(record, config) == pair and instant(record['startTime']) != instant(row['startTime'])) or
         (instant(record['startTime']) == instant(row['startTime']) and
          set(pair_key(record, config)[1:]) & set(pair[1:]) and pair_key(record, config) != pair))
        for record in records)


def signal_conflict(row, records, config, mapping):
    if row['source'] in {'lpf_official', 'afa_official'} and row.get('broadcasters'):
        return False  # Explicit organisation fixture TV overrides lower-level guide conflicts.
    candidates = [record for record in records if exact_key(record, config) == exact_key(row, config)]
    signals = {record['guideId']: {map_signal(name, 'Argentina', mapping)[0] for name in record['broadcasters']}
               for record in candidates if record['broadcasters']}
    tv_signals = {record['guideId']: {group for name in record['broadcasters']
                 if (group := map_signal(name, 'Argentina', mapping)[2]) is not None}
                 for record in candidates}
    # A shared streaming platform cannot conceal ESPN vs ESPN 2 TV disagreement.
    tv_signals = {key: value for key, value in tv_signals.items() if value}
    return any(not left.intersection(right) for table in (signals, tv_signals)
               for key, left in table.items() for other, right in table.items() if key < other)


def evidence_for(row, identifier, records, config):
    key, evidence = exact_key(row, config), []
    for record in records:
        if exact_key(record, config) != key:
            continue
        provider = next(p for p in PROVIDERS if p['id'] == record['guideId'])
        for signal in record['broadcasters']:
            owned = provider['id'] == 'tyc-agenda' and norm(signal) in OWNED_TYC
            evidence.append(Evidence(identifier, instant(row['startTime']).isoformat(), signal, 'Argentina',
                provider['id'], provider['owner'], owned, True, provider['url'],
                'official_broadcaster' if owned else 'trusted_guide'))
    return evidence


def unresolved_targets(official_rows, records, day, official_config, config, groups, mapping):
    rows, conflicts = merge_rows(official_rows, records, day, official_config, config)
    targets = [row for row in rows if instant(row['startTime']).date() == day]
    if not targets:
        return True
    if conflicts:
        return True
    for index, row in enumerate(targets, 1):
        if guide_conflict(row, records, config) or signal_conflict(row, records, config, mapping):
            return True
        start = instant(row['startTime'])
        fixture = Fixture(index, row['homeTeam'], row['awayTeam'], row['competition'], start, '', 'Argentina')
        evidence = evidence_for(row, index, records, config)
        if row['source'] in {'lpf_official', 'afa_official'}:
            evidence += [Evidence(index, start.isoformat(), signal, 'Argentina', row['source'], row['source'],
                         True, True, row['sourceUrl'], 'official_league') for signal in row.get('broadcasters', [])]
        decision = confidence(fixture, evidence, mapping, groups)
        if decision['state'] not in {'CONFIRMED_MBOX', 'CONFIRMED_EXTERNAL'}:
            return True
        if decision['state'] == 'CONFIRMED_EXTERNAL' and any(map_signal(item.broadcaster, 'Argentina', mapping)[2]
                                                            for item in evidence):
            return True  # Keep looking for corroboration of a still-unconfirmed MBox alternative.
    return False


def acquire_guides(config, day, now, pages, official_rows, official_config, groups, mapping, cache=None, offline=False):
    config = validate_guides(config)
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    previous = cache if isinstance(cache, dict) and cache.get('date') == day.isoformat() and \
        cache.get('configHash') == fingerprint else {}
    entries = copy.deepcopy(previous.get('sources', {}))
    if not isinstance(entries, dict):
        entries = {}
    records, report = [], []
    for provider in PROVIDERS:
        # Continue for missing fixtures or insufficient confirmation, not one request per match.
        if records and not unresolved_targets(official_rows, records, day, official_config, config, groups, mapping):
            report.append({'source': provider['id'], 'state': 'SKIP_RESOLVED', 'requests': 0, 'fixtures': 0})
            continue
        entry = entries.get(provider['id'], {})
        entry = entry if isinstance(entry, dict) else {}
        cached, fresh = [], False
        try:
            age = (instant(now.isoformat()) - instant(entry['fetchedAt'])).total_seconds()
            cached = cached_records(entry['records'], provider, day, config)
            fresh = 0 <= age < 2 * 3600
        except (DiscoveryError, KeyError, ValueError, TypeError):
            pass
        before = pages.budget.requests
        reason = None
        parsed_count = entry.get('parsedFixtures', 0)
        if fresh:
            selected, state = cached, 'CACHE_HIT'
        elif offline:
            selected, state = cached, 'CACHE_STALE' if cached else 'OFFLINE_MISS'
        elif pages.budget.requests >= pages.budget.maximum:
            selected, state = cached, 'DEFERRED_BUDGET'
        else:
            try:
                listings = parse_page(provider['adapter'], pages.fetch(provider['url']), day)
                parsed_count = len(listings)
                selected = checked_records(listings, provider, day, config)
                state = 'OK' if selected else 'NO_SUPPORTED_FIXTURES' if listings else 'NO_MATCHES'
                entries[provider['id']] = {'fetchedAt': instant(now.isoformat()).isoformat(), 'records': selected,
                                            'parsedFixtures': parsed_count}
            except (DiscoveryError, KeyError, ValueError, TypeError, OSError, TimeoutError, UnicodeError) as error:
                selected, state, reason = cached, 'ERROR_USING_CACHE' if cached else 'ERROR', type(error).__name__
        records.extend(selected)
        report.append({'source': provider['id'], 'state': state, 'requests': pages.budget.requests - before,
                       'fixtures': len(selected), 'parsedFixtures': parsed_count, **({'reason': reason} if reason else {})})
    return records, {'schemaVersion': 1, 'date': day.isoformat(), 'configHash': fingerprint, 'sources': entries}, report


def acquire_all(official_config, config, day, now, groups, mapping, official_cache=None, guide_cache=None,
                transport=None, offline=False):
    from .official_football_bot import acquire, PublicPages, PublicTransport, RequestBudget
    # Existing official cache provides exact targets without moving guide network priority.
    cached_official, _, _ = acquire(official_config, day, now, official_cache, offline=True)
    budget = RequestBudget(transport or PublicTransport(8), 6)
    records, guide_cache, guide_report = acquire_guides(config, day, now, PublicPages(budget), cached_official,
        official_config, groups, mapping, guide_cache, offline)
    official_rows, official_cache, report = acquire(official_config, day, now, official_cache,
        offline=offline, budget=budget)
    rows, conflicts = merge_rows(official_rows, records, day, official_config, config)
    report.update(guides=guide_report, conflicts=conflicts, publicRequests=budget.requests,
                  blockedCompetitions=[source['competition'] for source in report['sources'] if source['state'] == 'REVIEW'])
    return rows, official_cache, guide_cache, records, report
