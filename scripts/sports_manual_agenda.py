"""Offline, date-scoped editorial additions to an unchanged automatic agenda.

Only public fixture metadata and references to an approved channel catalog are
accepted. Playback URLs, source credentials and inferred broadcasts never enter
the overlay. Callers own reading files, last-good configuration and publication.
"""
import copy
from datetime import date, datetime, timedelta
import json
import re
from urllib.parse import urlsplit

from . import generate_sports_today as generator
from .broadcaster_sources import ZONE, norm, public_url, safe_text
from .sports_agenda_window import validate_window
from .sports_fixture_identity import IdentityStore


TIMEZONE = 'America/Argentina/Buenos_Aires'
MANUAL_PROVIDER = 'manual_agenda'
MAX_ENTRIES = 500
MAX_CONFIG_BYTES = 1_000_000
MAX_CATALOG_ROWS = 50_000
MAX_INT = 2_147_483_647
_SECTIONS = frozenset(row[0] for row in generator.SECTIONS)
_DATE_METADATA = {'sourceDate', 'reviewedDate', 'supplementalSourceDate'}
_TEXT_METADATA = {'source', 'coverage'}


class ManualAgendaError(ValueError):
    """Fixed diagnostic codes; never interpolate user input or source bodies."""


def _fail(code):
    raise ManualAgendaError('MANUAL_' + code) from None


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        _fail('DATE_INVALID')
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail('DATE_INVALID')
    return value


def _text(value, *, limit=200, blank=False):
    if blank and value == '':
        return ''
    if not safe_text(value, limit) or not norm(value):
        _fail('TEXT_INVALID')
    return value.strip()


def _positive(value):
    if type(value) is not int or not 0 < value <= MAX_INT:
        _fail('ID_INVALID')
    return value


def _metadata(value, allowed):
    result = {}
    for field in allowed.intersection(value):
        if field in _DATE_METADATA:
            result[field] = _day(value[field])
        elif field in _TEXT_METADATA:
            result[field] = _text(value[field], limit=600)
        elif type(value[field]) is bool:
            result[field] = value[field]
        else:
            _fail('CATALOG_METADATA_INVALID')
    return result


def validate_manual_catalog(value):
    """Validate the sanitized catalog; numeric IDs must be unambiguous.

    Optional source/date/coverage metadata remains descriptive. It does not
    certify that the entire IPTV catalog is present or that a stream is live.
    """
    metadata = _DATE_METADATA | _TEXT_METADATA | {'serverSpecified'}
    if not isinstance(value, dict) or set(value) - {'schemaVersion', 'channels'} - metadata or \
            type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1 or \
            not isinstance(value.get('channels'), list) or len(value['channels']) > MAX_CATALOG_ROWS:
        _fail('CATALOG_INVALID')
    channels, seen = [], set()
    row_metadata = {'sourceDate', 'source', 'temporary'}
    for raw in value['channels']:
        if not isinstance(raw, dict) or not {'streamId', 'name', 'category'} <= set(raw) or \
                set(raw) - {'streamId', 'name', 'category', 'enabled'} - row_metadata:
            _fail('CATALOG_CHANNEL_INVALID')
        identifier = _positive(raw['streamId'])
        if identifier in seen:
            _fail('CATALOG_DUPLICATE_ID')
        seen.add(identifier)
        enabled = raw.get('enabled', True)
        if type(enabled) is not bool:
            _fail('CATALOG_CHANNEL_INVALID')
        channels.append({'streamId': identifier, 'name': _text(raw['name'], limit=600),
            'category': _text(raw['category'], limit=300, blank=True), 'enabled': enabled,
            **_metadata(raw, row_metadata)})
    return {'schemaVersion': 1, **_metadata(value, metadata), 'channels': channels}


def _channel_ids(value, channels):
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        _fail('CHANNEL_IDS_INVALID')
    result, seen = [], set()
    for raw in value:
        identifier = _positive(raw)
        if identifier in seen:
            _fail('DUPLICATE_CHANNEL_ID')
        seen.add(identifier)
        if identifier not in channels:
            _fail('UNKNOWN_CHANNEL_ID')
        if not channels[identifier]['enabled']:
            _fail('DISABLED_CHANNEL_ID')
        result.append(identifier)
    return result


def _logo(value):
    if value == '':
        return ''
    try:
        if not isinstance(value, str) or len(value) > 1024 or any(ord(c) <= 32 for c in value):
            raise ValueError()
        public_url(value)
        if not re.search(r'\.(?:png|webp|jpe?g)\Z', urlsplit(value).path, re.I):
            raise ValueError()
    except (TypeError, ValueError):
        _fail('LOGO_INVALID')
    return value


def _fixture_key(event):
    return tuple(norm(event[field]) for field in ('competition', 'homeTeam', 'awayTeam')) + (event['startTime'],)


def validate_manual_config(value, catalog):
    """Return a normalized, complete config or a safe validation error.

    An empty channel list deliberately makes a card informational. Unknown
    keys are rejected, including scores, status, venue and playback addresses.
    """
    catalog = validate_manual_catalog(catalog)
    channels = {row['streamId']: row for row in catalog['channels']}
    if not isinstance(value, dict) or set(value) != {'schemaVersion', 'timezone', 'events', 'channelOverrides'} or \
            type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1 or \
            value.get('timezone') != TIMEZONE or not isinstance(value.get('events'), list) or \
            not isinstance(value.get('channelOverrides'), list) or \
            len(value['events']) + len(value['channelOverrides']) > MAX_ENTRIES:
        _fail('CONFIG_INVALID')
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8'))
    except (TypeError, ValueError, UnicodeError):
        _fail('CONFIG_INVALID')
    if size > MAX_CONFIG_BYTES:
        _fail('CONFIG_TOO_LARGE')
    events, overrides, ids, fixtures, targets = [], [], set(), set(), set()
    required = {'id', 'date', 'sectionId', 'competition', 'homeTeam', 'awayTeam', 'startTime', 'channelIds'}
    for raw in value['events']:
        if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - {'homeLogo', 'awayLogo'}:
            _fail('EVENT_INVALID')
        identifier = raw['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', identifier):
            _fail('EVENT_ID_INVALID')
        day = _day(raw['date'])
        if (day, identifier) in ids:
            _fail('DUPLICATE_EVENT_ID')
        ids.add((day, identifier))
        if not isinstance(raw['sectionId'], str) or raw['sectionId'] not in _SECTIONS:
            _fail('SECTION_INVALID')
        if not isinstance(raw['startTime'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', raw['startTime']):
            _fail('KICKOFF_INVALID')
        event = {field: raw[field] for field in ('id', 'date', 'sectionId', 'startTime')}
        event.update({field: _text(raw[field]) for field in ('competition', 'homeTeam', 'awayTeam')})
        if norm(event['homeTeam']) == norm(event['awayTeam']):
            _fail('TEAMS_INVALID')
        event['channelIds'] = _channel_ids(raw['channelIds'], channels)
        for field in ('homeLogo', 'awayLogo'):
            if field in raw:
                event[field] = _logo(raw[field])
        key = (day, *_fixture_key(event))
        if key in fixtures:
            _fail('DUPLICATE_FIXTURE')
        fixtures.add(key)
        events.append(event)
    for raw in value['channelOverrides']:
        if not isinstance(raw, dict) or set(raw) != {'date', 'fixtureId', 'channelIds'}:
            _fail('OVERRIDE_INVALID')
        day, identifier = _day(raw['date']), _positive(raw['fixtureId'])
        if (day, identifier) in targets:
            _fail('DUPLICATE_OVERRIDE')
        targets.add((day, identifier))
        overrides.append({'date': day, 'fixtureId': identifier, 'channelIds': _channel_ids(raw['channelIds'], channels)})
    return {'schemaVersion': 1, 'timezone': TIMEZONE, 'events': events, 'channelOverrides': overrides}


def _references(identifiers, catalog):
    return [{'name': catalog[identifier]['name'], 'streamId': identifier, 'aliases': [],
             'priority': index, 'enabled': True} for index, identifier in enumerate(identifiers, 1)]


def apply_manual_agenda(window, config, catalog, identity, now):
    """Overlay only existing ART dates, preserving every automatic card.

    Previously materialized manual cards are removed before current entries are
    re-added, so deletion and expiration work without affecting automatic IDs.
    Missing override targets remain pending rather than creating a fixture.
    """
    config = validate_manual_config(config, catalog)
    catalog = {row['streamId']: row for row in validate_manual_catalog(catalog)['channels']}
    try:
        validate_window(window)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError()
        clock = now.astimezone(ZONE).replace(microsecond=0)
        if datetime.fromisoformat(window['generatedAt']) > clock:
            raise ValueError()
        store = IdentityStore(copy.deepcopy(identity))
    except (TypeError, ValueError, KeyError):
        _fail('WINDOW_OR_IDENTITY_INVALID')
    result = copy.deepcopy(window)
    report = {'eventsAdded': 0, 'eventsRemoved': 0, 'channelOverridesApplied': 0,
              'inactiveEvents': 0, 'pendingEvents': 0, 'pendingOverrides': 0,
              'warnings': [], 'publicRequests': 0, 'sportsApiRequests': 0}
    today = clock.date()
    active_dates = {(today + timedelta(days=offset)).isoformat() for offset in range(3)}
    result['days'] = {key: feed for key, feed in result['days'].items() if key in active_dates}
    if today.isoformat() not in result['days']:
        _fail('CURRENT_DATE_NOT_IN_WINDOW')
    manual_ids = {f'fixture-{identifier}' for key, identifier in store.value['entries'].items()
                  if key.startswith(MANUAL_PROVIDER + ':')}
    for feed in result['days'].values():
        for section in feed['sections']:
            previous = section['events']
            section['events'] = [event for event in previous if event['id'] not in manual_ids]
            report['eventsRemoved'] += len(previous) - len(section['events'])
    for entry in sorted(config['events'], key=lambda row: (row['date'], row['startTime'], row['id'])):
        if entry['date'] not in active_dates:
            report['inactiveEvents'] += 1
            continue
        feed = result['days'].get(entry['date'])
        if feed is None:
            report['pendingEvents'] += 1
            report['warnings'].append('MANUAL_DATE_NOT_IN_WINDOW')
            continue
        if any(_fixture_key(event) == _fixture_key(entry) for section in feed['sections'] for event in section['events']):
            report['warnings'].append('MANUAL_DUPLICATE_USE_OVERRIDE')
            continue
        provider_id = entry['date'] + ':' + entry['id']
        if MANUAL_PROVIDER + ':' + provider_id in store.value.get('reconciliations', {}):
            _fail('IDENTITY_RECONCILIATION_FORBIDDEN')
        try:
            identifier = store.resolve(MANUAL_PROVIDER, provider_id)
        except ValueError:
            _fail('IDENTITY_COLLISION')
        event = {'id': f'fixture-{identifier}', 'sport': 'football',
                 **{field: entry[field] for field in ('competition', 'homeTeam', 'awayTeam', 'startTime')},
                 'homeLogo': entry.get('homeLogo', ''), 'awayLogo': entry.get('awayLogo', ''),
                 'status': 'SCHEDULED', 'channels': _references(entry['channelIds'], catalog)}
        section = next((section for section in feed['sections'] if section['id'] == entry['sectionId']), None)
        if section is None:
            report['pendingEvents'] += 1
            report['warnings'].append('MANUAL_SECTION_NOT_IN_FEED')
            continue
        position = next((index for index, item in enumerate(section['events'])
                         if item['startTime'] and item['startTime'] > event['startTime']), len(section['events']))
        section['events'].insert(position, event)
        report['eventsAdded'] += 1
    for entry in config['channelOverrides']:
        if entry['date'] not in active_dates:
            continue
        feed = result['days'].get(entry['date'])
        event = next((event for section in (feed or {}).get('sections', []) for event in section['events']
                      if event['id'] == f"fixture-{entry['fixtureId']}"), None)
        if event is None:
            report['pendingOverrides'] += 1
            report['warnings'].append('MANUAL_OVERRIDE_TARGET_PENDING')
            continue
        event['channels'] = _references(entry['channelIds'], catalog)
        report['channelOverridesApplied'] += 1
    for key, feed in result['days'].items():
        original = window['days'][key]
        if {k: v for k, v in feed.items() if k != 'updatedAt'} != {k: v for k, v in original.items() if k != 'updatedAt'}:
            feed['updatedAt'] = clock.isoformat(timespec='seconds')
    if result['days'] != window['days']:
        result['generatedAt'] = clock.isoformat(timespec='seconds')
    report['warnings'] = list(dict.fromkeys(report['warnings']))
    try:
        validate_window(result)
        IdentityStore(store.value)
    except (ValueError, TypeError, KeyError):
        _fail('RESULT_INVALID')
    return result, store.value, report
