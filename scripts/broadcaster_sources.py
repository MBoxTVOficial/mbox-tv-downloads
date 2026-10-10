"""Bounded public schedule adapters. A lookup never requests one URL per fixture."""
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlsplit, urljoin
from threading import Lock
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

def local_zone(name, hours):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=hours), name)


ZONE = local_zone('America/Argentina/Buenos_Aires', -3)
COLOMBIA = local_zone('America/Bogota', -5)
MAX_BODY = 2_000_000


class DiscoveryError(ValueError):
    pass


def norm(value):
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFKD', value.replace('+', ' plus ')).casefold()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).split())


def safe_text(value, limit=160):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and \
        '://' not in value and not any(ord(c) < 32 for c in value)


def public_url(value):
    try:
        part = urlsplit(value)
        if part.scheme != 'https' or not part.hostname or part.username or part.password or \
                part.query or part.fragment or part.port not in (None, 443) or \
                part.hostname == 'localhost' or part.hostname.endswith(('.local', '.internal')) or \
                any(word in value.casefold() for word in ('player_api.php', 'get.php', 'username=', 'password=')):
            raise ValueError()
        try:
            ipaddress.ip_address(part.hostname)
        except ValueError:
            pass
        else:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise DiscoveryError('Provider requiere URL HTTPS pública sin credenciales/parámetros.') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PublicTransport:
    """No retries/redirects. Bounded body, socket timeout and elapsed-time checks."""
    def __init__(self, timeout=8):
        if type(timeout) is not int or not 1 <= timeout <= 8:
            raise DiscoveryError('Timeout público debe ser entero entre 1 y 8 segundos.')
        self.timeout = timeout

    def fetch(self, url):
        public_url(url)
        # OS DNS can outlive socket timeouts. A short-lived worker gives every
        # public request an actual wall-clock bound, including DNS and TLS.
        try:
            result = subprocess.run([sys.executable, __file__, '--fetch-public', url, str(self.timeout)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        except subprocess.TimeoutExpired:
            raise TimeoutError('provider_deadline') from None
        if result.returncode != 0 or len(result.stdout) > MAX_BODY:
            raise DiscoveryError('No se pudo leer programación pública.')
        return result.stdout.decode('utf-8', errors='strict')

    def _fetch(self, url):
        public_url(url)
        host = urlsplit(url).hostname
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise DiscoveryError('Provider no resuelve a un servidor público.')
        deadline = time.monotonic() + self.timeout
        request = Request(url, headers={'User-Agent': 'MBoxSportsSchedule/1.0', 'Accept': 'text/html,application/json'})
        with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError('provider_deadline')
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY:
                    raise DiscoveryError('Provider excede límite de respuesta.')
                chunks.append(chunk)
            charset = response.headers.get_content_charset() or 'utf-8'
            return b''.join(chunks).decode(charset, errors='strict')


@dataclass(frozen=True)
class Fixture:
    fixture_id: int
    home: str
    away: str
    competition: str
    kickoff: datetime
    section: str = ''
    country: str = ''

    @property
    def fingerprint(self):
        return [self.home, self.away, self.competition, self.kickoff.isoformat()]


@dataclass(frozen=True)
class Listing:
    home: str
    away: str
    kickoff: datetime
    competition: str
    broadcasters: tuple


@dataclass(frozen=True)
class Evidence:
    fixture_id: int
    kickoff: str
    broadcaster: str
    territory: str
    source_name: str
    owner: str
    official: bool
    trusted: bool
    url: str
    source_type: str = ''


def instant(value):
    if not isinstance(value, str):
        raise DiscoveryError('La programación requiere timestamp textual con zona.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise DiscoveryError('La programación requiere fecha/hora con zona.')
    return parsed.astimezone(ZONE)


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return ' '.join(' '.join(child.text() if isinstance(child, Node) else child for child in self.children).split())

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()

    def find(self, name):
        return next((node for node in self.walk() if node.has_class(name)), None)


class Tree(HTMLParser):
    VOID = {'img', 'meta', 'input', 'br', 'hr', 'link', 'source', 'area', 'wbr', 'embed', 'param', 'base', 'col'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.count = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 40000:
            raise DiscoveryError('HTML excede límite de nodos.')
        ancestor, depth = self.current, 0
        while ancestor.parent is not None:
            ancestor, depth = ancestor.parent, depth + 1
        if depth > 80:
            raise DiscoveryError('HTML excede profundidad permitida.')
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


MONTHS = dict(zip(('enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre').split(), range(1, 13)))


class RequestBudget:
    """Shared hard GET budget, including linked articles; no redirects or retries."""
    def __init__(self, transport, maximum):
        self.transport, self.maximum, self.requests = transport, maximum, 0
        self.lock = Lock()

    def fetch(self, url):
        with self.lock:
            if self.requests >= self.maximum:
                raise DiscoveryError('Límite de requests públicos alcanzado.')
            self.requests += 1
        return self.transport.fetch(url)


def lpf_listings(html, day):
    """Official agenda only: publication date is never used as the match date."""
    tree = Tree(html)
    article = next((n for n in tree.root.walk() if n.has_class('entry-content') or
                    n.has_class('elementor-widget-theme-post-content')), None)
    if article is None:
        raise DiscoveryError('LPF: agenda sin contenido reconocido.')
    text = article.text()
    if not re.search(r'\b(clausura|apertura|liga profesional)\b', norm(text)):
        raise DiscoveryError('LPF: competición de agenda no identificada.')
    if set(re.findall(r'\b20\d{2}\b', text)) != {str(day.year)}:
        raise DiscoveryError('LPF: año de agenda ambiguo o ausente.')
    current, result = None, []
    pattern = r'(?P<date>\b(?:Lunes|Martes|Miércoles|Miercoles|Jueves|Viernes|Sábado|Sabado|Domingo)\s+\d{1,2}\s+de\s+\w+)|(?P<clock>\b\d{1,2}[.:]\d{2}\s+)'
    tokens = list(re.finditer(pattern, text, re.IGNORECASE))
    for index, token in enumerate(tokens):
        end = tokens[index + 1].start() if index + 1 < len(tokens) else len(text)
        if token.group('date'):
            heading = norm(token.group('date')).split()
            try:
                current = date(day.year, MONTHS[heading[-1]], int(heading[-3]))
            except (KeyError, ValueError):
                current = None
            continue
        if current != day:
            continue
        body = text[token.end():end].strip()
        signals = re.findall(r'\((ESPN(?: Premium|\s*\d+)?|TNT Sports(?: Premium)?|TyC Sports(?: Play)?|'
                             r'FOX Sports(?: Argentina|\s*\d+)?|DSports(?:\s*\d+|\+)?|Telefe|TV Pública|Disney\+)\)',
                             body, re.IGNORECASE)
        teams = re.split(r'\s+[–—-]\s+', body.split('(')[0].strip())
        if not signals or len(teams) != 2 or not all(safe_text(name) for name in teams):
            continue
        try:
            hour, minute = map(int, token.group('clock').strip().replace('.', ':').split(':'))
            kickoff = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)
        except ValueError:
            continue
        result.append(Listing(*teams, kickoff, 'Liga Profesional Argentina', tuple(signals)))
    return result


def tyc_listings(html, day):
    tree = Tree(html)
    sections = [node for node in tree.root.walk() if node.has_class('agenda_results')]
    if not sections:
        raise DiscoveryError('TyC agenda: estructura no reconocida.')
    result = []
    for section in sections:
        header = next((node.text() for node in section.walk() if node.tag == 'h2'), '')
        match = re.search(r'(\d{1,2}) de ([a-z]+) del (\d{4})', norm(header))
        if not match or match[2] not in MONTHS or date(int(match[3]), MONTHS[match[2]], int(match[1])) != day:
            continue
        for tournament in (node for node in section.walk() if node.has_class('agenda_comp_results')):
            if tournament.attrs.get('data-selectdeporte') != 'futbol':
                continue
            heading = tournament.find('header_comp_agenda')
            for item in (node for node in tournament.walk() if node.has_class('item_agenda')):
                teams, clock, tv = item.find('text-teams-agenda'), item.find('hs-item-agenda'), item.find('text-channels-agenda')
                if not teams or not clock or not tv:
                    continue
                names = [node.attrs.get('alt', '') for node in teams.walk() if node.tag == 'img']
                channels = tuple(node.text() for node in tv.walk() if node.tag == 'span' and safe_text(node.text()))
                if len(names) != 2 or not channels or not re.fullmatch(r'\d{2}:\d{2}', clock.text()):
                    continue
                result.append(Listing(*names, instant(f'{day}T{clock.text()}:00-03:00'),
                                      heading.text() if heading else '', channels))
    return result


def espn_listings(html, day):
    match = re.search(r"window(?:\[['\"]__espnfitt__['\"]\]|\.__espnfitt__)\s*=\s*", html)
    if not match:
        raise DiscoveryError('ESPN agenda: datos estructurados no encontrados.')
    data, _ = json.JSONDecoder().raw_decode(html[match.end():])
    content = data.get('data', data)['page']['content']
    events = content.get('events')
    if not isinstance(events, list):
        raise DiscoveryError('ESPN agenda: events inválido.')
    result = []
    for tournament in events:
        for item in tournament if isinstance(tournament, list) else [tournament]:
            competitors = item.get('competitors', [])
            home = [team for team in competitors if team.get('isHome') is True]
            away = [team for team in competitors if team.get('isHome') is False]
            channels = []
            for broadcast in item.get('broadcasts', []):
                if isinstance(broadcast, str):
                    channels.append(broadcast)
                elif isinstance(broadcast, dict):
                    # Explicit listing labels only; stream/watch URLs and package logos are ignored.
                    if safe_text(broadcast.get('name')):
                        channels.append(broadcast['name'])
                    channels.extend(name for name in broadcast.get('names', []) if safe_text(name))
            kickoff = instant(item['date'])
            if len(home) == len(away) == 1 and channels and kickoff.date() == day:
                result.append(Listing(home[0]['displayName'], away[0]['displayName'], kickoff,
                                      str(item.get('league', '')), tuple(channels)))
    return result


def win_listings(html, day):
    tree = Tree(html)
    # The official page gives today's Colombian date without a year. Only use it on that actual day.
    colombia_today = datetime.now(COLOMBIA).date()
    if day != colombia_today:
        return []
    headers = [norm(node.text()) for node in tree.root.walk() if node.tag == 'h2']
    month = next(name for name, number in MONTHS.items() if number == day.month)
    if not any(re.search(rf'\b0?{day.day} de {month}\b', header) for header in headers):
        raise DiscoveryError('Win agenda: fecha actual no verificada.')
    result = []
    blocks = []
    for title in (node for node in tree.root.walk() if node.tag == 'h3'):
        node = title.parent
        for _ in range(5):
            if node is None or node is tree.root:
                break
            if re.search(r'\bvs\.?\b.+\d{1,2}:\d{2}', node.text(), re.I):
                blocks.append(node)
                break
            node = node.parent
    for node in blocks:
        text = node.text()
        pair = re.search(r'([^\n]+?)\s+vs\.?\s+(.+?)\s+(\d{1,2}:\d{2})\s*(AM|PM)\b', text, re.I)
        titles = [child.text() for child in node.walk() if child.tag == 'h3']
        if not pair or len(titles) != 1:
            continue
        # Only the smallest programme block with its title, both teams, clock and explicit TV label.
        if any(isinstance(child, Node) and re.search(r'\bvs\.?\b.+\d{1,2}:\d{2}', child.text(), re.I)
               and any(n.tag == 'h3' for n in child.walk()) for child in node.children):
            continue
        home = pair[1].removeprefix(titles[0]).strip()
        away = pair[2].strip()
        tail = text[pair.end():].strip()
        # No inference from Win Play buttons, league ownership or logos.
        if not home or not away or not safe_text(tail) or re.search(r'\b(ver online|winplay|win play)\b', norm(tail)):
            continue
        clock = datetime.strptime(pair[3]+' '+pair[4].upper(), '%I:%M %p').time()
        kickoff = datetime.combine(day, clock, COLOMBIA).astimezone(ZONE)
        result.append(Listing(home, away, kickoff, titles[0], (tail,)))
    return result


def jsonld_listings(html, day):
    tree = Tree(html)
    result = []
    def walk(value):
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
    for script in tree.root.walk():
        if script.tag != 'script' or script.attrs.get('type') != 'application/ld+json':
            continue
        for item in walk(json.loads(script.text())):
            if item.get('@type') != 'BroadcastEvent':
                continue
            event, channel = item.get('broadcastOfEvent', {}), item.get('publishedOn', {})
            if event.get('@type') != 'SportsEvent' or not safe_text(channel.get('name')):
                continue
            kickoff = instant(event['startDate'])
            if kickoff.date() == day:
                result.append(Listing(event['homeTeam']['name'], event['awayTeam']['name'], kickoff,
                                      event.get('name', ''), (channel['name'],)))
    return result


def football_tv_listings(html, day):
    """Colombian guide: dated table heading + displayed Colombian clock only.

    Its timezone-less JSON-LD clock differs from the displayed clock, so it is
    deliberately not used. Never infer from competition statistics or links.
    """
    tree = Tree(html)
    rows = [node for node in tree.root.walk() if node.tag == 'tr']
    if not any(row.has_class('cabeceraTabla') for row in rows):
        raise DiscoveryError('Guía TV: tabla/fecha no reconocida.')
    current_day, competition, result = None, '', []
    for row in rows:
        if row.has_class('cabeceraTabla'):
            match = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', row.text())
            current_day = date(int(match[3]), int(match[2]), int(match[1])) if match else None
            continue
        if row.has_class('cabeceraCompericion'):
            competition = row.text()
            continue
        home, away, clock, tv = row.find('local'), row.find('visitante'), row.find('hora'), row.find('listaCanales')
        if current_day is None or not all((home, away, clock, tv)) or not re.fullmatch(r'\d{2}:\d{2}', clock.text()):
            continue
        kickoff = datetime.combine(current_day, datetime.strptime(clock.text(), '%H:%M').time(), COLOMBIA).astimezone(ZONE)
        channels = tuple(node.text() for node in tv.walk() if node.tag == 'li' and safe_text(node.text()))
        if kickoff.date() == day and channels and safe_text(home.text()) and safe_text(away.text()):
            result.append(Listing(home.text(), away.text(), kickoff, competition, channels))
    return result


PARSERS = {'lpf_agenda': lpf_listings, 'tyc_agenda': tyc_listings, 'espn_schedule': espn_listings,
           'win_schedule': win_listings, 'jsonld': jsonld_listings, 'football_tv': football_tv_listings}


class BroadcasterProvider:
    def __init__(self, config, aliases):
        self.config, self.aliases = config, aliases
        self.sourceName = config['id']
        self.listings = []
        self.fixture_context = []
        self.listing_urls = {}

    def bind_fixtures(self, fixtures):
        self.fixture_context = [fixture for fixture in fixtures if self.supports(fixture)]

    def supports(self, fixture):
        if self.config['adapter'] == 'lpf_agenda' and fixture.country and norm(fixture.country) != 'argentina':
            return False
        competitions = self.config.get('competitions', [])
        return not competitions or norm(fixture.competition) in {norm(value) for value in competitions}

    def prepare(self, day, transport):
        if self.config['adapter'] == 'lpf_agenda':
            tree = Tree(transport.fetch(self.config['url']))
            links = []
            for node in tree.root.walk():
                if node.tag != 'a' or not re.search(r'\b(agenda|programacion|cambios)\b', norm(node.text())):
                    continue
                link = urljoin(self.config['url'], node.attrs.get('href', ''))
                parsed = urlsplit(link)
                if parsed.hostname == 'www.ligaprofesional.ar' and re.fullmatch(
                        r'/notas/primera/\d{4}/\d{2}/\d{2}/[^/]+/', parsed.path):
                    if link not in links:
                        links.append(link)
            self.listings, self.listing_urls = [], {}
            if not links:
                raise DiscoveryError('LPF: no se encontraron enlaces de agenda oficiales.')
            # Newest articles first. At most two articles in addition to the index.
            for link in sorted(links, reverse=True)[:2]:
                for listing in lpf_listings(transport.fetch(link), day):
                    self.listings.append(listing)
                    self.listing_urls[listing] = link
            return
        self.listings = PARSERS[self.config['adapter']](transport.fetch(self.config['url']), day)

    def team(self, name):
        key = norm(name)
        return self.aliases.get(key, key)

    def lookup(self, fixture):
        found = []
        for listing in self.listings:
            if listing.kickoff != fixture.kickoff:
                continue
            exact = self.team(listing.home) == self.team(fixture.home) and self.team(listing.away) == self.team(fixture.away)
            if self.config['adapter'] == 'lpf_agenda':
                # Publisher short labels require a unique complete pair, competition and instant.
                # Compare full tokens, never substrings; reject youth/women/reserve identities.
                def team_match(label, name):
                    left, right = self.team(label).split(), self.team(name).split()
                    variants = {'women', 'femenino', 'femenina', 'reserves', 'reserva', 'olympic'}
                    if any(t in variants or re.fullmatch(r'u\d+', t) for t in left + right):
                        return False
                    if not left or not set(left) - {'club', 'fc', 'ca', 'cd', 'deportivo', 'atletico'}:
                        return False
                    return left == right or (len(left) < len(right) and right[:len(left)] == left)
                names = {name for f in self.fixture_context for name in (f.home, f.away)}
                if any(len({self.team(name) for name in names if team_match(label, name)}) != 1
                       for label in (listing.home, listing.away)):
                    continue
                matches = [f for f in self.fixture_context if f.kickoff == listing.kickoff and
                           team_match(listing.home, f.home) and team_match(listing.away, f.away)]
                if len(matches) != 1 or matches[0].fixture_id != fixture.fixture_id:
                    continue
            elif not exact:
                continue
            for name in listing.broadcasters:
                if not safe_text(name):
                    continue
                owned = norm(name) in {norm(value) for value in self.config.get('ownedBroadcasters', [])}
                found.append(Evidence(fixture.fixture_id, fixture.kickoff.isoformat(), name,
                    self.config['territory'], self.sourceName, self.config['independenceKey'],
                    self.config['kind'] in ('official_league', 'official_club') or
                    self.config['kind'] == 'official_broadcaster' and owned,
                    True, self.listing_urls.get(listing, self.config['url']), self.config['kind']))
        return found


if __name__ == '__main__':
    # Restricted worker: one public GET, no retries, no credential support.
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--fetch-public':
            raise DiscoveryError('Invalid worker invocation')
        timeout = int(sys.argv[3])
        if not 1 <= timeout <= 8:
            raise DiscoveryError('Invalid timeout')
        data = PublicTransport(timeout)._fetch(sys.argv[2]).encode('utf-8')
        if len(data) > MAX_BODY:
            raise DiscoveryError('Oversized response')
        sys.stdout.buffer.write(data)
    except Exception:
        sys.exit(1)
