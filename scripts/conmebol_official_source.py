"""Official CONMEBOL server-rendered fixture cards; no widget/API credentials.

The public competition homepages contain fixture links, complete team labels and
Unix timestamps in their HTML. Scores, match-state classes and third-party
widgets are deliberately ignored. These cards do not establish Argentine TV.
"""
from datetime import datetime, timezone
import re
from urllib.parse import urljoin, urlsplit

from .broadcaster_sources import DiscoveryError, MAX_BODY, Tree, ZONE, norm, safe_text


COMPETITIONS = {
    'CONMEBOL Libertadores': 'libertadores',
    'CONMEBOL Sudamericana': 'sudamericana',
}
SOURCE_URLS = {name: f'https://gol.conmebol.com/{slug}/es'
               for name, slug in COMPETITIONS.items()}
COUNTRY_CODES = frozenset(('AR', 'BO', 'BR', 'CL', 'CO', 'EC', 'PY', 'PE', 'UY', 'VE'))
COUNTRY_NAMES = {'argentina': 'AR', 'bolivia': 'BO', 'brasil': 'BR', 'brazil': 'BR',
                 'chile': 'CL', 'colombia': 'CO', 'ecuador': 'EC', 'paraguay': 'PY',
                 'peru': 'PE', 'uruguay': 'UY', 'venezuela': 'VE'}


def _crest_ids(node):
    """Read only official crest identities, never infer identity from team names."""
    identities = set()
    for image in node.walk() if node else ():
        if image.tag != 'img':
            continue
        try:
            parsed = urlsplit(image.attrs.get('src', ''))
            match = re.fullmatch(r'/icons/team/(?:light|dark)/[123]x/id/([1-9]\d*)\.png', parsed.path)
            if parsed.scheme == 'https' and parsed.hostname == 'gol-cdn.conmebol.com' and \
                    parsed.port in (None, 443) and not parsed.username and not parsed.password and \
                    not parsed.fragment and (not parsed.query or re.fullmatch(r'version=\d+', parsed.query)) and match:
                identities.add(match.group(1))
        except (TypeError, ValueError, AttributeError):
            continue
    return identities


def _team_countries(tree, source_url, slug):
    """The same official page's team directory binds a crest ID to its country.

    Cards without this optional directory retain the legacy fixture format.
    Conflicting declarations quarantine only the affected country identity.
    """
    countries, conflicts = {}, set()
    for team in tree.root.walk():
        if not team.has_class('m-team-teaser'):
            continue
        link = team.find('m-team-teaser__link')
        title = team.find('m-team-teaser__title')
        country = team.find('m-team-teaser__country')
        if not link or not title or not country or not safe_text(title.text()):
            continue
        code = COUNTRY_NAMES.get(norm(country.text()))
        if code is None:
            continue
        try:
            parsed = urlsplit(urljoin(source_url, link.attrs.get('href', '')))
            match = re.fullmatch(rf'/{slug}/es/team/([1-9]\d*)/?', parsed.path)
            if parsed.scheme != 'https' or parsed.hostname != 'gol.conmebol.com' or \
                    parsed.port not in (None, 443) or parsed.username or parsed.password or \
                    parsed.query or parsed.fragment or match is None:
                continue
            identifier = match.group(1)
            if _crest_ids(team) != {identifier} or identifier in conflicts:
                continue
            if identifier in countries and countries[identifier] != code:
                countries.pop(identifier)
                conflicts.add(identifier)
            else:
                countries[identifier] = code
        except (TypeError, ValueError, AttributeError):
            continue
    return countries


def _source_slug(competition, source_url):
    slug = COMPETITIONS.get(competition)
    if slug is None:
        raise DiscoveryError('CONMEBOL: unsupported competition')
    try:
        parsed = urlsplit(source_url)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'gol.conmebol.com'
                 and parsed.path.rstrip('/') == f'/{slug}/es'
                 and not parsed.query and not parsed.fragment and not parsed.username
                 and not parsed.password and parsed.port in (None, 443))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise DiscoveryError('CONMEBOL: source URL mismatch')
    return slug


def _fixture_url(link, source_url, slug):
    try:
        parsed = urlsplit(urljoin(source_url, link.attrs.get('href', '')))
        match = re.fullmatch(rf'/{slug}/es/fixture/view/([1-9]\d*)/?', parsed.path)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'gol.conmebol.com'
                 and match is not None and not parsed.query and not parsed.fragment
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise DiscoveryError('CONMEBOL: fixture link or competition mismatch')
    identifier = match.group(1)
    return identifier, f'https://gol.conmebol.com/{slug}/es/fixture/view/{identifier}'


def _card(card, competition, slug, source_url, countries=None):
    links = [node for node in card.walk()
             if node.has_class('m-fixture-teaser-card__match-centre-link')]
    if len(links) != 1:
        raise DiscoveryError('CONMEBOL: missing or ambiguous fixture identity')
    identifier, fixture_url = _fixture_url(links[0], source_url, slug)
    # The official accessibility label is "<complete home>vs<complete away>".
    # Do not derive identities from shortened badge names or guess a delimiter.
    label = links[0].text()
    teams = label.split('vs')
    if len(teams) != 2:
        raise DiscoveryError('CONMEBOL: ambiguous complete team label')
    home, away = (name.strip() for name in teams)
    if not all(safe_text(name) for name in (home, away)) or norm(home) == norm(away):
        raise DiscoveryError('CONMEBOL: invalid team names')
    clock = card.find('m-fixture-teaser-card__date')
    stamps = {node.attrs['data-timestamp'] for node in clock.walk()
              if 'data-timestamp' in node.attrs} if clock else set()
    if len(stamps) != 1:
        raise DiscoveryError('CONMEBOL: missing or conflicting kickoff')
    value = next(iter(stamps))
    if not re.fullmatch(r'[1-9]\d{8,10}', value):
        raise DiscoveryError('CONMEBOL: invalid Unix kickoff')
    try:
        start = datetime.fromtimestamp(int(value), timezone.utc).astimezone(ZONE)
    except (ValueError, OverflowError, OSError):
        raise DiscoveryError('CONMEBOL: invalid Unix kickoff') from None
    row = {'competition': competition, 'competitionId': slug,
           'providerFixtureId': f'{slug}:{identifier}', 'homeTeam': home,
           'awayTeam': away, 'startTime': start, 'source': 'conmebol_official',
           'sourceUrl': fixture_url, 'broadcasters': []}
    for side in ('home', 'away'):
        identities = _crest_ids(card.find(f'm-fixture-teaser-card__team-badge-{side}'))
        code = (countries or {}).get(next(iter(identities))) if len(identities) == 1 else None
        if code is not None:
            row[f'{side}Country'] = code
    return row


def parse_conmebol_fixtures(html, competition, source_url):
    """Parse only a validated official homepage's fixture strip, with an ART clock.

    A recognized empty strip is an empty result; missing/changed structure is an
    error. The source is a recent-fixture window, not a claim of whole-season
    coverage. Consumers must filter by the converted ART date before publishing.
    """
    slug = _source_slug(competition, source_url)
    if not isinstance(html, str) or len(html.encode('utf8')) > MAX_BODY:
        raise DiscoveryError('CONMEBOL: invalid or oversized HTML')
    tree = Tree(html)
    countries = _team_countries(tree, source_url, slug)
    strips = [node for node in tree.root.walk()
              if node.has_class('paragraph--type--tournament-fixtures-results')
              and node.has_class('m-tournament-strip')]
    if not strips or any(strip.find('m-tournament-strip__slider') is None for strip in strips):
        raise DiscoveryError('CONMEBOL: fixture structure not recognized')
    rows, country_conflicts = {}, {}
    for strip in strips:
        cards = [node for node in strip.walk() if node.has_class('m-fixture-teaser-card')]
        known_links = [node for node in strip.walk() if node.tag == 'a'
                       and '/fixture/view/' in node.attrs.get('href', '')]
        if not cards and known_links:
            raise DiscoveryError('CONMEBOL: fixture card structure changed')
        for card in cards:
            row = _card(card, competition, slug, source_url, countries)
            key = row['providerFixtureId']
            if key in rows:
                fields = ('homeCountry', 'awayCountry')
                previous = rows[key]
                if {name: value for name, value in previous.items() if name not in fields} != \
                        {name: value for name, value in row.items() if name not in fields}:
                    raise DiscoveryError('CONMEBOL: conflicting fixture duplicate')
                blocked = country_conflicts.setdefault(key, set())
                for field in fields:
                    if field in blocked:
                        row.pop(field, None)
                    elif field in previous and field in row and previous[field] != row[field]:
                        row.pop(field)
                        blocked.add(field)
                    elif field in previous:
                        row.setdefault(field, previous[field])
            rows[key] = row
    return sorted(rows.values(), key=lambda row: (row['startTime'], row['providerFixtureId']))
