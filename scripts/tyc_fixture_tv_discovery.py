"""One bounded own-TyC article supplement, sharing the existing public budget.

No credentials, IPTV queries or new request budget. Persist only the raw dated
article/agenda proof, never a precomputed confidence or channel assignment.
"""
import copy
from datetime import date, datetime
import json
import re

from .argentina_tv_sources import TYC_AGENDA_URL
from .broadcaster_discovery import confidence
from .broadcaster_sources import DiscoveryError, Fixture, ZONE, instant
from .sports_national_agenda import national_key, senior_pair
from .tyc_fixture_tv_source import (
    find_tyc_fixture_tv_link, parse_tyc_fixture_tv, validate_tyc_fixture_tv_url,
)


MAX_CACHE_BYTES = 1_200_000
CACHE_SECONDS = 2 * 3600
_COMPETITION = 'Amistosos de selecciones'
_FAILURES = (DiscoveryError, KeyError, ValueError, TypeError, OSError, TimeoutError, UnicodeError)


def _safe_reason(error):
    code = str(error) if isinstance(error, DiscoveryError) else ''
    return code if re.fullmatch(r'TYC_FIXTURE_TV_[A-Z_]+', code) else type(error).__name__


def _empty_cache(day):
    return {'schemaVersion': 1, 'date': day.isoformat(), 'entry': None}


def _bounded(cache):
    try:
        return len(json.dumps(cache, ensure_ascii=False, allow_nan=False).encode('utf8')) <= MAX_CACHE_BYTES
    except (ValueError, TypeError, UnicodeError):
        return False


def _previous_entry(cache, day, now):
    if not isinstance(cache, dict) or set(cache) != {'schemaVersion', 'date', 'entry'} \
            or type(cache.get('schemaVersion')) is not int or cache['schemaVersion'] != 1 \
            or cache.get('date') != day.isoformat() or not _bounded(cache):
        return None
    entry = cache['entry']
    fields = {'fixtureKey', 'sourceUrl', 'fetchedAt', 'articleHtml', 'agendaHtml'}
    if not isinstance(entry, dict) or set(entry) != fields or \
            not isinstance(entry['fixtureKey'], list) or len(entry['fixtureKey']) != 4 or \
            not all(isinstance(value, str) for value in entry['fixtureKey']) or \
            not all(isinstance(entry[field], str) for field in ('articleHtml', 'agendaHtml')):
        return None
    try:
        validate_tyc_fixture_tv_url(entry['sourceUrl'])
        fetched = instant(entry['fetchedAt'])
        age = (now - fetched).total_seconds()
        if fetched.date() != day or not 0 <= age < CACHE_SECONDS:
            return None
    except _FAILURES:
        return None
    return copy.deepcopy(entry)


def _proof_cache(day, key, url, fetched_at, article, agenda):
    value = {'schemaVersion': 1, 'date': day.isoformat(), 'entry': {
        'fixtureKey': list(key), 'sourceUrl': url, 'fetchedAt': fetched_at,
        'articleHtml': article, 'agendaHtml': agenda,
    }}
    if not _bounded(value):
        raise DiscoveryError('TYC_FIXTURE_TV_CACHE_SIZE_LIMIT')
    return value


def _supplement(target, signals, url):
    # Keep only fixture identity. No copied claims, scores, logo provenance,
    # status, venue, URLs for streams, channel IDs or channelGroups.
    return {**{field: target[field] for field in ('competition', 'homeTeam', 'awayTeam', 'startTime')},
            'guideId': 'tyc-agenda', 'sourceUrl': TYC_AGENDA_URL,
            'broadcasters': list(signals), 'ownedTvUrl': url}


def enrich_fixture_tv(records, day, pages, guides, mapping, previous_cache=None, now=None, *, groups=None, offline=False):
    """Return ``(records, raw_proof_cache, report)`` without changing inputs.

    ``groups`` must be the already-loaded real catalog, so skip decisions use
    the existing confidence engine and actual enabled streams. Only current-day
    senior national friendlies without CONFIRMED_MBOX are considered; Argentina
    takes precedence. At most one article attempt per shared PublicPages object.

    Cached proof has a strict two-hour TTL and is parsed again on every use. If
    this invocation already fetched the agenda, that newer HTML supersedes the
    cached agenda, including when it invalidates an old row/link. Offline mode
    may revalidate fresh proof but never fetches a page or extends its TTL.
    """
    if type(day) is not date:
        raise DiscoveryError('TYC_FIXTURE_TV_EXPLICIT_DATE_REQUIRED')
    result, cache = copy.deepcopy(records), _empty_cache(day)
    report = {'source': 'tyc-fixture-tv', 'state': 'SKIP_NO_TARGETS', 'requests': 0,
              'articleRequests': 0, 'fixtures': 0}
    budget = pages.budget
    before = budget.requests

    def finish(state, *, reason=None):
        report.update(state=state, requests=budget.requests - before)
        if reason:
            report['reason'] = reason
        return result, cache, report

    clock = datetime.now(ZONE) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        return finish('SKIP_INVALID_CLOCK')
    clock = instant(clock.isoformat())
    if day != clock.date():
        return finish('SKIP_NOT_CURRENT_DAY')
    if not isinstance(groups, dict):
        return finish('SKIP_MISSING_GROUPS')
    if type(budget.maximum) is not int or not 0 <= budget.maximum <= 6:
        return finish('SKIP_INVALID_BUDGET')

    # Local import keeps prioritized_agendas free to call this after it has
    # loaded; no dependency cycle at module import time.
    from .prioritized_agendas import evidence_for, exact_key, signal_conflict
    targets = {}
    try:
        for record in result:
            if record.get('competition') != _COMPETITION or not senior_pair(record.get('homeTeam'), record.get('awayTeam')) \
                    or instant(record['startTime']).date() != day:
                continue
            targets.setdefault(exact_key(record, guides), record)
        unresolved = []
        for index, (key, target) in enumerate(targets.items(), 1):
            fixture = Fixture(index, target['homeTeam'], target['awayTeam'], target['competition'],
                              instant(target['startTime']), '', 'Argentina')
            decision = confidence(fixture, evidence_for(target, index, result, guides), mapping, groups)
            row = {**target, 'source': 'argentina_agendas'}
            if decision['state'] != 'CONFIRMED_MBOX' or signal_conflict(row, result, guides, mapping):
                unresolved.append((key, target))
    except _FAILURES as error:
        return finish('ERROR', reason=_safe_reason(error))
    if not unresolved:
        return finish('SKIP_RESOLVED' if targets else 'SKIP_NO_TARGETS')
    unresolved.sort(key=lambda item: (
        0 if 'Argentina' in {national_key(item[1]['homeTeam']), national_key(item[1]['awayTeam'])} else 1,
        item[0][3], item[0][1], item[0][2],
    ))
    fresh_entry = _previous_entry(previous_cache, day, clock)
    cached_agenda = pages.pages.get(TYC_AGENDA_URL)

    # A cached article can only apply to its exact current unresolved target.
    # Prefer this invocation's agenda even if doing so rejects cached proof.
    if fresh_entry is not None:
        matching = next(((key, target) for key, target in unresolved
                         if list(key) == fresh_entry['fixtureKey']), None)
        if matching is not None and matching[0] == unresolved[0][0]:
            key, target = matching
            agenda = cached_agenda if cached_agenda is not None else fresh_entry['agendaHtml']
            try:
                signals = parse_tyc_fixture_tv(fresh_entry['articleHtml'], target, day,
                    source_url=fresh_entry['sourceUrl'], agenda_html=agenda, guides=guides)
                cache = _proof_cache(day, key, fresh_entry['sourceUrl'], fresh_entry['fetchedAt'],
                                     fresh_entry['articleHtml'], agenda)
                if signals:
                    result.append(_supplement(target, signals, fresh_entry['sourceUrl']))
                    report['fixtures'] = 1
                return finish('CACHE_HIT' if signals else 'CACHE_NO_OWNED_TV')
            except _FAILURES as error:
                report['cacheRejectedReason'] = _safe_reason(error)
    if offline:
        return finish('OFFLINE_MISS')
    if getattr(pages, '_tyc_fixture_tv_article_attempted', False):
        return finish('DEFERRED_ARTICLE_LIMIT')

    # A failed URL is not retried; PublicPages already remembers that failure.
    if cached_agenda is None:
        if TYC_AGENDA_URL in pages.failed:
            return finish('AGENDA_UNAVAILABLE', reason='PUBLIC_PAGE_FAILED')
        if budget.maximum - budget.requests < 2:
            return finish('DEFERRED_BUDGET')
        try:
            cached_agenda = pages.fetch(TYC_AGENDA_URL)
        except _FAILURES as error:
            return finish('AGENDA_UNAVAILABLE', reason=_safe_reason(error))

    selected = None
    try:
        for key, target in unresolved:
            url = find_tyc_fixture_tv_link(cached_agenda, target, day, guides)
            if url:
                selected = key, target, url
                break
    except _FAILURES as error:
        return finish('AGENDA_UNAVAILABLE', reason=_safe_reason(error))
    if selected is None:
        return finish('NO_EXACT_ARTICLE_LINK')
    key, target, url = selected
    if url in pages.failed:
        return finish('ARTICLE_UNAVAILABLE', reason='PUBLIC_PAGE_FAILED')
    if url not in pages.pages and budget.requests >= budget.maximum:
        return finish('DEFERRED_BUDGET')
    pages._tyc_fixture_tv_article_attempted = True
    article_before = budget.requests
    try:
        article = pages.fetch(url)
        signals = parse_tyc_fixture_tv(article, target, day, source_url=url,
                                       agenda_html=cached_agenda, guides=guides)
        cache = _proof_cache(day, key, url, clock.isoformat(), article, cached_agenda)
        if signals:
            result.append(_supplement(target, signals, url))
            report['fixtures'] = 1
        state = 'OK' if signals else 'NO_OWNED_TV'
    except _FAILURES as error:
        report['reason'] = _safe_reason(error)
        state = 'ARTICLE_UNAVAILABLE'
    report['articleRequests'] = budget.requests - article_before
    return finish(state)
