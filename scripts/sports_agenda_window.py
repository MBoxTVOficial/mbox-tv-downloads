"""Three ART dates prepared from one public-source acquisition, never three budgets."""
import copy
import json
from datetime import date, timedelta

from . import generate_sports_today as generator
from . import official_football_bot as bot
from .broadcaster_sources import instant
from .prioritized_agendas import pair_key
from .sports_fixture_identity import IdentityStore

TIMEZONE = 'America/Argentina/Buenos_Aires'
WINDOW_DAYS = 3
MAX_WINDOW_BYTES = 3 * 1024 * 1024


def validate_window(value):
    if not isinstance(value, dict) or set(value) != {'schemaVersion', 'timezone', 'generatedAt', 'days'} or \
            type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1 or value['timezone'] != TIMEZONE:
        raise ValueError('Invalid agenda window')
    generated = instant(value['generatedAt'])
    if not isinstance(value['days'], dict) or not 1 <= len(value['days']) <= WINDOW_DAYS:
        raise ValueError('Invalid agenda window dates')
    if len((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8')) > MAX_WINDOW_BYTES:
        raise ValueError('Agenda window exceeds byte limit')
    supported = {(generated.date() + timedelta(days=offset)).isoformat() for offset in range(WINDOW_DAYS)}
    for key, feed in value['days'].items():
        if key not in supported or not isinstance(feed, dict) or feed.get('date') != key or feed.get('timezone') != TIMEZONE:
            raise ValueError('Agenda window date mismatch')
        generator.validate_feed(feed)
        if feed.get('sportsDataMode') != 'DEGRADED' or instant(feed['updatedAt']) > generated:
            raise ValueError('Agenda window metadata mismatch')
        for section in feed['sections']:
            for event in section['events']:
                if event['status'] != 'SCHEDULED' or 'homeScore' in event or 'awayScore' in event:
                    raise ValueError('Agenda-only window cannot assert scores or match states')
    return value


def read_previous_window(value):
    if value is None:
        return None
    return validate_window(value)


def _events(feed):
    return [event for section in (feed or {}).get('sections', []) for event in section['events']]


def _sanitize_cached_channels(feed, groups):
    """Keep only cached stream identities still enabled now; never add alternatives."""
    enabled = {stream['streamId'] for group in groups.values() for stream in group['streams']
               if stream['enabled']}
    for event in _events(feed):
        if 'channels' not in event:
            continue
        retained, seen = [], set()
        for channel in event['channels']:
            identifier = channel.get('streamId')
            if identifier in enabled and channel.get('enabled', True) and identifier not in seen:
                seen.add(identifier)
                retained.append({**channel, 'priority': len(retained) + 1})
        if retained:
            event['channels'] = retained
        else:
            event.pop('channels', None)


def build_agenda_window(day, now, today, config, guides, groups, mapping, identity, report,
                        previous_window=None, reconcile=None, daily=None):
    """Reuse only the exact prior date; empty future coverage is omitted, not invented.

    Future updatedAt uses the real now. Playback day guards are deferred to the
    client; confirmed options must already exist at midnight for a later T-10 gate.
    """
    now = instant(now.isoformat())
    if now.date() != day or today['date'] != day.isoformat():
        raise ValueError('Agenda window current date mismatch')
    previous_window = read_previous_window(previous_window)
    window = {'schemaVersion': 1, 'timezone': TIMEZONE,
              'generatedAt': now.isoformat(timespec='seconds'),
              'days': {day.isoformat(): copy.deepcopy(today)}}
    identity = copy.deepcopy(identity)
    diagnostics = []
    for offset in range(1, WINDOW_DAYS):
        target = day + timedelta(days=offset)
        key = target.isoformat()
        old = copy.deepcopy((previous_window or {}).get('days', {}).get(key))
        original_old = copy.deepcopy(old)
        _sanitize_cached_channels(old, groups)
        manual_ids = {f"fixture-{item['fixtureId']}" for item in (daily or {}).get('broadcasts', [])
            if daily['date'] == key and item.get('source', 'manual') == 'manual' and item['confidence'] == 'confirmed'}
        for event in _events(old):
            if event['competition'] in report.get('blockedCompetitions', ()) and event['id'] not in manual_ids:
                event.pop('channels', None)  # A new explicit conflict invalidates old TV evidence.
        rows = report.get('windowRows', {}).get(key, [])
        records = report.get('windowRecords', {}).get(key, [])
        state = 'OMITTED_NO_COVERAGE'
        if rows:
            try:
                candidate_identity = reconcile(rows, old, identity, guides, report, target) if reconcile else identity
                feed, _, details, candidate_identity = bot.preview(rows, target, now, config, groups, mapping,
                    identity=candidate_identity, daily=daily if daily and daily['date'] == key else None,
                    guide_records=records, guide_config=guides,
                    blocked_competitions=report.get('blockedCompetitions', ()))
                incoming = _events(feed)
                for previous_event in _events(old):
                    if previous_event['id'] in manual_ids:
                        continue  # Explicit operator assignments retain their existing priority.
                    if any(detail['source'] in {'lpf_official', 'afa_official', 'conmebol_official'} and
                           pair_key(detail, guides) == pair_key(previous_event, guides) and
                           instant(detail['startTime']).date() == target and
                           instant(detail['startTime']).strftime('%H:%M') != previous_event['startTime']
                           for detail in details):
                        # A confirmed reschedule is a contradiction, not a coverage gap.
                        # New kickoff/identity needs its own TV evidence; never copy old TV.
                        previous_event.pop('channels', None)
                # A removed official identity with confirmed channels requires exact
                # reconciliation. Never guess the replacement based on team substrings.
                identifiers = {event['id'] for event in incoming}
                if manual_ids - identifiers:
                    state = 'KEEP_LAST_GOOD_IDENTITY'
                elif any(event.get('channels') and event['id'] not in identifiers for event in _events(old)):
                    state = 'KEEP_LAST_GOOD_IDENTITY'
                elif incoming:
                    failures = report.get('sources', []) + report.get('windowGuides', {}).get(key, [])
                    if any(source.get('state') in {'ERROR', 'ERROR_USING_CACHE', 'DEFERRED_BUDGET', 'OFFLINE_MISS'} for source in failures):
                        previous_events = {event['id']: event for event in _events(old)}
                        decisions = {f"fixture-{item['fixtureId']}": item for item in details}
                        for event in incoming:
                            prior = previous_events.get(event['id'])
                            if prior and prior.get('channels') and not event.get('channels') and \
                                    decisions[event['id']]['state'] == 'UNRESOLVED' and \
                                    event['competition'] not in report.get('blockedCompetitions', ()) and \
                                    event['startTime'] == prior['startTime'] and pair_key(event, guides) == pair_key(prior, guides):
                                event['channels'] = copy.deepcopy(prior['channels'])
                    generator.validate_feed(feed)
                    window['days'][key] = feed
                    identity, old, state = candidate_identity, None, 'GENERATED'
            except (ValueError, KeyError, TypeError):
                state = 'KEEP_LAST_GOOD_INVALID_SOURCE' if old else 'OMITTED_INVALID_SOURCE'
        if old is not None:
            if old != original_old:
                # Retiring TV is a real content update. Publish the real current
                # timestamp so date-scoped client merges accept the withdrawal.
                old['updatedAt'] = now.isoformat(timespec='seconds')
            window['days'][key] = old
            if state == 'OMITTED_NO_COVERAGE':
                state = 'KEEP_LAST_GOOD_NO_COVERAGE'
        diagnostics.append({'date': key, 'state': state,
                            'events': len(_events(window['days'].get(key)))})
    IdentityStore(identity)
    validate_window(window)
    return window, identity, diagnostics
