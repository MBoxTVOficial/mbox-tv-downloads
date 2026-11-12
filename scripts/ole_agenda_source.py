"""Olé Argentina agenda snapshots; explicit fixture TV only, no network or inference."""
from datetime import date
from html.parser import HTMLParser
import json
import re

from .broadcaster_sources import DiscoveryError, Listing, instant, safe_text

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
        key = (home, away, kickoff, competition)
        if key in listings:
            channels = tuple(dict.fromkeys(listings[key].broadcasters + channels))
        listings[key] = Listing(home, away, kickoff, competition, channels)
    return sorted(listings.values(), key=lambda item: (item.kickoff, item.competition, item.home, item.away))
