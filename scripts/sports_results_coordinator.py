"""Separate two-hour informative finals from five-minute confirmed match facts.

The shared sidecar is merged by exact fixture identity. Each scope owns its
network budget, never another scope's observations or any channel assignment.
"""
import copy
from datetime import date, timedelta
from pathlib import Path

from .broadcaster_discovery import atomic_json
from .broadcaster_sources import ZONE, norm
from .prioritized_agendas import validate_guides
from .sports_channel_rules import load_clean_groups
from .sports_daily_broadcasts import validate_broadcasts
from .sports_match_updates import (TIMEZONE, MatchUpdatesError, _confirmed_channel_events,
    _fixture, _iso, _source_key, _stamp, agenda_date, eligible_events, fixture_key,
    merge_updates, read_json, validate_request_state, validate_updates)
from .sports_results_sources import (PublicResultsTransport, compare_results,
    parse_provider, source_competitions, source_urls)

SCOPES = {'confirmed': (5, 288), 'informative': (120, 12)}


def _empty_scope():
    return {'cycles': 0, 'lastAttemptAt': None, 'slot': -1,
            'requests': {provider: 0 for provider in source_urls()}, 'settled': {}}


def _slot(now, scope):
    local = now.astimezone(ZONE)
    return (local.hour * 60 + local.minute) // SCOPES[scope][0]


def validate_ledger(value):
    """Validate the whole persisted budget before any mutation/network work."""
    if value is None:
        return None
    if isinstance(value, dict) and value.get('schemaVersion') == 1:
        return validate_request_state(value)
    try:
        required = {'schemaVersion', 'date', 'timezone', 'scopes'}
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'legacy'} or \
                type(value['schemaVersion']) is not int or value['schemaVersion'] != 2 or \
                value['timezone'] != TIMEZONE or not isinstance(value['scopes'], dict) or set(value['scopes']) != set(SCOPES):
            raise ValueError()
        day = date.fromisoformat(value['date'])
        if 'legacy' in value:
            validate_request_state(value['legacy'])
            if value['legacy']['date'] != value['date']:
                raise ValueError()
        for scope, ledger in value['scopes'].items():
            if not isinstance(ledger, dict) or set(ledger) != {'cycles', 'lastAttemptAt', 'slot', 'requests', 'settled'} or \
                    type(ledger['cycles']) is not int or not 0 <= ledger['cycles'] <= SCOPES[scope][1] or \
                    type(ledger['slot']) is not int or not -1 <= ledger['slot'] < SCOPES[scope][1] or \
                    not isinstance(ledger['requests'], dict) or set(ledger['requests']) != set(source_urls()) or \
                    any(type(count) is not int or not 0 <= count <= ledger['cycles'] for count in ledger['requests'].values()):
                raise ValueError()
            if ledger['cycles'] == 0:
                if ledger['lastAttemptAt'] is not None or ledger['slot'] != -1:
                    raise ValueError()
            else:
                stamp = _stamp(ledger['lastAttemptAt'])
                if stamp.date() != day or _slot(stamp, scope) != ledger['slot']:
                    raise ValueError()
            if not isinstance(ledger['settled'], dict) or len(ledger['settled']) > 1000:
                raise ValueError()
            for identifier, key in ledger['settled'].items():
                import re
                if not isinstance(identifier, str) or not re.fullmatch(r'fixture-[1-9][0-9]*', identifier) or \
                        not isinstance(key, list) or len(key) != 6 or key[0] not in source_urls() or \
                        not all(isinstance(item, str) and len(item) <= 160 for item in key) or \
                        not re.fullmatch(r'[A-Za-z0-9]{1,64}', key[1]) or _stamp(key[5]).date() != day:
                    raise ValueError()
        for provider in source_urls():
            charged = sum(item['requests'][provider] for item in value['scopes'].values())
            charged += value.get('legacy', {}).get('requests', 0) if provider == 'ole-opta' else 0
            if charged > sum(limit for _, limit in SCOPES.values()):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise MatchUpdatesError('invalid_result_budget') from None
    return value


def ledger_for_day(value, day):
    validate_ledger(value)
    if value is not None and value['date'] > day.isoformat():
        raise MatchUpdatesError('future_request_state')
    if value is not None and value['date'] == day.isoformat() and value['schemaVersion'] == 2:
        return copy.deepcopy(value)
    result = {'schemaVersion': 2, 'date': day.isoformat(), 'timezone': TIMEZONE,
              'scopes': {scope: _empty_scope() for scope in SCOPES}}
    if value is not None and value['date'] == day.isoformat():
        result['legacy'] = copy.deepcopy(value)
        # Existing finals keep their exact identity during the one-time legacy
        # migration. Old observations are not refreshed or treated as new I/O.
        for item in result['scopes'].values():
            item['settled'] = {identifier: ['ole-opta', *binding]
                               for identifier, binding in value['settled'].items()}
    return result


def reserve_cycle(value, day, now, scope):
    if scope not in SCOPES or now.tzinfo is None or now.astimezone(ZONE).date() != day:
        raise MatchUpdatesError('invalid_result_cycle')
    ledger = ledger_for_day(value, day)
    item = ledger['scopes'][scope]
    slot = _slot(now, scope)
    legacy = ledger.get('legacy')
    if legacy is not None and (slot <= _slot(_stamp(legacy['lastAttemptAt']), scope) or
            now - _stamp(legacy['lastAttemptAt']) < timedelta(minutes=4)):
        return None
    if item['cycles'] >= SCOPES[scope][1] or slot <= item['slot'] or \
            (item['lastAttemptAt'] is not None and now - _stamp(item['lastAttemptAt']) < timedelta(minutes=4)):
        return None
    item.update(cycles=item['cycles'] + 1, lastAttemptAt=_iso(now), slot=slot)
    return ledger


def _binding(entry):
    return [entry['source']['provider'], entry['source']['matchId'], *entry['fixtureKey']]


def scoped_pending(eligible, confirmed, day, now, guides, previous, ledger, scope, supported):
    selected, pending = {}, {}
    for identifier, event in eligible.items():
        if (identifier in confirmed) != (scope == 'confirmed'):
            continue
        selected[identifier] = event
        key = fixture_key(event, day, guides)
        if key[0] not in supported or now < _stamp(key[3]) - timedelta(minutes=10 if scope == 'confirmed' else 0):
            continue
        old = (previous or {}).get('matches', {}).get(identifier)
        final = (previous or {}).get('date') == day.isoformat() and old is not None and \
            old['fixture'] == _fixture(event) and old['fixtureKey'] == key and old['status'] == 'FINISHED'
        if final and old.get('homeScore') is not None and old.get('awayScore') is not None:
            if scope == 'informative' or ledger['scopes'][scope]['settled'].get(identifier) == _binding(old):
                continue
        pending[identifier] = event
    return selected, pending


def refresh_scope(root, output, day, now, scope, transport=None, state_output=None, source_inputs=None):
    if scope not in SCOPES or now.tzinfo is None or now.astimezone(ZONE).date() != day:
        raise MatchUpdatesError('invalid_result_cycle')
    root, output = Path(root), Path(output)
    now = now.astimezone(ZONE).replace(microsecond=0)
    feed = read_json(root / 'sports_today.json', optional=True)
    guides = validate_guides(read_json(root / 'sports_agenda_guides.json'))
    groups = load_clean_groups(root / 'mbox_sports_channels_clean.json', norm)
    window = read_json(root / 'sports_agenda_window.json', optional=True)
    daily = read_json(root / 'sports_broadcasts_today.json', optional=True)
    if daily is not None:
        validate_broadcasts(daily)
    previous = read_json(output, optional=True)
    if previous is not None:
        validate_updates(previous)
    state_output = Path(state_output or root / 'sports_match_updates_state.json')
    offline = source_inputs is not None
    state = None if offline else read_json(state_output, optional=True)
    validate_ledger(state)
    available = agenda_date(feed)
    report = {'scope': scope, 'eligible': 0, 'pending': 0, 'matched': 0,
              'requests': 0, 'sourceFailed': False, 'changed': False,
              'stateChanged': False, 'conflicts': 0, 'corroborated': 0,
              'sourcesSucceeded': '', 'sourcesFailed': ''}
    if available != day:
        report.update(agendaUnavailable=True, availableDate=available.isoformat() if available else 'missing')
        return previous, report
    ledger = ledger_for_day(state, day)
    eligible = eligible_events(feed, day, guides, groups, window, daily)
    confirmed = _confirmed_channel_events(feed, day, groups, window, daily)
    supported = source_competitions(guides)
    selected, pending = scoped_pending(eligible, confirmed, day, now, guides, previous, ledger,
                                       scope, set().union(*supported.values()))
    report.update(eligible=len(selected), pending=len(pending))
    providers = [provider for provider, competitions in supported.items() if
                 any(fixture_key(event, day, guides)[0] in competitions for event in pending.values())]
    if offline:
        if not isinstance(source_inputs, dict) or set(source_inputs) - set(source_urls()):
            raise MatchUpdatesError('invalid_source_inputs')
        providers = [provider for provider in supported if provider in source_inputs]
    elif providers:
        ledger = reserve_cycle(state, day, now, scope)
        if ledger is None:
            report['budgetSkipped'] = True
            return previous, report
        report['stateChanged'] = atomic_json(state_output, ledger)
    rows, succeeded, failed = [], [], []
    transport = transport or PublicResultsTransport()
    for provider in providers:
        if not offline:
            charged = sum(item['requests'][provider] for item in ledger['scopes'].values())
            charged += ledger.get('legacy', {}).get('requests', 0) if provider == 'ole-opta' else 0
            if charged >= sum(limit for _, limit in SCOPES.values()):
                report['budgetSkipped'] = True
                continue
            # Failed attempts count. Persist the reservation before I/O.
            ledger['scopes'][scope]['requests'][provider] += 1
            report['stateChanged'] = atomic_json(state_output, ledger) or report['stateChanged']
            report['requests'] += 1
        try:
            raw = source_inputs[provider] if offline else transport.fetch(provider, day)
            if isinstance(raw, Path):
                raw = raw.read_bytes()
            rows.extend(parse_provider(provider, raw, day, now))
            succeeded.append(provider)
        except (MatchUpdatesError, OSError, TimeoutError, UnicodeError, ValueError):
            failed.append(provider)
    comparison = compare_results(rows, guides, now, previous)
    report.update(conflicts=comparison.conflicts, corroborated=comparison.corroborated,
                  sourcesSucceeded=','.join(succeeded), sourcesFailed=','.join(failed),
                  sourceFailed=bool(failed))
    value, merged = merge_updates(feed, day, now,
        None if providers and not succeeded else comparison.rows, guides, groups,
        previous=previous, window=window, daily=daily, scope=scope)
    report['matched'] = merged['matched']
    if not offline and providers and succeeded and previous is not None and previous.get('date') == day.isoformat():
        index = {tuple(_source_key(row, guides)): row for row in comparison.rows}
        for identifier, event in selected.items():
            old = previous['matches'].get(identifier)
            row = index.get(tuple(fixture_key(event, day, guides)))
            if old is not None and row is not None and old['status'] == row.status == 'FINISHED' and \
                    row.home_score is not None and row.away_score is not None and \
                    old['source'] == {'provider': row.provider, 'matchId': row.match_id, 'url': row.source_url} and \
                    old['fixture'] == _fixture(event) and old['fixtureKey'] == fixture_key(event, day, guides) and \
                    (old.get('homeScore'), old.get('awayScore')) == (row.home_score, row.away_score) and now > _stamp(old['observedAt']):
                ledger['scopes'][scope]['settled'][identifier] = _binding(old)
        validate_ledger(ledger)
        report['stateChanged'] = atomic_json(state_output, ledger) or report['stateChanged']
    if value is not None and value != previous:
        report['changed'] = atomic_json(output, value)
    return value, report
