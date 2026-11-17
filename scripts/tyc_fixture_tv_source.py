"""Exact, dated TyC-owned TV evidence from an agenda-linked match article.

This module performs no requests. The caller must keep the existing acquisition
budget and cache policy. The initial, deliberately narrow article template is
senior national-team friendlies; it does not generalize a TyC logo or an ESPN,
Telefe or platform listing into an owned linear broadcast.

The dated agenda row proves the fixture date. Article publication alone does not:
the real 17 November 2026 article also contains a LiveBlogPosting startDate on
the previous day. Liveblog coverage dates and TV text are never kickoff/TV
authority here. Only the editorial article's exact match-data list is read.
"""
from dataclasses import dataclass
from datetime import date, datetime
import json
import re
from urllib.parse import urlsplit

from .broadcaster_sources import DiscoveryError, Listing, MAX_BODY, MONTHS, Node, Tree, ZONE, instant, norm
from .sports_national_agenda import national_key


_FRIENDLIES = frozenset(map(norm, (
    'Amistosos de selecciones', 'Amistoso', 'Amistosos', 'Amistoso Internacional',
    'Amistosos Internacionales', 'International Friendlies',
)))
_ARTICLE_PATH = re.compile(r'/amistosos-de-selecciones/[a-z0-9]+(?:-[a-z0-9]+)*-id[1-9]\d{0,15}\.html\Z')
_TITLE_SUFFIX = 'por un amistoso donde ver en vivo formaciones y horario'
_FACTS_SUFFIX = 'por un amistoso donde ver en vivo y datos del partido'


@dataclass(frozen=True)
class _FixtureKey:
    home: str
    away: str
    kickoff: datetime


def validate_tyc_fixture_tv_url(value):
    """Return a normalized observed TyC article URL, or reject unsafe routes.

    Scheme-relative agenda hrefs are supported. No arbitrary hosts, redirects,
    query strings, fragments, credentials, ports or other article categories.
    """
    try:
        if not isinstance(value, str) or value != value.strip() or any(ord(char) < 33 for char in value):
            raise ValueError()
        normalized = 'https:' + value if value.startswith('//') else value
        parsed = urlsplit(normalized)
        if (parsed.scheme != 'https' or parsed.netloc != 'www.tycsports.com'
                or parsed.query or parsed.fragment or not _ARTICLE_PATH.fullmatch(parsed.path)
                or normalized != 'https://www.tycsports.com' + parsed.path):
            raise ValueError()
        return normalized
    except (ValueError, TypeError):
        raise DiscoveryError('TYC_FIXTURE_TV_UNSAFE_URL') from None


def _tree(html, day):
    if type(day) is not date:
        raise DiscoveryError('TYC_FIXTURE_TV_EXPLICIT_DATE_REQUIRED')
    if not isinstance(html, str) or len(html.encode('utf8')) > MAX_BODY:
        raise DiscoveryError('TYC_FIXTURE_TV_INVALID_HTML')
    return Tree(html)


def _fixture_key(fixture, day):
    if isinstance(fixture, Listing):
        home, away, competition, kickoff = fixture.home, fixture.away, fixture.competition, fixture.kickoff
    elif isinstance(fixture, dict):
        home, away, competition = (fixture.get(field) for field in ('homeTeam', 'awayTeam', 'competition'))
        try:
            kickoff = instant(fixture.get('startTime'))
        except (ValueError, TypeError):
            raise DiscoveryError('TYC_FIXTURE_TV_INVALID_FIXTURE') from None
    else:
        raise DiscoveryError('TYC_FIXTURE_TV_INVALID_FIXTURE')
    if norm(competition) not in _FRIENDLIES:
        return None
    left, right = national_key(home), national_key(away)
    if not left or not right or left == right:
        return None
    if not isinstance(kickoff, datetime) or kickoff.tzinfo is None:
        raise DiscoveryError('TYC_FIXTURE_TV_INVALID_FIXTURE')
    kickoff = kickoff.astimezone(ZONE)
    if kickoff.date() != day or kickoff.second or kickoff.microsecond:
        return None
    return _FixtureKey(left, right, kickoff)


def _day_heading(section):
    heading = next((node.text() for node in section.walk() if node.tag == 'h2'), '')
    match = re.search(r'\b(\d{1,2}) de ([a-z]+) del (\d{4})\b', norm(heading))
    if match is None or match[2] not in MONTHS:
        raise DiscoveryError('TYC_FIXTURE_TV_AGENDA_DATE_UNRECOGNIZED')
    try:
        return date(int(match[3]), MONTHS[match[2]], int(match[1]))
    except ValueError:
        raise DiscoveryError('TYC_FIXTURE_TV_AGENDA_DATE_UNRECOGNIZED') from None


def find_tyc_fixture_tv_link(agenda_html, fixture, day, guides=None):
    """Find one article in the exact dated/ordered friendly row, never by search.

    ``guides`` is accepted for the orchestration adapter's common signature; the
    observed friendly aliases above remain a closed allowlist in this parser.
    Duplicate identical markup is harmless; multiple distinct links are unsafe.
    Unsupported competitions and non-senior teams yield no link.
    """
    tree = _tree(agenda_html, day)
    key = _fixture_key(fixture, day)
    if key is None:
        return None
    sections = [node for node in tree.root.walk() if node.has_class('agenda_results')]
    if not sections:
        raise DiscoveryError('TYC_FIXTURE_TV_AGENDA_UNRECOGNIZED')
    links = set()
    for section in sections:
        if _day_heading(section) != day:
            continue
        for tournament in (node for node in section.walk() if node.has_class('agenda_comp_results')):
            if tournament.attrs.get('data-selectdeporte') != 'futbol':
                continue
            header = tournament.find('header_comp_agenda')
            competition = next((node.text() for node in header.walk() if node.tag in ('h2', 'h3')), '') if header else ''
            if norm(competition) not in _FRIENDLIES:
                continue
            for item in (node for node in tournament.walk() if node.has_class('item_agenda')):
                teams, clock = item.find('text-teams-agenda'), item.find('hs-item-agenda')
                if teams is None or clock is None or clock.text() != key.kickoff.strftime('%H:%M'):
                    continue
                names = [national_key(node.attrs.get('alt')) for node in teams.walk() if node.tag == 'img']
                if names != [key.home, key.away]:
                    continue
                for anchor in (node for node in item.walk() if node.tag == 'a'):
                    try:
                        links.add(validate_tyc_fixture_tv_url(anchor.attrs.get('href')))
                    except DiscoveryError:
                        continue
    return next(iter(links)) if len(links) == 1 else None


def _title_pair(text, suffix):
    match = re.fullmatch(r'(?:seleccion )?(.+?) vs (.+?) ' + re.escape(suffix), norm(text))
    if match is None:
        return None
    # Fixed title grammar only. No stripping Sub-17, women's or reserve labels.
    home, away = national_key(match[1]), national_key(match[2])
    return (home, away) if home and away and home != away else None


def _news_article(tree, source_url, title, day):
    records = []
    for node in tree.root.walk():
        if node.tag != 'script' or node.attrs.get('type') != 'application/ld+json':
            continue
        try:
            record = json.loads(node.text())
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and record.get('@type') == 'NewsArticle':
            records.append(record)
    if len(records) != 1:
        raise DiscoveryError('TYC_FIXTURE_TV_EDITORIAL_METADATA_UNRECOGNIZED')
    record = records[0]
    main = record.get('mainEntityOfPage')
    if not isinstance(main, dict) or main.get('@id') != source_url or norm(record.get('headline')) != norm(title) \
            or norm(record.get('articleSection')) not in _FRIENDLIES:
        raise DiscoveryError('TYC_FIXTURE_TV_EDITORIAL_IDENTITY_MISMATCH')
    try:
        published = instant(record.get('datePublished'))
    except (ValueError, TypeError):
        raise DiscoveryError('TYC_FIXTURE_TV_PUBLICATION_DATE_UNRECOGNIZED') from None
    if published.date() != day:
        raise DiscoveryError('TYC_FIXTURE_TV_PUBLICATION_DATE_MISMATCH')
    return published


def parse_tyc_fixture_tv(html, fixture, day, *, source_url, agenda_html, guides=None):
    """Return only explicitly owned ``('TyC Sports',)`` for this exact fixture.

    The caller supplies the fetched article URL and already-acquired agenda HTML.
    Both are required: a publication date by itself is not a fixture date. On
    unrecognized or mismatched context this raises a sanitized DiscoveryError;
    an explicit non-owned TV line yields (). Never copy venue/scores/status.
    """
    tree = _tree(html, day)
    source_url = validate_tyc_fixture_tv_url(source_url)
    key = _fixture_key(fixture, day)
    if key is None or find_tyc_fixture_tv_link(agenda_html, fixture, day, guides) != source_url:
        raise DiscoveryError('TYC_FIXTURE_TV_DATED_AGENDA_MISMATCH')
    canonicals = [node.attrs.get('href') for node in tree.root.walk()
                  if node.tag == 'link' and node.attrs.get('rel') == 'canonical']
    if canonicals != [source_url]:
        raise DiscoveryError('TYC_FIXTURE_TV_CANONICAL_MISMATCH')
    articles = [node for node in tree.root.walk() if node.tag == 'article'
                and node.attrs.get('data-name') == 'article-left-1']
    if len(articles) != 1:
        raise DiscoveryError('TYC_FIXTURE_TV_ARTICLE_UNRECOGNIZED')
    article = articles[0]
    children = [node for node in article.children if isinstance(node, Node)]
    headings = [node for node in children if node.tag == 'h1']
    if len(headings) != 1 or _title_pair(headings[0].text(), _TITLE_SUFFIX) != (key.home, key.away):
        raise DiscoveryError('TYC_FIXTURE_TV_TEAMS_MISMATCH')
    published = _news_article(tree, source_url, headings[0].text(), day)
    times = [node.attrs.get('datetime') for node in article.walk() if node.tag == 'time']
    try:
        if len(times) != 1 or instant(times[0]) != published:
            raise ValueError()
    except (ValueError, TypeError):
        raise DiscoveryError('TYC_FIXTURE_TV_PUBLICATION_DATE_MISMATCH') from None
    facts = [index for index, node in enumerate(children) if node.tag == 'h2'
             and _title_pair(node.text(), _FACTS_SUFFIX) == (key.home, key.away)]
    if len(facts) != 1 or facts[0] + 1 >= len(children) or children[facts[0] + 1].tag != 'ul':
        raise DiscoveryError('TYC_FIXTURE_TV_MATCH_DATA_UNRECOGNIZED')
    rows = [node.text() for node in children[facts[0] + 1].children if isinstance(node, Node) and node.tag == 'li']
    clocks = [text[8:].strip() for text in rows if text.startswith('Horario:')]
    tv = [text[3:].strip() for text in rows if text.startswith('TV:')]
    if clocks != [key.kickoff.strftime('%H.%M')] or len(tv) != 1:
        raise DiscoveryError('TYC_FIXTURE_TV_MATCH_DATA_MISMATCH')
    # Exact observed ownership only: neither TyC branding nor another provider's
    # channel/platform mention is independent evidence for a linear TV service.
    return ('TyC Sports',) if norm(tv[0]) == 'tyc sports' else ()
