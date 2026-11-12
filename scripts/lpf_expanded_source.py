"""Local official-bot adapters. Primera and Proyección remain separate categories."""
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from .broadcaster_sources import (DiscoveryError, MONTHS, Tree, ZONE,
    _lpf_article_text, lpf_listings, norm, public_url, safe_text)

COMPETITIONS = {
    'Liga Profesional Argentina': ('primera', 'lpf-primera'),
    'Torneo Proyección': ('proyeccion', 'lpf-proyeccion'),
}


def article_context(url, category):
    public_url(url)
    part = urlsplit(url)
    match = re.fullmatch(r'/notas/' + category + r'/(\d{4})/(\d{2})/(\d{2})/[^/]+/', part.path)
    if part.hostname != 'www.ligaprofesional.ar' or not match:
        raise DiscoveryError('LPF_CATEGORY_CONTEXT_INVALID')
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        raise DiscoveryError('LPF_PUBLICATION_DATE_INVALID') from None


def article_links(html, competition, index_url, day):
    if competition not in COMPETITIONS:
        raise DiscoveryError('LPF_COMPETITION_UNSUPPORTED')
    category = COMPETITIONS[competition][0]
    expected = 'https://www.ligaprofesional.ar/categoria/' + category + '/'
    if index_url != expected:
        raise DiscoveryError('LPF_INDEX_CONTEXT_INVALID')
    links = set()
    for node in Tree(html).root.walk():
        if node.tag != 'a':
            continue
        url = urljoin(index_url, node.attrs.get('href', ''))
        try:
            published = article_context(url, category)
        except DiscoveryError:
            continue
        if -14 <= (day - published).days <= 120 and re.search(
                r'\b(agenda|programacion|cambios|dias y horarios)\b', norm(url + ' ' + node.text())):
            links.add(url)
    return sorted(links, reverse=True)


def proyeccion_listings(html, day, source_url):
    """Only explicit schedule rows; stadium suffixes and result rows are not teams/TV."""
    published = article_context(source_url, 'proyeccion')
    tree = Tree(html)
    article = next((node for node in tree.root.walk() if node.has_class('entry-content') or
                    node.has_class('elementor-widget-theme-post-content')), None)
    if article is None:
        raise DiscoveryError('LPF_ARTICLE_UNRECOGNIZED')
    text = re.sub(r'[^\S\n]+', ' ', _lpf_article_text(article))
    if not re.search(r'\b(torneo|clausura|apertura)\b.*\bproyeccion\b', norm(text)) or \
            re.search(r'\b(juveniles|femenin[ao])\b', norm(text)):
        raise DiscoveryError('LPF_CATEGORY_CONTEXT_INVALID')
    years = set(re.findall(r'\b20\d{2}\b', text))
    if years != {str(day.year)} and not (not years and published.year == day.year and
                                         0 <= (day - published).days <= 92):
        raise DiscoveryError('LPF_AGENDA_YEAR_AMBIGUOUS')
    current, recognized, rows = None, False, []
    heading = re.compile(r'^(?:Lunes|Martes|Miércoles|Miercoles|Jueves|Viernes|Sábado|Sabado|Domingo)\s+(\d{1,2})[°º]?\s+de\s+(\w+)\s*$', re.I)
    signal = r'(?:ESPN(?: Premium|\s*\d+)?|TNT Sports(?: Premium)?|TyC Sports(?: Play)?|FOX Sports(?: Argentina|\s*\d+)?|DSports(?:\s*\d+|\+)?|Telefe|TV Pública|Disney\+|LPF Play)'
    signals = signal + r'(?:\s*/\s*' + signal + r')*'
    for line in text.splitlines():
        line = line.strip()
        match = heading.fullmatch(line)
        if match:
            recognized = True
            try:
                current = date(day.year, MONTHS[norm(match[2])], int(match[1]))
            except (ValueError, KeyError):
                current = None
            continue
        clock = re.match(r'^(\d{1,2})[.:](\d{2})\s+(.+)$', line)
        if current != day or not clock:
            continue
        body, broadcasters = clock[3], []
        # A broadcaster must occur in this fixture's own row, never global navigation.
        for wrapper in (r'\((' + signals + r')\)',
                        r'(?:^|\s)[–—-]\s*(' + signals + r')\s*[–—-](?=\s|$)'):
            for tv in re.finditer(wrapper, body, re.I):
                broadcasters.extend(re.split(r'\s*/\s*', tv[1]))
            body = re.sub(wrapper, '', body, flags=re.I)
        body = re.split(r'\s*\((?:Zona\b|Interzonal\b)', body, maxsplit=1, flags=re.I)[0].strip()
        teams = re.split(r'\s+[–—-]\s+', body)
        if len(teams) != 2 or any(re.search(r'\d', team) or not safe_text(team) for team in teams):
            continue
        try:
            kickoff = datetime(day.year, day.month, day.day, int(clock[1]), int(clock[2]), tzinfo=ZONE)
        except ValueError:
            continue
        rows.append((teams[0], teams[1], kickoff, list(dict.fromkeys(broadcasters))))
    if not recognized:
        raise DiscoveryError('LPF_AGENDA_DATES_MISSING')
    return rows


def parse_lpf_fixtures(html, competition, source_url, day, days=30):
    if competition not in COMPETITIONS or not 0 <= days <= 31:
        raise DiscoveryError('LPF_COMPETITION_UNSUPPORTED')
    category, code = COMPETITIONS[competition]
    article_context(source_url, category)
    rows = []
    for offset in range(days + 1):
        target = day + timedelta(days=offset)
        if target.year != day.year:
            break  # Never infer next year's date from an article with no explicit year.
        if category == 'primera':
            listings = lpf_listings(html, target, require_broadcaster=False, source_url=source_url)
            items = [(item.home, item.away, item.kickoff, list(item.broadcasters)) for item in listings]
        else:
            items = proyeccion_listings(html, target, source_url)
        for home, away, start, broadcasters in items:
            # Same namespace/row identity as the already-published Primera provider.
            identity = json.dumps([competition, target.year, target.isoformat(), norm(home), norm(away)], ensure_ascii=True)
            key = 'lpf:' + hashlib.sha256(identity.encode('utf8')).hexdigest()
            rows.append({'providerFixtureId': key, 'competition': competition, 'competitionId': code,
                'homeTeam': home, 'awayTeam': away, 'startTime': start, 'source': 'lpf_official',
                'sourceUrl': source_url, 'broadcasters': broadcasters})
    return rows
