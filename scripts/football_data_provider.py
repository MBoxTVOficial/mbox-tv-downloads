"""Read-only football-data.org adapter. Whitelisted agenda, no score/state output."""
from datetime import datetime, timedelta, timezone
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from .sports_auxiliary_policy import local_start

COMPETITIONS = {'PD': 'LaLiga', 'PL': 'Premier League', 'SA': 'Serie A',
                'FL1': 'Ligue 1', 'CL': 'UEFA Champions League'}


class ProviderError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def normalize_matches(payload, code, day):
    if code not in COMPETITIONS:
        raise ProviderError('INVALID_COMPETITION')
    if not isinstance(payload, dict) or not isinstance(payload.get('matches'), list):
        raise ProviderError('INVALID_RESPONSE')
    rows, seen = [], set()
    for match in payload['matches']:
        try:
            if match['competition']['code'] != code:
                raise ValueError()
            identifier = match['id']
            if type(identifier) is not int or identifier <= 0:
                raise ValueError()
            start = local_start(match['utcDate'])
            home, away = match['homeTeam']['name'], match['awayTeam']['name']
            if not all(isinstance(name, str) and name.strip() for name in (home, away)):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ProviderError('INVALID_MATCH') from None
        if start.date() != day:
            continue
        row = {'competition': COMPETITIONS[code], 'competitionId': code,
               'providerFixtureId': f'football-data:{identifier}',
               'homeTeam': home, 'awayTeam': away, 'startTime': start.isoformat()}
        if identifier in seen:
            if row not in rows:
                raise ProviderError('CONFLICTING_DUPLICATE')
            continue
        seen.add(identifier)
        rows.append(row)
    return rows


class FootballDataProvider:
    """Budget independent of API-Football. Sequential, no retry, no redirects.

    Conservative application caps: six requests/execution, eight/minute. These
    are not assertions about the authenticated account's plan. Real headers may
    reduce the budget; they never increase it. Cache holds safe normalized data.
    """
    def __init__(self, token, fetch=None, sleep=time.sleep, clock=time.monotonic):
        if not isinstance(token, str) or not token.strip():
            raise ProviderError('TOKEN_NOT_AVAILABLE')
        self._token = token
        self._fetch = fetch or self._http
        self._sleep, self._clock = sleep, clock
        self.requests = 0
        self._last = None
        self.cache = {}
        self.remaining = None

    def _http(self, path):
        request = Request('https://api.football-data.org/v4/' + path,
                          headers={'X-Auth-Token': self._token, 'Accept': 'application/json'})
        try:
            with build_opener(NoRedirects()).open(request, timeout=8) as response:
                raw = response.read(4_000_001)
                headers = dict(response.headers)
            if len(raw) > 4_000_000:
                raise ProviderError('RESPONSE_TOO_LARGE')
            return json.loads(raw), headers
        except HTTPError as error:
            raise ProviderError(f'HTTP_{error.code}') from None
        except (OSError, ValueError):
            raise ProviderError('REQUEST_FAILED') from None

    def _get(self, path):
        if self.requests >= 6 or self.remaining == 0:
            raise ProviderError('REQUEST_BUDGET_EXHAUSTED')
        if self._last is not None:
            self._sleep(max(0, 7.5 - (self._clock() - self._last)))
        self.requests += 1
        self._last = self._clock()
        try:
            payload, headers = self._fetch(path)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('REQUEST_FAILED') from None
        lower = {k.lower(): v for k, v in headers.items()}
        remaining = lower.get('x-requests-available-minute')
        if isinstance(remaining, str) and remaining.isdigit():
            self.remaining = int(remaining)
        return payload

    def competitions(self):
        payload = self._get('competitions')
        if not isinstance(payload, dict) or not isinstance(payload.get('competitions'), list):
            raise ProviderError('INVALID_RESPONSE')
        result = []
        for item in payload['competitions']:
            if item.get('code') in COMPETITIONS:
                season = item.get('currentSeason') or {}
                result.append({'code': item['code'], 'name': item.get('name'),
                               'seasonStart': season.get('startDate'), 'seasonEnd': season.get('endDate'),
                               'access': 'CATALOG_ONLY_NOT_MATCH_ACCESS'})
        return result

    def matches(self, code, day):
        if code not in COMPETITIONS:
            raise ProviderError('INVALID_COMPETITION')
        key = (code, day.isoformat())
        if key in self.cache:
            return [dict(row) for row in self.cache[key]]
        # UTC date query intentionally spans both UTC dates; postfilter is ART.
        start = datetime.combine(day, datetime.min.time(), local_start('2000-01-01T00:00:00Z').tzinfo)
        end = start + timedelta(days=1)
        query = urlencode({'dateFrom': start.astimezone(timezone.utc).date().isoformat(),
                           'dateTo': end.astimezone(timezone.utc).date().isoformat()})
        payload = self._get(f'competitions/{code}/matches?{query}')
        rows = normalize_matches(payload, code, day)
        self.cache[key] = rows
        return [dict(row) for row in rows]
