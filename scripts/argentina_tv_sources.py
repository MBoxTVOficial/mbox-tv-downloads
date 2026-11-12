"""Dated Argentine TV guides, separate from official fixture/rights authority.

Only the displayed Argentine clock is used. The first guide's timezone-less
structured clock differs from its visible clock and is deliberately ignored.
TyC is a listing publisher here: its brand never becomes an implied TV signal.
"""
from datetime import date, datetime
import re

from .broadcaster_sources import DiscoveryError, Listing, MAX_BODY, MONTHS, Tree, ZONE, norm, safe_text


ARGENTINA_TV_URL = 'https://www.futbolenvivoargentina.com/'
TYC_AGENDA_URL = 'https://www.tycsports.com/agenda-deportiva-hoy.html'
_UNCONFIRMED = {'por confirmar', 'canal por confirmar', 'a confirmar',
                'canal a confirmar', 'sin tv', 'sin transmision', 'sin informacion'}


def _tree(html, day, name):
    if type(day) is not date:
        raise DiscoveryError(name + ': target date must be an explicit date')
    if not isinstance(html, str) or len(html.encode('utf8')) > MAX_BODY:
        raise DiscoveryError(name + ': invalid or oversized HTML')
    return Tree(html)


def _kickoff(day, text):
    if not isinstance(text, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', text.strip()):
        return None
    hour, minute = map(int, text.strip().split(':'))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)


def _channels(node, tag):
    if node is None:
        return ()
    return tuple(dict.fromkeys(n.text() for n in node.walk()
                               if n.tag == tag and safe_text(n.text())
                               and norm(n.text()) not in _UNCONFIRMED))


def _merge(rows):
    # A page may repeat a row; repeated same-source markup is not independent evidence.
    result = {}
    for row in rows:
        key = row.home, row.away, row.kickoff, row.competition
        if key in result:
            old = result[key]
            row = Listing(row.home, row.away, row.kickoff, row.competition,
                          tuple(dict.fromkeys(old.broadcasters + row.broadcasters)))
        result[key] = row
    return sorted(result.values(), key=lambda row: (row.kickoff, row.competition, row.home, row.away))


def parse_argentina_tv_listings(html, day):
    """Parse the guide's dated tables without generalizing Femenino/Reserva/nationals.

    Missing TV is a valid agenda row with broadcasters=(). Missing time/teams are
    not safe fixtures. Recognized dated empty tables are valid empty results.
    """
    tree = _tree(html, day, 'Argentina TV')
    # The site reuses tablaPrincipal for statistics; only explicitly dated tables are agenda.
    tables = [n for n in tree.root.walk() if n.tag == 'table' and n.has_class('tablaPrincipal')
              and any(row.tag == 'tr' and row.has_class('cabeceraTabla') for row in n.walk())]
    if not tables:
        raise DiscoveryError('Argentina TV: dated table structure not recognized')
    result = []
    for table in tables:
        rows = [n for n in table.walk() if n.tag == 'tr']
        if not any(row.has_class('cabeceraTabla') for row in rows):
            raise DiscoveryError('Argentina TV: explicit date heading missing')
        current, competition = None, ''
        for row in rows:
            if row.has_class('cabeceraTabla'):
                match = re.search(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', row.text())
                if match is None:
                    raise DiscoveryError('Argentina TV: date/year not recognized')
                try:
                    current = date(int(match[3]), int(match[2]), int(match[1]))
                except ValueError:
                    raise DiscoveryError('Argentina TV: invalid date') from None
                competition = ''
                continue
            if row.has_class('cabeceraCompericion'):
                competition = row.text()
                continue
            if current != day:
                continue
            home, away, clock = row.find('local'), row.find('visitante'), row.find('hora')
            if home is None or away is None or clock is None:
                continue
            kickoff = _kickoff(current, clock.text())
            if kickoff is None or not all(safe_text(value) for value in (home.text(), away.text(), competition)):
                continue
            if norm(home.text()) == norm(away.text()):
                continue
            channels = _channels(row.find('listaCanales'), 'li')
            result.append(Listing(home.text(), away.text(), kickoff, competition, channels))
    return _merge(result)


def parse_tyc_listings(html, day):
    """Parse TyC's explicit day/competition sections, including matches with no TV.

    Caller must retain the engine's ownedBroadcasters check: an ESPN/Win listing
    on TyC is not official TyC transmission evidence. Non-football rows are ignored.
    """
    tree = _tree(html, day, 'TyC agenda')
    sections = [n for n in tree.root.walk() if n.has_class('agenda_results')]
    if not sections:
        raise DiscoveryError('TyC agenda: section structure not recognized')
    result = []
    for section in sections:
        heading = next((n.text() for n in section.walk() if n.tag == 'h2'), '')
        match = re.search(r'\b(\d{1,2}) de ([a-z]+) del (\d{4})\b', norm(heading))
        if match is None or match[2] not in MONTHS:
            raise DiscoveryError('TyC agenda: date/year not recognized')
        try:
            current = date(int(match[3]), MONTHS[match[2]], int(match[1]))
        except ValueError:
            raise DiscoveryError('TyC agenda: invalid date') from None
        if current != day:
            continue
        for tournament in (n for n in section.walk() if n.has_class('agenda_comp_results')):
            if tournament.attrs.get('data-selectdeporte') != 'futbol':
                continue
            header = tournament.find('header_comp_agenda')
            # The enclosing header also contains navigation links; they are not a league name.
            competition = next((n.text() for n in header.walk() if n.tag in ('h2', 'h3')), '') if header else ''
            for item in (n for n in tournament.walk() if n.has_class('item_agenda')):
                teams, clock = item.find('text-teams-agenda'), item.find('hs-item-agenda')
                if teams is None or clock is None:
                    continue
                names = [n.attrs.get('alt', '') for n in teams.walk() if n.tag == 'img']
                kickoff = _kickoff(current, clock.text())
                if len(names) != 2 or kickoff is None or not all(safe_text(value) for value in [*names, competition]):
                    continue
                if norm(names[0]) == norm(names[1]):
                    continue
                channels = _channels(item.find('text-channels-agenda'), 'span')
                result.append(Listing(*names, kickoff, competition, channels))
    return _merge(result)
