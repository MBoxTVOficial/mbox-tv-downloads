"""Verified Olé result-page contract, without inventing independent sources.

Checked 2026-11-15: Estadísticas' Results component calls the same anonymous
Opta bulk endpoint already used by the updater. Its HTML contains an empty
``properties.results.data`` object. Agenda's nested mams currently contain
neither an explicit match state nor complete scores. Poll the shared JSON once,
not both HTML pages plus the same JSON, and do not count them as separate votes.
"""
from datetime import date

from .broadcaster_sources import DiscoveryError
from .ole_agenda_source import _NextPayload, parse_ole_listings
from .sports_match_updates import (
    MAX_BODY, SOURCE_URL as OPTA_URL, MatchUpdatesError, decode_json, parse_ole_bulk,
)

AGENDA_URL = 'https://www.ole.com.ar/agenda-deportiva'
STATISTICS_URL = 'https://www.ole.com.ar/estadisticas/futbol/home.html'
# One owner/provider even though two public screens display the data.
SOURCE_URLS = {'ole-opta': OPTA_URL}
SOURCE_PAGES = {'ole-agenda': AGENDA_URL, 'ole-statistics': STATISTICS_URL}


def _text(raw):
    if isinstance(raw, bytes):
        if len(raw) > MAX_BODY:
            raise MatchUpdatesError('invalid_source_size')
        try:
            return raw.decode('utf-8-sig')
        except UnicodeError:
            raise MatchUpdatesError('invalid_source_encoding') from None
    if not isinstance(raw, str) or len(raw.encode('utf8')) > MAX_BODY:
        raise MatchUpdatesError('invalid_source_size')
    return raw


def _page_payload(raw, route, canonical):
    text = _text(raw)
    parser = _NextPayload()
    try:
        parser.feed(text)
        if parser.count != 1 or parser.canonicals != [canonical]:
            raise ValueError()
        payload = decode_json(''.join(parser.chunks).encode('utf8'))
        if payload['query']['match'] != route:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise MatchUpdatesError('unrecognized_ole_page') from None
    return text, payload


def parse_ole_statistics_results(raw, day, now=None):
    """Parse Estadísticas' actual grouped JSON with truthful Olé/Opta provenance.

    The date is the requested ART date. ``now`` is accepted for a common adapter
    interface; stale/future observations are rejected by the merger, not changed
    here. A readable HTML shell is not a successful empty result observation.
    """
    if type(day) is not date:
        raise MatchUpdatesError('invalid_date')
    if isinstance(raw, dict):
        return parse_ole_bulk(raw, day)
    text = _text(raw)
    if text.lstrip().startswith('{'):
        return parse_ole_bulk(decode_json(text.encode('utf8')), day)
    _, payload = _page_payload(text, ['estadisticas', 'futbol', 'home.html'], STATISTICS_URL)
    try:
        results = payload['props']['pageProps']['matchConfig']['properties']['results']
        if results != {'page': 'home', 'slug': 'home', 'competitionSlug': None,
                       'teamId': None, 'matchId': None, 'data': {}}:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise MatchUpdatesError('unrecognized_ole_statistics_payload') from None
    raise MatchUpdatesError('ole_statistics_requires_shared_feed')


def parse_ole_agenda_results(raw, day, now=None):
    """Fail closed for Agenda's verified result-less hydration records.

    The agenda adapter remains valid for fixture/channel discovery. Its current
    mams snapshots are not an additional validated result feed. In particular,
    scores/status supplied in arbitrary added fields, or kickoff having passed,
    must not promote an agenda entry to a live/final observation.
    """
    if type(day) is not date:
        raise MatchUpdatesError('invalid_date')
    text, _ = _page_payload(raw, ['agenda-deportiva'], AGENDA_URL)
    try:
        parse_ole_listings(text, day)
    except DiscoveryError:
        raise MatchUpdatesError('unrecognized_ole_agenda_payload') from None
    raise MatchUpdatesError('ole_agenda_results_not_embedded')
