"""Official Primera agenda, bounded public reads, no API credentials or match states."""
import copy
import hashlib
import json

from .broadcaster_sources import PublicTransport, DiscoveryError, lpf_article_links, lpf_listings, norm
from .football_data_provider import ProviderError

INDEX_URL = 'https://www.ligaprofesional.ar/categoria/primera/'
COMPETITION = 'Liga Profesional Argentina'


class LpfOfficialProvider:
    """Three GETs per instance, even when an outer shared budget is supplied."""
    def __init__(self, transport=None):
        self.transport = transport if transport is not None else PublicTransport(timeout=8)
        self.requests = 0
        self.cache = {}

    def _fetch(self, url):
        if self.requests >= 3:
            raise ProviderError('LPF_OFFICIAL_REQUEST_LIMIT')
        self.requests += 1
        return self.transport.fetch(url)

    def matches(self, code, day):
        if code != 'lpf-primera':
            raise ProviderError('INVALID_COMPETITION')
        key = (code, day.isoformat())
        if key in self.cache:
            return copy.deepcopy(self.cache[key])
        try:
            links = lpf_article_links(self._fetch(INDEX_URL), INDEX_URL, day)
            if not links:
                raise ProviderError('LPF_OFFICIAL_UNAVAILABLE')
            rows, recognized = {}, 0
            for url in links[:2]:
                try:
                    listings = lpf_listings(self._fetch(url), day, require_broadcaster=False, source_url=url)
                except (DiscoveryError, TimeoutError, OSError, UnicodeError):
                    continue
                recognized += 1
                for item in listings:
                    # This is an internal source-row key, not an LPF-issued fixture ID.
                    identity = json.dumps([COMPETITION, day.year, day.isoformat(),
                                           norm(item.home), norm(item.away)], ensure_ascii=True)
                    identifier = 'lpf:' + hashlib.sha256(identity.encode('utf8')).hexdigest()
                    row = {'providerFixtureId': identifier, 'competition': COMPETITION,
                           'competitionId': code, 'homeTeam': item.home, 'awayTeam': item.away,
                           'startTime': item.kickoff.isoformat(), 'source': 'lpf_official',
                           'sourceUrl': url}
                    previous = rows.get(identifier)
                    if previous and previous['startTime'] != row['startTime']:
                        raise ProviderError('LPF_OFFICIAL_AMBIGUOUS')
                    rows.setdefault(identifier, row)
            if not recognized:
                raise ProviderError('LPF_OFFICIAL_UNAVAILABLE')
            result = sorted(rows.values(), key=lambda row: (row['startTime'], row['providerFixtureId']))
            self.cache[key] = result
            return copy.deepcopy(result)
        except (DiscoveryError, TimeoutError, OSError, UnicodeError):
            raise ProviderError('LPF_OFFICIAL_UNAVAILABLE') from None
