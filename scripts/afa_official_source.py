"""Conservative public AFA Primera Nacional HTML parser; no network or channel inference."""
from datetime import date, datetime
from html.parser import HTMLParser
import hashlib
import json
import re
import unicodedata
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

COMPETITION = 'Primera Nacional'
COMPETITION_ID = 'primera-nacional'
ART = ZoneInfo('America/Argentina/Buenos_Aires')
MONTHS = {name: i for i, name in enumerate(('enero', 'febrero', 'marzo', 'abril',
          'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'), 1)}
WEEKDAYS = ('lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo')
DATE_RE = re.compile(r'\b(' + '|'.join(WEEKDAYS) + r')\s+(\d{1,2})(?:\s+de\s+('
                     + '|'.join(MONTHS) + r')|\s*[-/]\s*(\d{1,2}))(?:\s+(?:de\s+)?(20\d{2}))?\b')
SIGNALS = {name.casefold(): name for name in ('TyC Sports', 'TyC Sports Play',
           'TNT Sports Premium', 'TNT Sports Argentina', 'ESPN Premium',
           'FOX Sports Argentina', 'DSports Argentina', 'LPF Play', 'AFA Play')}


class AfaSourceError(ValueError):
    """A changed, ambiguous or unsupported source must not invent agenda rows."""


def _norm(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.casefold())
                   if not unicodedata.combining(c))


def _official_url(value, article=False):
    try:
        p = urlsplit(value)
        return (p.scheme == 'https' and p.hostname in ('afa.com.ar', 'www.afa.com.ar')
                and not p.username and not p.password and p.port in (None, 443)
                and not p.query and not p.fragment
                and (not article or bool(re.fullmatch(r'/es/posts/[a-z0-9-]+/?', p.path))))
    except (TypeError, ValueError):
        return False


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.buffer, self.lines, self.published = [], [], []
        self.date_depth = 0
        self.ignore = 0
        self.links, self.link = [], None
        self.heading = False
        self.heading_indexes = set()

    def flush(self):
        text = re.sub(r'\s+', ' ', ''.join(self.buffer)).strip()
        if text:
            self.lines.append(text)
            if self.heading:
                self.heading_indexes.add(len(self.lines) - 1)
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag in ('script', 'style'):
            self.ignore += 1
        if tag == 'a':
            self.link = [attrs.get('href', ''), []]
        if 'btArticleDate' in classes:
            self.date_depth = 1
        elif self.date_depth and tag == 'span':
            self.date_depth += 1
        if tag == 'div' and 'btArticleContentInnerInner' in classes:
            if self.depth:
                raise AfaSourceError('AFA_AMBIGUOUS_CONTENT')
            self.depth = 1
        elif self.depth and tag == 'div':
            self.depth += 1
        if self.depth and tag in ('p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li'):
            self.flush()
        if self.depth and re.fullmatch(r'h[1-6]', tag):
            self.heading = True

    def handle_endtag(self, tag):
        if tag == 'a' and self.link is not None:
            self.links.append((self.link[0], ''.join(self.link[1])))
            self.link = None
        if self.depth and tag in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'div'):
            self.flush()
        if re.fullmatch(r'h[1-6]', tag):
            self.heading = False
        if self.depth and tag == 'div':
            self.depth -= 1
        if self.date_depth and tag == 'span':
            self.date_depth -= 1
        if tag in ('script', 'style') and self.ignore:
            self.ignore -= 1

    def handle_data(self, data):
        if self.ignore:
            return
        if self.depth:
            self.buffer.append(data)
        if self.date_depth:
            self.published.append(data)
        if self.link is not None:
            self.link[1].append(data)


def afa_article_links(html, source_url):
    """Current official index links only. Caller owns cache and request ceilings."""
    if not _official_url(source_url):
        raise AfaSourceError('AFA_INVALID_SOURCE')
    doc = _Document()
    doc.feed(html)
    links = []
    for href, label in doc.links:
        url = urljoin(source_url, href)
        hint = _norm(url + ' ' + label)
        if (_official_url(url, article=True) and 'programacion' in hint
                and ('ascenso' in hint or 'primera-nacional' in hint or 'primera nacional' in hint)
                and url not in links):
            links.append(url)
    # Multi-category weekly programming is preferable to a historical standalone final.
    return sorted(links, key=lambda url: 'ascenso' not in url)


def _signals(text):
    match = re.search(r'\b(?:TV|Televisa|Transmite|Transmisi[oó]n)\s*:\s*(.+)$', text, re.I)
    if not match:
        return []
    parts = re.split(r'\s*(?:/|;|,|\s+y\s+)\s*', match[1].strip(), flags=re.I)
    # A foreign regional qualifier, unknown platform or prose never becomes a domestic signal.
    if not parts or any(part.casefold() not in SIGNALS for part in parts):
        return []
    return list(dict.fromkeys(SIGNALS[part.casefold()] for part in parts))


def _pair(value):
    parts = re.split(r'\s+(?:c\.|[-–—])\s+', value.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    if any(not re.fullmatch(r"[^\d:;,<>]{2,90}", name) for name in parts):
        return None
    if any(re.search(r'\b(?:estadio|arbitro|campeonato|final|horas|jugar|caso)\b', _norm(name))
           for name in parts):
        return None
    return tuple(parts)


def parse_afa_fixtures(html, competition, source_url):
    """Parse explicit Primera Nacional fixture rows into aware ART datetimes.

    Missing years use only the same article's dated publication, within 62 days
    and the same year. They never use today's date, a footer or another category.
    Unlabelled future team pairings, scores and broadcaster rights are ignored.
    """
    if competition not in (COMPETITION, COMPETITION_ID):
        raise AfaSourceError('AFA_UNSUPPORTED_COMPETITION')
    if not _official_url(source_url, article=True):
        raise AfaSourceError('AFA_INVALID_SOURCE')
    if not isinstance(html, str) or len(html) > 2_000_000:
        raise AfaSourceError('AFA_INVALID_CONTENT')
    doc = _Document()
    doc.feed(html)
    if not doc.lines:
        raise AfaSourceError('AFA_UNRECOGNIZED_CONTENT')
    publication = None
    stamp = re.fullmatch(r'\s*(\d{2})/(\d{2})/(20\d{2})\s*', ''.join(doc.published))
    if stamp:
        try:
            publication = date(int(stamp[3]), int(stamp[2]), int(stamp[1]))
        except ValueError:
            raise AfaSourceError('AFA_INVALID_PUBLICATION_DATE') from None
    active, recognized, year, day, pending, last = False, False, None, None, None, None
    rows = {}

    def add(pair, clock, fixture_day, text):
        nonlocal last
        hour, minute = clock.replace(':', '.').split('.') if re.search(r'[.:]', clock) else (clock, '00')
        try:
            kickoff = datetime.combine(fixture_day, datetime.min.time(), ART).replace(hour=int(hour), minute=int(minute))
        except ValueError:
            raise AfaSourceError('AFA_INVALID_KICKOFF') from None
        identity = json.dumps([COMPETITION_ID, fixture_day.isoformat(), *(name.casefold() for name in pair)])
        identifier = 'afa:' + COMPETITION_ID + ':' + hashlib.sha256(identity.encode()).hexdigest()
        row = {'competition': COMPETITION, 'competitionId': COMPETITION_ID,
               'providerFixtureId': identifier, 'homeTeam': pair[0], 'awayTeam': pair[1],
               'startTime': kickoff, 'source': 'afa_official', 'sourceUrl': source_url,
               'broadcasters': _signals(text)}
        if identifier in rows and rows[identifier]['startTime'] != kickoff:
            raise AfaSourceError('AFA_CONFLICTING_FIXTURE')
        if identifier in rows:
            row['broadcasters'] = list(dict.fromkeys(rows[identifier]['broadcasters'] + row['broadcasters']))
        rows[identifier] = row
        last = row

    for line_index, line in enumerate(doc.lines):
        normalized = _norm(line)
        heading = re.fullmatch(r'(?:final por el campeonato de la |campeonato de |torneo de la )?primera nacional(?:\s*[-–]?\s*(?:\(?(?:fecha\s+\d+|20\d{2})\)?))?', normalized)
        if heading:
            active, recognized, day, pending, last = True, True, None, None, None
            years = re.findall(r'\b20\d{2}\b', normalized)
            year = int(years[0]) if years else (publication.year if publication else None)
            continue
        boundary = normalized.replace('“', '').replace('”', '').replace('"', '')
        phase_heading = re.fullmatch(r'(?:fecha\s+\d+|zona\s+[ab]|final|reducido)', normalized)
        other_heading = line_index in doc.heading_indexes and not phase_heading
        if other_heading or re.match(r'^(?:campeonato\s+)?(?:primera (?:division )?[abcd]\b|copa\b|supercopa\b|promocional\b|federal [ab]\b|futbol (?:femenino|playa)|futsal\b|proyeccion\b|reservas?\b)', boundary):
            active, day, pending, last = False, None, None, None
        if not active:
            continue
        date_match = DATE_RE.search(normalized)
        if date_match:
            explicit_year = int(date_match[5]) if date_match[5] else year
            if explicit_year is None:
                raise AfaSourceError('AFA_MISSING_YEAR')
            month = MONTHS[date_match[3]] if date_match[3] else int(date_match[4])
            try:
                day = date(explicit_year, month, int(date_match[2]))
            except ValueError:
                raise AfaSourceError('AFA_INVALID_DATE') from None
            if WEEKDAYS[day.weekday()] != date_match[1]:
                raise AfaSourceError('AFA_DATE_WEEKDAY_CONFLICT')
            if not date_match[5] and publication and (day.year != publication.year or not -7 <= (day-publication).days <= 62):
                raise AfaSourceError('AFA_AMBIGUOUS_YEAR')
            clock = re.search(r'\ba las\s+(\d{1,2}(?:[.:]\d{2})?)\b', normalized)
            if pending and clock:
                add(pending, clock[1], day, line)
            else:
                last = None
            pending = None
            continue
        if last and re.match(r'^(?:TV|Televisa|Transmite|Transmisi[oó]n)\s*:', line, re.I) and _signals(line):
            last['broadcasters'] = list(dict.fromkeys(last['broadcasters'] + _signals(line)))
            continue
        without_tv = re.split(r'\b(?:TV|Televisa|Transmite|Transmisi[oó]n)\s*:', line, flags=re.I)[0].strip(' -–;')
        clock_first = re.fullmatch(r'(\d{1,2}[.:]\d{2})\s+(.+)', without_tv)
        clock_last = re.fullmatch(r'(.+?),\s*a las\s+(\d{1,2}[.:]\d{2})\s+horas(?:\s+en\s+.+)?', without_tv, re.I)
        if clock_first or clock_last:
            clock, teams = (clock_first[1], clock_first[2]) if clock_first else (clock_last[2], clock_last[1])
            pair = _pair(teams)
            if pair and day:
                add(pair, clock, day, line)
            pending = None
            continue
        pending = _pair(without_tv)
        last = None
    if not recognized:
        raise AfaSourceError('AFA_CATEGORY_NOT_RECOGNIZED')
    return sorted(rows.values(), key=lambda item: (item['startTime'], item['providerFixtureId']))
