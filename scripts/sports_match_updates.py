"""A bounded public match-state overlay; never changes the agenda or TV evidence.

Olé's anonymous Opta bulk endpoint was checked on 2026-11-13. The two numeric
featured categories come from agenda related.mams.category (941 Nations League,
88 national friendlies). They are a request hint, not proof of a competition:
the response can contain other competitions, which are filtered independently.
"""
import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from http.client import HTTPException
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .broadcaster_sources import NoRedirect, ZONE, instant, safe_text
from .prioritized_agendas import canonical_competition, team_key

TIMEZONE = 'America/Argentina/Buenos_Aires'
SOURCE_URL = 'https://www.ole.com.ar/api/opta/sdapi/futbol/all-tournament-calendar'
MAX_BODY = 2_000_000
TIMEOUT_SECONDS = 8
MAX_REQUESTS = 2
LIVE_TTL = timedelta(minutes=15)
MAX_DAILY_REQUESTS = 120
MIN_REQUEST_INTERVAL = timedelta(minutes=4)
FINAL_CATCHUP_WINDOW = timedelta(hours=8)
# Exact Opta competition IDs and names verified in the public bulk response.
COMPETITIONS = {
    'cesdwwnxbc5fmajgroc0hqzy2': ('Friendlies', 'Amistosos de selecciones'),
    '595nsvo7ykvoe690b1e4u5n56': ('UEFA Nations League', 'UEFA Nations League'),
}


class MatchUpdatesError(ValueError):
    """Safe diagnostics: never include remote bodies, headers or user config."""


def _stamp(value):
    try:
        return instant(value)
    except (TypeError, ValueError):
        raise MatchUpdatesError('invalid_timestamp') from None


def _iso(value):
    return value.astimezone(ZONE).isoformat(timespec='seconds')


def decode_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise MatchUpdatesError('invalid_json_size')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise MatchUpdatesError('invalid_json') from None


def read_json(path, optional=False):
    try:
        return decode_json(Path(path).read_bytes())
    except FileNotFoundError:
        if optional:
            return None
        raise MatchUpdatesError('missing_input') from None
    except OSError:
        raise MatchUpdatesError('unreadable_input') from None


def request_body(day):
    if type(day) is not date:
        raise MatchUpdatesError('invalid_date')
    return {'date': day.isoformat(), 'status': 'all',
            'featuredCompetitions': [{'id': 941}, {'id': 88}],
            'pgSz': 50, 'country': 'argentina'}


class OleBulkTransport:
    """No retries/redirects, <=2 grouped requests, 8s wall-clock, 2MB body."""
    def __init__(self, timeout=TIMEOUT_SECONDS, max_requests=1):
        if type(timeout) is not int or not 1 <= timeout <= TIMEOUT_SECONDS or \
                type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise MatchUpdatesError('invalid_transport_limits')
        self.timeout, self.max_requests, self.requests = timeout, max_requests, 0

    def fetch(self, day):
        if self.requests >= self.max_requests:
            raise MatchUpdatesError('request_budget_exhausted')
        self.requests += 1
        # A subprocess also bounds DNS, TLS and a server sending a slow trickle.
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'scripts.sports_match_updates', '--fetch-source',
                 day.isoformat(), str(self.timeout)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=self.timeout, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise MatchUpdatesError('source_unavailable') from None
        if result.returncode != 0:
            raise MatchUpdatesError('source_unavailable')
        return decode_json(result.stdout)


def _fetch_source(day, timeout):
    request = Request(SOURCE_URL, data=json.dumps(request_body(day)).encode('utf8'),
        headers={'User-Agent': 'Mozilla/5.0 (compatible; MBoxSportsMatchUpdates/1.0)',
                 'Accept': 'application/json', 'Content-Type': 'application/json'}, method='POST')
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise MatchUpdatesError('source_unavailable')
            raw = response.read(MAX_BODY + 1)
        decode_json(raw)
        return raw
    except HTTPError as error:
        error.close()
        raise MatchUpdatesError('source_unavailable') from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise MatchUpdatesError('source_unavailable') from None


@dataclass(frozen=True)
class SourceMatch:
    match_id: str
    competition: str
    home: str
    away: str
    kickoff: datetime
    status: str
    source_updated: datetime | None
    home_score: int | None = None
    away_score: int | None = None


def parse_ole_bulk(payload, day):
    """Read the verified SDAPI shape. Unknown status never becomes LIVE by time."""
    if type(day) is not date or not isinstance(payload, dict) or \
            payload.get('code') != 200 or not isinstance(payload.get('data'), dict) or \
            not isinstance(payload['data'].get('fixture'), list):
        raise MatchUpdatesError('unrecognized_source_payload')
    groups = payload['data']['fixture']
    if len(groups) > 500:
        raise MatchUpdatesError('source_record_limit')
    parsed, duplicates = {}, set()
    count = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('match'), list):
            raise MatchUpdatesError('unrecognized_source_payload')
        for row in group['match']:
            count += 1
            if count > 1000:
                raise MatchUpdatesError('source_record_limit')
            # A malformed row must not discard independently valid matches.
            try:
                info = row['matchInfo']
                details = row['liveData']['matchDetails']
                source_status = details.get('matchStatus')
                status = {'Playing': 'LIVE', 'Played': 'FINISHED'}.get(source_status)
                if status is None:
                    continue
                competition = info['competition']
                verified = COMPETITIONS.get(competition['id'])
                if verified is None or competition.get('name') != verified[0]:
                    continue
                identifier = info['id']
                if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}', identifier):
                    continue
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}Z', info['date']) or \
                        not re.fullmatch(r'\d{2}:\d{2}:\d{2}Z', info['time']):
                    continue
                kickoff = _stamp(info['date'][:-1] + 'T' + info['time'])
                if kickoff.date() != day:
                    continue
                teams = info['contestant']
                if not isinstance(teams, list) or len(teams) != 2:
                    continue
                by_side = {item['position']: item['name'] for item in teams}
                if set(by_side) != {'home', 'away'} or not all(safe_text(x, 100) for x in by_side.values()):
                    continue
                updated = _stamp(info['lastUpdated']) if info.get('lastUpdated') is not None else None
                total = details.get('scores', {}).get('total', {})
                home_score, away_score = total.get('home'), total.get('away')
                if not all(type(x) is int and 0 <= x <= 999 for x in (home_score, away_score)):
                    home_score = away_score = None
                value = SourceMatch(identifier, verified[1], by_side['home'], by_side['away'],
                                    kickoff, status, updated, home_score, away_score)
            except (KeyError, TypeError, AttributeError, ValueError):
                continue
            if identifier in parsed and parsed[identifier] != value:
                duplicates.add(identifier)
            parsed[identifier] = value
    return [value for identifier, value in parsed.items() if identifier not in duplicates]


def _fixture(event):
    return {field: event[field] for field in ('competition', 'homeTeam', 'awayTeam', 'startTime')}


def fixture_key(event, day, guides):
    try:
        competition = canonical_competition(event['competition'], guides)
        if competition is None or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', event['startTime']):
            raise ValueError()
        return [competition, team_key(event['homeTeam'], competition, guides),
                team_key(event['awayTeam'], competition, guides),
                f"{day.isoformat()}T{event['startTime']}:00-03:00"]
    except (KeyError, TypeError, ValueError):
        raise MatchUpdatesError('invalid_fixture_identity') from None


def _source_key(row, guides):
    return [row.competition, team_key(row.home, row.competition, guides),
            team_key(row.away, row.competition, guides), _iso(row.kickoff)]


def _events(feed):
    if not isinstance(feed, dict) or feed.get('timezone') != TIMEZONE or not isinstance(feed.get('sections'), list):
        raise MatchUpdatesError('invalid_agenda')
    events = {}
    try:
        for section in feed['sections']:
            for event in section['events']:
                identifier = event['id']
                if not isinstance(identifier, str) or not re.fullmatch(r'fixture-[1-9][0-9]*', identifier) or identifier in events:
                    raise ValueError()
                if event.get('sport') == 'football':
                    if not all(safe_text(event[field], 100) for field in ('competition', 'homeTeam', 'awayTeam', 'startTime')):
                        raise ValueError()
                    events[identifier] = event
    except (KeyError, TypeError, ValueError):
        raise MatchUpdatesError('invalid_agenda') from None
    return events


def agenda_date(feed):
    """A missing daily publication is normal around the ART day boundary."""
    if feed is None:
        return None
    _events(feed)
    try:
        if not isinstance(feed.get('date'), str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', feed['date']):
            raise ValueError()
        return date.fromisoformat(feed['date'])
    except (TypeError, ValueError):
        raise MatchUpdatesError('invalid_agenda_date') from None


def eligible_events(feed, day, guides, groups, window=None, daily=None):
    """A confirmed existing stream is eligibility evidence, never a new assignment."""
    if feed.get('date') != day.isoformat():
        raise MatchUpdatesError('agenda_date_mismatch')
    events = _events(feed)
    enabled = {stream['streamId'] for group in groups.values() for stream in group['streams'] if stream['enabled']}
    def has_channel(event):
        return any(isinstance(channel, dict) and channel.get('enabled', True) is True and
                   type(channel.get('streamId')) is int and channel['streamId'] in enabled
                   for channel in event.get('channels', []))
    retained = {}
    if isinstance(window, dict) and window.get('timezone') == TIMEZONE:
        cached = window.get('days', {}).get(day.isoformat())
        if isinstance(cached, dict) and cached.get('date') == day.isoformat():
            retained = _events(cached)
    assigned = set()
    if isinstance(daily, dict) and daily.get('date') == day.isoformat() and daily.get('timezone') == TIMEZONE:
        for assignment in daily.get('broadcasts', []):
            if assignment.get('confidence') == 'confirmed' and any(
                    identifier in groups and any(stream['enabled'] for stream in groups[identifier]['streams'])
                    for identifier in assignment.get('channelGroups', [])):
                assigned.add(f"fixture-{assignment['fixtureId']}")
    result = {}
    for identifier, event in events.items():
        old = retained.get(identifier)
        same = old is not None and _fixture(old) == _fixture(event)
        if has_channel(event) or (same and has_channel(old)) or identifier in assigned:
            fixture_key(event, day, guides)
            result[identifier] = event
    return result


def pending_events(eligible, day, now, guides, previous=None, state=None):
    """The clock only gates network work; it never asserts a match's state."""
    supported = {value[1] for value in COMPETITIONS.values()}
    result = {}
    for identifier, event in eligible.items():
        key = fixture_key(event, day, guides)
        start = _stamp(key[3])
        # A delayed scheduled job must still retrieve an unobserved final. This
        # changes request eligibility only, never the app's three-hour launch guard.
        if key[0] not in supported or not start - timedelta(minutes=10) <= now <= start + FINAL_CATCHUP_WINDOW:
            continue
        old = (previous or {}).get('matches', {}).get(identifier)
        final = (previous or {}).get('date') == day.isoformat() and old is not None and \
            old.get('fixture') == _fixture(event) and old.get('fixtureKey') == key and old.get('status') == 'FINISHED'
        settled = final and (state or {}).get('date') == day.isoformat() and \
            (state or {}).get('settled', {}).get(identifier) == [old['source']['matchId'], *key]
        if not settled:
            result[identifier] = event
    return result


def validate_request_state(previous):
    if previous is not None:
        try:
            if not isinstance(previous, dict) or set(previous) != {'schemaVersion', 'date', 'timezone', 'requests', 'lastAttemptAt', 'settled'} or \
                    type(previous['schemaVersion']) is not int or previous['schemaVersion'] != 1 or \
                    previous['timezone'] != TIMEZONE or type(previous['requests']) is not int or \
                    not 1 <= previous['requests'] <= MAX_DAILY_REQUESTS or \
                    date.fromisoformat(previous['date']) != _stamp(previous['lastAttemptAt']).date() or \
                    not isinstance(previous['settled'], dict) or len(previous['settled']) > 500:
                raise ValueError()
            for identifier, binding in previous['settled'].items():
                if not re.fullmatch(r'fixture-[1-9][0-9]*', identifier) or not isinstance(binding, list) or len(binding) != 5 or \
                        not all(isinstance(x, str) and len(x) <= 160 for x in binding) or \
                        not re.fullmatch(r'[A-Za-z0-9]{1,64}', binding[0]) or _stamp(binding[4]).date().isoformat() != previous['date']:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise MatchUpdatesError('invalid_request_state') from None
    return previous


def reserve_request(previous, day, now):
    """A separate persisted counter counts failed attempts as well as successes."""
    if now.tzinfo is None or now.astimezone(ZONE).date() != day:
        raise MatchUpdatesError('current_art_date_required')
    validate_request_state(previous)
    if previous is not None and previous['date'] > day.isoformat():
        raise MatchUpdatesError('future_request_state')
    same_day = previous is not None and previous['date'] == day.isoformat()
    count = previous['requests'] if same_day else 0
    if same_day and (count >= MAX_DAILY_REQUESTS or now - _stamp(previous['lastAttemptAt']) < MIN_REQUEST_INTERVAL):
        return None
    return {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': TIMEZONE,
            'requests': count + 1, 'lastAttemptAt': _iso(now),
            'settled': copy.deepcopy(previous['settled']) if same_day else {}}


def confirm_finished(state, previous, rows, eligible, guides, day, now):
    """One later source observation confirms a final latch without restamping it."""
    state = copy.deepcopy(state)
    if previous is None or previous.get('date') != day.isoformat() or rows is None:
        return state
    for identifier, event in eligible.items():
        old = previous['matches'].get(identifier)
        key = fixture_key(event, day, guides)
        if old is None or old['status'] != 'FINISHED' or old['fixtureKey'] != key or old['fixture'] != _fixture(event):
            continue
        candidates = [row for row in rows if _source_key(row, guides) == key]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        if row.status != 'FINISHED' or row.match_id != old['source']['matchId'] or now <= _stamp(old['observedAt']):
            continue
        old_updated = _stamp(old['sourceUpdatedAt']) if 'sourceUpdatedAt' in old else None
        if (old_updated is not None and (row.source_updated is None or row.source_updated < old_updated)) or \
                (row.source_updated is not None and row.source_updated > now):
            continue
        state['settled'][identifier] = [row.match_id, *key]
    return state


def validate_updates(value):
    if not isinstance(value, dict) or set(value) != {'schemaVersion', 'date', 'timezone', 'updatedAt', 'matches'} or \
            type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1 or \
            value.get('timezone') != TIMEZONE or not isinstance(value.get('matches'), dict):
        raise MatchUpdatesError('invalid_updates')
    try:
        day = date.fromisoformat(value['date'])
        if _stamp(value['updatedAt']).date() != day:
            raise ValueError()
        for identifier, entry in value['matches'].items():
            required = {'status', 'fixture', 'fixtureKey', 'observedAt', 'expiresAt', 'source'}
            if not re.fullmatch(r'fixture-[1-9][0-9]*', identifier) or not isinstance(entry, dict) or \
                    not required <= set(entry) or set(entry) - required - {'homeScore', 'awayScore', 'sourceUpdatedAt'} or \
                    entry['status'] not in ('LIVE', 'FINISHED'):
                raise ValueError()
            scores = {'homeScore', 'awayScore'} & set(entry)
            if scores and (len(scores) != 2 or any(type(entry[key]) is not int or not 0 <= entry[key] <= 999 for key in scores)):
                raise ValueError()
            if not isinstance(entry['fixture'], dict) or set(entry['fixture']) != {'competition', 'homeTeam', 'awayTeam', 'startTime'} or \
                    not all(safe_text(x, 100) for x in entry['fixture'].values()) or \
                    not isinstance(entry['fixtureKey'], list) or len(entry['fixtureKey']) != 4:
                raise ValueError()
            observed, expiry = _stamp(entry['observedAt']), _stamp(entry['expiresAt'])
            if observed.date() != day or expiry <= observed or _stamp(entry['fixtureKey'][3]).date() != day:
                raise ValueError()
            if entry['status'] == 'LIVE' and expiry != observed + LIVE_TTL:
                raise ValueError()
            if entry['status'] == 'FINISHED' and expiry != datetime.combine(day + timedelta(days=1), time(), ZONE):
                raise ValueError()
            if 'sourceUpdatedAt' in entry and _stamp(entry['sourceUpdatedAt']) > observed:
                raise ValueError()
            source = entry['source']
            if not isinstance(source, dict) or set(source) != {'provider', 'matchId', 'url'} or \
                    source['provider'] != 'ole-opta' or source['url'] != SOURCE_URL or \
                    not isinstance(source['matchId'], str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}', source['matchId']):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise MatchUpdatesError('invalid_updates') from None
    return value


def merge_updates(feed, day, now, rows, guides, groups, previous=None, window=None, daily=None):
    """rows=None means acquisition failed: retain last good metadata unchanged."""
    if now.tzinfo is None or now.astimezone(ZONE).date() != day:
        raise MatchUpdatesError('current_art_date_required')
    now = now.astimezone(ZONE).replace(microsecond=0)
    if previous is not None:
        validate_updates(previous)
    eligible = eligible_events(feed, day, guides, groups, window, daily)
    old_current = previous is not None and previous['date'] == day.isoformat()
    retained = {}
    if old_current:
        for identifier, entry in previous['matches'].items():
            event = eligible.get(identifier)
            if event is not None and entry['fixture'] == _fixture(event) and entry['fixtureKey'] == fixture_key(event, day, guides):
                retained[identifier] = copy.deepcopy(entry)
    if rows is None and (not old_current or retained == previous['matches']):
        return copy.deepcopy(previous), {'eligible': len(eligible), 'matched': 0, 'sourceFailed': True}
    matches, index, ambiguous = retained, {}, set()
    for row in rows or []:
        key = tuple(_source_key(row, guides))
        if key in index and index[key] != row:
            ambiguous.add(key)
        index[key] = row
    matched = 0
    for identifier, event in eligible.items():
        key = fixture_key(event, day, guides)
        row = index.get(tuple(key))
        if row is None or tuple(key) in ambiguous or row.kickoff > now or \
                (row.source_updated is not None and row.source_updated > now):
            continue
        if row.status == 'LIVE' and row.source_updated is not None and now - row.source_updated > LIVE_TTL:
            continue
        previous_entry = matches.get(identifier)
        if previous_entry is not None:
            if previous_entry['source']['matchId'] != row.match_id:
                continue  # A verified persisted raw identity cannot silently move.
            if previous_entry['status'] == 'FINISHED' and row.status != 'FINISHED':
                continue
            old_observed = _stamp(previous_entry['observedAt'])
            old_updated = _stamp(previous_entry['sourceUpdatedAt']) if 'sourceUpdatedAt' in previous_entry else None
            if now < old_observed or (old_updated is not None and (row.source_updated is None or row.source_updated < old_updated)):
                continue
            # Re-reading the same snapshot must not extend a stale LIVE label.
            if old_updated is not None and row.source_updated == old_updated:
                continue
            if old_updated is None and row.source_updated is None and \
                    previous_entry['status'] == row.status and \
                    previous_entry.get('homeScore') == row.home_score and previous_entry.get('awayScore') == row.away_score:
                continue
        expires = now + LIVE_TTL if row.status == 'LIVE' else datetime.combine(day + timedelta(days=1), time(), ZONE)
        entry = {'status': row.status, 'fixture': _fixture(event), 'fixtureKey': key,
                 'observedAt': _iso(now), 'expiresAt': _iso(expires),
                 'source': {'provider': 'ole-opta', 'matchId': row.match_id, 'url': SOURCE_URL}}
        if row.source_updated is not None:
            entry['sourceUpdatedAt'] = _iso(row.source_updated)
        if row.home_score is not None and row.away_score is not None:
            entry.update(homeScore=row.home_score, awayScore=row.away_score)
        matches[identifier] = entry
        matched += 1
    if old_current and matches == previous['matches']:
        return copy.deepcopy(previous), {'eligible': len(eligible), 'matched': matched, 'sourceFailed': rows is None}
    value = {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': TIMEZONE,
             'updatedAt': _iso(now), 'matches': matches}
    validate_updates(value)
    return value, {'eligible': len(eligible), 'matched': matched, 'sourceFailed': rows is None}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--fetch-source':
            raise MatchUpdatesError('invalid_worker_arguments')
        timeout = int(sys.argv[3])
        if not 1 <= timeout <= TIMEOUT_SECONDS:
            raise MatchUpdatesError('invalid_transport_limits')
        sys.stdout.buffer.write(_fetch_source(date.fromisoformat(sys.argv[2]), timeout))
    except (MatchUpdatesError, ValueError):
        sys.exit(1)
