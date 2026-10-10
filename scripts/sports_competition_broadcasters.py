"""Versioned rights are search hints, never fixture confirmation or stream IDs."""
from datetime import date
import re

if __package__:
    from .broadcaster_sources import DiscoveryError, norm, public_url, safe_text
else:
    from broadcaster_sources import DiscoveryError, norm, public_url, safe_text


def territory(value):
    return {'ar': 'argentina', 'br': 'brazil', 'brasil': 'brazil', 'mx': 'mexico',
            'co': 'colombia', 'cl': 'chile'}.get(norm(value), norm(value))


def validate_registry(value):
    if not isinstance(value, dict) or type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1 or \
            set(value) != {'schemaVersion', 'territory', 'competitions'} or value['territory'] != 'AR' or \
            not isinstance(value['competitions'], list):
        raise DiscoveryError('Registro de derechos inválido.')
    seen = set()
    for item in value['competitions']:
        required = {'competitionId', 'name', 'aliases', 'season', 'territory', 'validFrom', 'validUntil',
                    'validUntilSeason', 'officialBroadcasters', 'competitionCountry'}
        if not isinstance(item, dict) or set(item) != required or any(not safe_text(item[k]) for k in
                ('competitionId', 'name', 'season', 'territory')) or not isinstance(item['aliases'], list) or \
                any(not safe_text(a) for a in item['aliases']) or item['territory'] != 'AR' or \
                not isinstance(item['competitionCountry'], str):
            raise DiscoveryError('Competición del registro inválida.')
        key = (item['competitionId'], item['season'], item['territory'])
        if key in seen:
            raise DiscoveryError('Competición/temporada/territorio duplicados.')
        seen.add(key)
        try:
            for field in ('validFrom', 'validUntil'):
                if item[field] is not None:
                    if date.fromisoformat(item[field]).isoformat() != item[field]:
                        raise ValueError()
            if item['validFrom'] and item['validUntil'] and item['validFrom'] > item['validUntil']:
                raise ValueError()
            if item['validUntilSeason'] is not None and not safe_text(item['validUntilSeason']):
                raise ValueError()
        except (ValueError, TypeError):
            raise DiscoveryError('Vigencia de derechos inválida.') from None
        if not isinstance(item['officialBroadcasters'], list):
            raise DiscoveryError('Broadcasters del registro inválidos.')
        names = set()
        for signal in item['officialBroadcasters']:
            fields = {'name', 'aliases', 'priority', 'sourceType', 'sourceUrl', 'sourceDate', 'notes'}
            if not isinstance(signal, dict) or set(signal) != fields or not safe_text(signal['name']) or \
                    norm(signal['name']) in names or not isinstance(signal['aliases'], list) or \
                    any(not safe_text(a) for a in signal['aliases']) or type(signal['priority']) is not int or \
                    signal['priority'] <= 0 or signal['sourceType'] not in ('official', 'official_broadcaster') or \
                    not isinstance(signal['notes'], str) or len(signal['notes']) > 2000:
                raise DiscoveryError('Titular de derechos inválido.')
            public_url(signal['sourceUrl'])
            if signal['sourceDate'] is not None:
                try:
                    date.fromisoformat(signal['sourceDate'])
                except (TypeError, ValueError):
                    raise DiscoveryError('Fecha de fuente inválida.') from None
            names.add(norm(signal['name']))
    return value


def candidates(registry, competition, day, market='AR', season=None, country=''):
    """Without a known season, only explicit date validity can establish current rights."""
    result = []
    for item in (registry or {}).get('competitions', []):
        if country and item['competitionCountry'] and norm(country) != norm(item['competitionCountry']):
            continue
        if territory(item['territory']) != territory(market) or norm(competition) not in {
                norm(a) for a in [item['name']] + item['aliases']}:
            continue
        if (item['validFrom'] and day.isoformat() < item['validFrom']) or \
                (item['validUntil'] and day.isoformat() > item['validUntil']):
            continue
        if season is None and not (item['validFrom'] and item['validUntil']):
            continue
        if season is not None and season != item['season']:
            continue
        result.extend(sorted(item['officialBroadcasters'], key=lambda s: s['priority']))
    return result


def search_hints(registry, competition, day, country=''):
    """Unknown expiry/season remains an explicitly unverified hint; never a confidence vote."""
    result = []
    for item in (registry or {}).get('competitions', []):
        if country and item['competitionCountry'] and norm(country) != norm(item['competitionCountry']):
            continue
        start_years = re.findall(r'\d{4}', item['season'])
        end_season = item['validUntilSeason'] or item['season']
        end_match = re.fullmatch(r'(\d{4})(?:/(\d{2}))?', end_season)
        if not start_years or not end_match:
            continue
        last_year = int(end_match[1]) + (1 if end_match[2] else 0)
        if not int(start_years[0]) <= day.year <= last_year:
            continue  # Unknown expiry is never treated as perpetual rights.
        if norm(competition) in {norm(a) for a in [item['name']] + item['aliases']} and \
                (not item['validFrom'] or item['validFrom'] <= day.isoformat()) and \
                (not item['validUntil'] or day.isoformat() <= item['validUntil']):
            result.extend(sorted(item['officialBroadcasters'], key=lambda s: s['priority']))
    return list(dict.fromkeys(signal['name'] for signal in result))
