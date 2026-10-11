"""Offline comparison only. External data never supplies broadcast evidence."""
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('America/Argentina/Buenos_Aires')
COMPETITIONS = {
    'lg_22EEYKR': {'state': 'LIMITED_AUXILIARY', 'competition': 'Liga Profesional Argentina'},
    'lg_20B3GRJ': {'state': 'DATA_QUALITY_WARNING', 'competition': 'CONMEBOL Libertadores'},
}


def local_start(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Explicit timezone required')
    return value.astimezone(ZONE)


def compare(official, provider, target_date, team_aliases=None):
    """Full names or explicit unambiguous aliases; zero time tolerance.

    Input is a normalized evaluation record, not a provider network adapter.
    Only a matching record may contribute a secondary providerFixtureId.
    Scores, status, venue and broadcaster hints are never copied.
    """
    aliases = team_aliases or {}
    def team(name):
        # Caller supplies verified aliases; no substring/fuzzy matching.
        return aliases.get(name, name)
    policy = COMPETITIONS.get(provider.get('competitionId'))
    result = {'state': policy['state'] if policy else 'UNVALIDATED',
              'match': False, 'allowedFields': {}, 'conflicts': []}
    if not policy:
        return result
    try:
        left, right = local_start(official['startTime']), local_start(provider['startTime'])
        if left.date() != target_date or right.date() != target_date:
            result['conflicts'].append('date')
        if left != right:
            result['conflicts'].append('kickoff')
        if official['competition'] != policy['competition']:
            result['conflicts'].append('competition')
        for field in ('homeTeam', 'awayTeam'):
            if not isinstance(provider.get(field), str) or team(official[field]) != team(provider[field]):
                result['conflicts'].append(field)
    except (KeyError, TypeError, ValueError):
        result['conflicts'].append('invalid_record')
    result['match'] = not result['conflicts']
    if result['match'] and isinstance(provider.get('providerFixtureId'), str) and provider['providerFixtureId']:
        result['allowedFields'] = {'providerFixtureId': provider['providerFixtureId']}
    return result
