"""Bounded, credential-free result acquisition and exact cross-source comparison.

Olé Estadísticas and its calendar are one provider, not independent votes.
No arbitrary URLs, redirects, search snippets, accounts or IPTV requests.
"""
from dataclasses import dataclass
from datetime import date
from http.client import HTTPException
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .broadcaster_sources import NoRedirect
from .sports_match_updates import (COMPETITIONS, LIVE_TTL, MAX_BODY, SOURCE_URL,
    TIMEOUT_SECONDS, MatchUpdatesError, SourceMatch, _source_key, request_body)


def source_urls():
    return {
        'ole-opta': SOURCE_URL,
        'canchallena': 'https://canchallena.lanacion.com.ar/',
        'tyc-results': 'https://www.tycsports.com/estadisticas/primera-nacional/fixture.html',
    }


def request_url(provider, day):
    """Return the bounded public request endpoint for one ART day.

    Cancha Llena publishes its dated results at ``/fecha/YYYY-MM-DD``.  Its
    landing page does not carry the daily hydration document, so it cannot
    produce a verified match state.  The stored source identity remains the
    stable provider URL from ``source_urls``; this is only the fetch target.
    """
    if provider not in source_urls() or type(day) is not date:
        raise MatchUpdatesError('invalid_result_request')
    if provider == 'canchallena':
        return f"https://canchallena.lanacion.com.ar/fecha/{day.isoformat()}/"
    return source_urls()[provider]


def source_competitions(guides):
    # Parsers still require their verified competition identifiers/names. This
    # controls requests, not acceptance of arbitrary/unvalidated competitions.
    from .public_results_tyc import SUPPORTED_COMPETITIONS
    result = {'ole-opta': {value[1] for value in COMPETITIONS.values()}}
    for provider in ('canchallena', 'tyc-results'):
        known = SUPPORTED_COMPETITIONS.get(provider, set())
        result[provider] = set(known) & set(guides['competitionAliases'])
    return result


def parse_provider(provider, raw, day, now):
    if provider == 'ole-opta':
        from .public_results_ole import parse_ole_statistics_results
        return parse_ole_statistics_results(raw, day, now=now)
    from .public_results_tyc import parse_tyc_results, parse_canchallena_results
    parser = {'tyc-results': parse_tyc_results, 'canchallena': parse_canchallena_results}.get(provider)
    if parser is None:
        raise MatchUpdatesError('unknown_result_provider')
    return parser(raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw, day, now=now)


class PublicResultsTransport:
    """One grouped request per provider; process timeout also bounds DNS/TLS."""
    def fetch(self, provider, day):
        if provider not in source_urls() or type(day) is not date:
            raise MatchUpdatesError('invalid_result_request')
        try:
            result = subprocess.run([sys.executable, '-m', 'scripts.sports_results_sources',
                '--fetch-provider', provider, day.isoformat(), str(TIMEOUT_SECONDS)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise MatchUpdatesError('source_unavailable') from None
        if result.returncode != 0 or len(result.stdout) > MAX_BODY:
            raise MatchUpdatesError('source_unavailable')
        return result.stdout


def _fetch_provider(provider, day, timeout):
    if provider not in source_urls() or type(day) is not date or not 1 <= timeout <= TIMEOUT_SECONDS:
        raise MatchUpdatesError('invalid_result_request')
    data = json.dumps(request_body(day)).encode('utf8') if provider == 'ole-opta' else None
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MBoxSportsMatchUpdates/1.1)',
               'Accept': 'application/json' if data else 'text/html'}
    if data:
        headers['Content-Type'] = 'application/json'
    try:
        with build_opener(NoRedirect()).open(Request(request_url(provider, day), data=data,
                headers=headers, method='POST' if data else 'GET'), timeout=timeout) as response:
            if response.status != 200:
                raise MatchUpdatesError('source_unavailable')
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise MatchUpdatesError('source_body_limit')
        return raw
    except HTTPError as error:
        error.close()
        raise MatchUpdatesError('source_unavailable') from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise MatchUpdatesError('source_unavailable') from None


@dataclass(frozen=True)
class Comparison:
    rows: tuple
    conflicts: int
    corroborated: int


def compare_results(rows, guides, now, previous=None):
    """Only exact competition + ordered teams + ART kickoff may corroborate.

    Conflicting usable snapshots quarantine this observation. Last-good data is
    retained by the merger and cannot be re-stamped or reopened by a conflict.
    """
    urls, grouped, identities = source_urls(), {}, {}
    for row in rows:
        if not isinstance(row, SourceMatch) or row.provider not in urls or urls[row.provider] != row.source_url or \
                not isinstance(row.match_id, str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}', row.match_id) or \
                row.status not in ('LIVE', 'FINISHED') or row.kickoff > now or \
                (row.source_updated is not None and row.source_updated > now):
            continue
        if (row.home_score is None) != (row.away_score is None) or any(
                value is not None and (type(value) is not int or not 0 <= value <= 999)
                for value in (row.home_score, row.away_score)):
            continue
        if row.status == 'LIVE' and row.source_updated is not None and now - row.source_updated > LIVE_TTL:
            continue
        key = tuple(_source_key(row, guides))
        grouped.setdefault(key, []).append(row)
        identities.setdefault((row.provider, row.match_id), set()).add(key)
    collided = {identity for identity, keys in identities.items() if len(keys) > 1}
    selected, conflicts, corroborated = [], 0, 0
    old_by_key = {tuple(entry['fixtureKey']): entry for entry in (previous or {}).get('matches', {}).values()}
    for key, observations in grouped.items():
        clean = [row for row in observations if (row.provider, row.match_id) not in collided]
        if len(clean) != len(observations):
            conflicts += 1
        observations = clean
        if not observations:
            continue
        by_provider = {}
        for row in observations:
            by_provider.setdefault(row.provider, set()).add(row)
        # Different raw IDs/values from the same provider for one fixture are
        # ambiguous, even if their displayed scores happen to match.
        if any(len(items) != 1 for items in by_provider.values()):
            conflicts += 1
            continue
        statuses = {row.status for row in observations}
        scores = {(row.home_score, row.away_score) for row in observations if row.home_score is not None}
        if len(statuses) != 1 or len(scores) > 1:
            conflicts += 1
            continue
        complete = [row for row in observations if row.home_score is not None]
        candidates = complete or observations
        old = old_by_key.get(key)
        preference = old['source']['provider'] if old else None
        # Prefer the freshest source timestamp, then a bound source and a
        # deterministic provider order. No majority voting.
        chosen = max(candidates, key=lambda row: (row.source_updated is not None,
            row.source_updated.timestamp() if row.source_updated is not None else 0,
            row.provider == preference, row.provider == 'ole-opta', row.provider))
        selected.append(chosen)
        corroborated += len(by_provider) > 1
    return Comparison(tuple(selected), conflicts, corroborated)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 5 or sys.argv[1] != '--fetch-provider':
            raise MatchUpdatesError('invalid_worker_arguments')
        sys.stdout.buffer.write(_fetch_provider(sys.argv[2], date.fromisoformat(sys.argv[3]), int(sys.argv[4])))
    except (MatchUpdatesError, ValueError):
        sys.exit(1)
