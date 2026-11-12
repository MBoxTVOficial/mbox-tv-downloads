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


def _card(card, competition, slug, source_url):
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
    return {'competition': competition, 'competitionId': slug,
            'providerFixtureId': f'{slug}:{identifier}', 'homeTeam': home,
            'awayTeam': away, 'startTime': start, 'source': 'conmebol_official',
            'sourceUrl': fixture_url, 'broadcasters': []}


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
    strips = [node for node in tree.root.walk()
              if node.has_class('paragraph--type--tournament-fixtures-results')
              and node.has_class('m-tournament-strip')]
    if not strips or any(strip.find('m-tournament-strip__slider') is None for strip in strips):
        raise DiscoveryError('CONMEBOL: fixture structure not recognized')
    rows = {}
    for strip in strips:
        cards = [node for node in strip.walk() if node.has_class('m-fixture-teaser-card')]
        known_links = [node for node in strip.walk() if node.tag == 'a'
                       and '/fixture/view/' in node.attrs.get('href', '')]
        if not cards and known_links:
            raise DiscoveryError('CONMEBOL: fixture card structure changed')
        for card in cards:
            row = _card(card, competition, slug, source_url)
            key = row['providerFixtureId']
            if key in rows and rows[key] != row:
                raise DiscoveryError('CONMEBOL: conflicting fixture duplicate')
            rows[key] = row
    return sorted(rows.values(), key=lambda row: (row['startTime'], row['providerFixtureId']))
