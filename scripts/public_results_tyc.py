"""Verified public TyC/Canchallena results; never TV/channel evidence.

Checked 2026-11-15. TyC's whole-season Primera Nacional fixture has explicit
dated final scores (the agenda itself has no results). Canchallena exposes its
page hydration JSON. Its dateTime is NOT kickoff: matchDateUTC is authoritative.
No external scripts are executed and no source's clock-derived final is accepted.
"""
from datetime import date, datetime
import json
import re

from .broadcaster_sources import DiscoveryError, Tree, ZONE, instant, norm, safe_text
from .sports_match_updates import MatchUpdatesError, SourceMatch


SOURCE_URLS = {
    'tyc-results': 'https://www.tycsports.com/estadisticas/primera-nacional/fixture.html',
    'canchallena': 'https://canchallena.lanacion.com.ar/',
}
SUPPORTED_COMPETITIONS = {
    'tyc-results': frozenset({'Primera Nacional'}),
    'canchallena': frozenset({'Primera Nacional', 'Copa Argentina',
                            'Amistosos de selecciones', 'UEFA Nations League'}),
}
MAX_BODY = 2_000_000
# IDs AND names verified against the real rendered groups. Unsupported
# competitions do not acquire guessed canonical identities from their labels.
_CANCHA_COMPETITIONS = {
    'byu00jvt1j6csyv4y1lkt2fm2': ('Primera Nacional', 'Primera Nacional'),
    'bdtat25m14jy85y484z3e6lf': ('Copa Argentina', 'Copa Argentina'),
    'cesdwwnxbc5fmajgroc0hqzy2': ('Amistosos de Selecciones', 'Amistosos de selecciones'),
    '595nsvo7ykvoe690b1e4u5n56': ('Liga de Naciones', 'UEFA Nations League'),
}
# Result-only PN labels tied to the exact TyC team crest ID in the verified
# fixture, not general text aliases. Never make bare "Estudiantes" Caseros/LP.
_TYC_PN_TEAM_ALIASES = {
    ('785', 'f c midland'): 'Midland',
    ('102', 'g y esgrima jujuy'): 'Gimnasia Jujuy',
    ('665', 'alte brown'): 'Almirante Brown',
}


def _html(raw, day):
    if type(day) is not date:
        raise MatchUpdatesError('invalid_date')
    if isinstance(raw, bytes):
        if len(raw) > MAX_BODY:
            raise MatchUpdatesError('invalid_html_size')
        try:
            raw = raw.decode('utf-8-sig')
        except UnicodeError:
            raise MatchUpdatesError('invalid_html_encoding') from None
    if not isinstance(raw, str) or len(raw.encode('utf8')) > MAX_BODY:
        raise MatchUpdatesError('invalid_html_size')
    return raw


def _scores(home, away):
    return all(type(value) is int and 0 <= value <= 999 for value in (home, away))


def _deduplicate(rows):
    values, ambiguous = {}, set()
    for row in rows:
        if row.match_id in values and values[row.match_id] != row:
            ambiguous.add(row.match_id)
        values[row.match_id] = row
    return [row for identifier, row in values.items() if identifier not in ambiguous]


def parse_tyc_results(raw, day, now=None):
    """Verified TyC PN fixture finals. Unverified live timer markup is ignored."""
    try:
        tree = Tree(_html(raw, day))
    except DiscoveryError:
        raise MatchUpdatesError('unrecognized_tyc_results') from None
    titles = [node.text() for node in tree.root.walk() if node.tag == 'title']
    if len(titles) != 1 or norm(titles[0]) != 'fixture de primera nacional tyc sports':
        raise MatchUpdatesError('unrecognized_tyc_results')
    containers = [node for node in tree.root.walk() if node.has_class('statsWrapFixture')]
    if len(containers) != 1:
        raise MatchUpdatesError('unrecognized_tyc_results')
    matches = [node for node in containers[0].walk() if node.has_class('partido')]
    if len(matches) > 1000:
        raise MatchUpdatesError('source_record_limit')
    result = []
    for node in matches:
        try:
            timer, dated, clock = node.find('timer'), node.find('date'), node.find('time')
            if timer is None or norm(timer.text()) != 'finalizado' or dated is None or clock is None:
                continue
            dated_match = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})', dated.text().split()[0])
            clock_match = re.fullmatch(r'((?:[01]\d|2[0-3]):[0-5]\d) HS', clock.text())
            if dated_match is None or clock_match is None:
                continue
            match_day = date(int(dated_match[3]), int(dated_match[2]), int(dated_match[1]))
            if match_day != day:
                continue
            kickoff = datetime.combine(day, datetime.strptime(clock_match[1], '%H:%M').time(), ZONE)
            teams, scores = {}, {}
            for side in ('local', 'visita'):
                cells = [cell for cell in node.walk() if cell.has_class('partido-team') and cell.has_class(side)]
                score_cells = [cell for cell in node.walk() if cell.has_class('partido-score') and cell.has_class(side)]
                if len(cells) != 1 or len(score_cells) != 1:
                    raise ValueError()
                images = [image for image in cells[0].walk() if image.tag == 'img']
                names = [image.attrs.get('alt', '') for image in images]
                values = [cell.text() for cell in score_cells[0].walk() if cell.has_class('partido-goles')]
                if len(names) != 1 or not safe_text(names[0], 100) or len(values) != 1 or not re.fullmatch(r'\d{1,3}', values[0]):
                    raise ValueError()
                image_id = re.fullmatch(r'https://statics-files\.tycsports\.com/escudos/([1-9]\d*)\.png(?:\?v=\d+)?',
                                       images[0].attrs.get('data-src', ''))
                name = _TYC_PN_TEAM_ALIASES.get((image_id[1], norm(names[0])), names[0]) if image_id else names[0]
                teams[side], scores[side] = name, int(values[0])
            parent = node.parent
            if parent is None or parent.tag != 'a':
                continue
            identifier = re.search(r'channel=deportes\.futbol\.nacionalb\.([1-9]\d{0,19})(?:[\x27\x22&,]|$)',
                                   parent.attrs.get('onclick', ''))
            if identifier is None or teams['local'] == teams['visita']:
                continue
            result.append(SourceMatch(identifier[1], 'Primera Nacional', teams['local'], teams['visita'],
                kickoff, 'FINISHED', None, scores['local'], scores['visita'],
                provider='tyc-results', source_url=SOURCE_URLS['tyc-results']))
        except (TypeError, ValueError, IndexError):
            continue
    return _deduplicate(result)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def parse_canchallena_results(raw, day, now=None):
    """Read bounded inert hydration JSON, with explicit status and original kickoff.

    Public bundle module 115 defines 0 Fixture, 1 Played, 2 Playing, 3 Cancelled,
    4 Postponed, 5 Suspended, 7 Awarded. The site can also fabricate final labels
    by elapsed time; requiring original post-match periodId=14 rejects that path.
    """
    html = _html(raw, day)
    markers = list(re.finditer(r'window\.__PRELOADED_STATE__\s*=\s*', html))
    if len(markers) != 1:
        raise MatchUpdatesError('unrecognized_canchallena_results')
    try:
        data, _ = json.JSONDecoder(object_pairs_hook=_unique).raw_decode(html[markers[0].end():])
        groups = data['homeReducer']['matchesData']['matchesByCompetition']
        if not isinstance(groups, list) or len(groups) > 100:
            raise ValueError()
    except (TypeError, ValueError, KeyError, RecursionError):
        raise MatchUpdatesError('unrecognized_canchallena_results') from None
    result, count = [], 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('competition'), dict) or not isinstance(group.get('matches'), list):
            continue
        metadata = group['competition']
        verified = _CANCHA_COMPETITIONS.get(metadata.get('id'))
        if verified is None or metadata.get('competitionName') != verified[0] or metadata.get('sportName') != 'futbol':
            continue
        for row in group['matches']:
            count += 1
            if count > 1000:
                raise MatchUpdatesError('source_record_limit')
            try:
                if not isinstance(row, dict) or row.get('isOverWritten') is not False:
                    continue
                # Numeric state is a source enum, never a conclusion from kickoff.
                if row.get('matchStatus') == '1' and type(row.get('periodId')) is int and row['periodId'] == 14 and norm(row.get('matchTime', '')) in {'final', 'finalizado'}:
                    status = 'FINISHED'
                elif row.get('matchStatus') == '2' and type(row.get('periodId')) is int and row['periodId'] in {1, 2}:
                    status = 'LIVE'
                else:
                    continue
                identifier = row['id']
                if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}', identifier):
                    continue
                kickoff = instant(row['matchDateUTC'])
                # In final rows dateTime is the last observation, not original kickoff.
                art_clock = datetime.strptime(row['matchDateText'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZONE)
                if kickoff != art_clock or kickoff.date() != day:
                    continue
                teams = row['contestants']
                names = [teams[side].get('nameUrl') or teams[side]['name'] for side in ('homeTeam', 'awayTeam')]
                if not all(safe_text(name, 100) for name in names) or names[0] == names[1]:
                    continue
                goals = row['goals']
                if not isinstance(goals, dict) or not _scores(goals.get('homeScore'), goals.get('awayScore')):
                    continue
                updated = None
                # The live site sometimes uses an ISO duration here; it cannot
                # serve as a last-updated timestamp or be replaced by fetch time.
                if isinstance(row.get('dateTime'), str) and re.match(r'\d{4}-\d{2}-\d{2}T', row['dateTime']):
                    updated = instant(row['dateTime'])
                if updated is not None and updated < kickoff:
                    continue
                result.append(SourceMatch(identifier, verified[1], *names, kickoff, status, updated,
                    goals['homeScore'], goals['awayScore'], provider='canchallena',
                    source_url=SOURCE_URLS['canchallena']))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return _deduplicate(result)
