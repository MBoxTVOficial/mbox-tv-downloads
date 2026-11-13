"""Olé Argentina agenda snapshots; explicit fixture TV only, no network or inference."""
from datetime import date
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

from .broadcaster_sources import DiscoveryError, Listing, Tree, instant, norm, safe_text
from .sports_national_agenda import national_key
from .sports_team_logos import accepted_image_url

SOURCE_URL = 'https://www.ole.com.ar/agenda-deportiva'
MAX_HTML = 2_000_000
FOOTBALL_SECTION = 'eventos-argentina/futbol'


class _NextPayload(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.chunks = []
        self.count = 0
        self.canonicals = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'link' and attributes.get('rel') == 'canonical':
            self.canonicals.append(attributes.get('href', ''))
        if tag == 'script':
            self.active = (attributes.get('id') == '__NEXT_DATA__' and
                           attributes.get('type') == 'application/json')
            if self.active:
                self.count += 1

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, value):
        if self.active:
            self.chunks.append(value)


def _text(value):
    if not isinstance(value, str):
        raise DiscoveryError('OLE_INVALID_EVENT_TEXT')
    value = ' '.join(value.split())
    if not safe_text(value):
        raise DiscoveryError('OLE_INVALID_EVENT_TEXT')
    return value


def _broadcasters(event):
    channels = event.get('tagsChannel')
    if channels is None:
        return ()
    if not isinstance(channels, list):
        raise DiscoveryError('OLE_INVALID_CHANNEL_TAGS')
    names = []
    for channel in channels:
        if not isinstance(channel, dict):
            raise DiscoveryError('OLE_INVALID_CHANNEL_TAGS')
        name = _text(channel.get('name'))
        if name not in names:
            names.append(name)
    return tuple(names)


def _event_path(url):
    """Join HTML and JSON by the exact event URL, never a title substring."""
    if not isinstance(url, str):
        return None
    try:
        part = urlsplit(url)
        if part.query or part.fragment or part.username or part.password or part.port not in (None, 443):
            return None
        if part.netloc and (part.scheme != 'https' or part.hostname not in ('ole.com.ar', 'www.ole.com.ar')):
            return None
        if part.scheme and part.scheme != 'https':
            return None
        return part.path if re.fullmatch(r'/eventos-argentina/futbol/[^/]+\.html', part.path) else None
    except ValueError:
        return None


def _html_team_logos(html):
    """Read paired fixture-card raster images; ignore site navigation SVG badges."""
    found, ambiguous = {}, set()
    for card in (node for node in Tree(html).root.walk() if node.has_class('event-canal-box')):
        links = [node for node in card.walk() if node.tag == 'a' and node.has_class('event-link')]
        if len(links) != 1:
            continue
        path = _event_path(links[0].attrs.get('href'))
        if path is None:
            continue
        paired = []
        for side in ('home', 'visitor'):
            groups = [node for node in card.walk() if node.has_class('event-info_team_group') and node.has_class(side)]
            if len(groups) != 1:
                break
            names = [node.text() for node in groups[0].walk() if node.tag == 'h3' and node.has_class('event-name')]
            images = [node for node in groups[0].walk() if node.tag == 'img']
            if len(names) != 1 or len(images) != 1 or norm(images[0].attrs.get('alt')) != norm(names[0]):
                break
            paired.append((names[0], accepted_image_url('ole-agenda', images[0].attrs.get('src'))))
        if len(paired) == 2:
            if path in found and found[path] != paired:
                ambiguous.add(path)
            found[path] = paired
    return {path: pair for path, pair in found.items() if path not in ambiguous}


def _fixture_badges(event, home, away, images):
    pair = images.get(_event_path(event.get('url')))
    if pair is None:
        return '', ''
    # Source HTML sometimes names nations in English; only the explicit national
    # alias catalog may reconcile those names. Club names still require equality.
    def team(value):
        return norm(national_key(value) or value)
    if team(pair[0][0]) != team(home) or team(pair[1][0]) != team(away):
        return '', ''
    return pair[0][1], pair[1][1]


def parse_ole_listings(html, day):
    """Read only the canonical agenda's Next.js event records.

    UTC/offset timestamps are converted to ART before filtering the day.
    Exact source competition names retain Reserva, femenina and Nacional labels;
    platform/channel tags remain literal evidence, never inferred MBox groups.
    Missing channel tags are valid agenda rows with an empty broadcaster tuple.
    """
    if not isinstance(html, str) or len(html) > MAX_HTML or type(day) is not date:
        raise DiscoveryError('OLE_INVALID_INPUT')
    parser = _NextPayload()
    parser.feed(html)
    if parser.count != 1 or any(url.rstrip('/') != SOURCE_URL for url in parser.canonicals):
        raise DiscoveryError('OLE_UNRECOGNIZED_AGENDA')
    try:
        payload = json.loads(''.join(parser.chunks))
        if payload['query']['match'] != ['agenda-deportiva']:
            raise ValueError()
        events = payload['props']['pageProps']['matchConfig']['content']
        if not isinstance(events, list) or len(events) > 2_000:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise DiscoveryError('OLE_UNRECOGNIZED_AGENDA') from None
    listings = {}
    images = _html_team_logos(html)
    for event in events:
        if not isinstance(event, dict):
            raise DiscoveryError('OLE_INVALID_EVENT')
        if event.get('type') != 'event':
            continue
        sections = event.get('sections')
        if not isinstance(sections, list):
            raise DiscoveryError('OLE_INVALID_EVENT_SECTIONS')
        if not any(isinstance(section, dict) and isinstance(section.get('url'), str)
                   and section['url'].strip('/') == FOOTBALL_SECTION
                   for section in sections):
            continue
        try:
            kickoff = instant(event['startDate'])
        except (KeyError, TypeError, ValueError):
            raise DiscoveryError('OLE_INVALID_EVENT_KICKOFF') from None
        if kickoff.date() != day:
            continue
        competition_tag = event.get('tagsEvent')
        if not isinstance(competition_tag, dict):
            raise DiscoveryError('OLE_INVALID_COMPETITION')
        competition = _text(competition_tag.get('name'))
        title = _text(event.get('eventTitle'))
        # Only an explicit home-vs-away event title identifies the teams.
        teams = re.split(r'\s+vs\.?\s+', title, flags=re.I)
        if len(teams) != 2 or not all(safe_text(team, 100) for team in teams):
            continue
        home, away = teams
        if home.casefold() == away.casefold():
            continue
        channels = _broadcasters(event)
        home_logo, away_logo = _fixture_badges(event, home, away, images)
        key = (home, away, kickoff, competition)
        if key in listings:
            channels = tuple(dict.fromkeys(listings[key].broadcasters + channels))
            home_logo = listings[key].home_logo or home_logo
            away_logo = listings[key].away_logo or away_logo
        listings[key] = Listing(home, away, kickoff, competition, channels, home_logo, away_logo)
    return sorted(listings.values(), key=lambda item: (item.kickoff, item.competition, item.home, item.away))
