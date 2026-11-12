"""Exact senior representative-team names for bounded agenda classification.

This is an explicit, versioned alias catalog, not fuzzy matching or evidence of a
fixture/transmission. Unknown teams remain unknown. Confederation membership is
used only to disambiguate competition labels; it does not assert FIFA eligibility
(some CONCACAF representative teams are not FIFA members). Regional teams such as
País Vasco and youth, reserve, Olympic, B and women's variants are not aliases.
"""
import re
import unicodedata

CATALOG_VERSION = 1

_GROUPS = {
    'UEFA': {
        'Albania': (), 'Andorra': (), 'Armenia': (), 'Austria': (),
        'Azerbaiyán': ('Azerbaijan',), 'Bielorrusia': ('Belarus',),
        'Bélgica': ('Belgium',), 'Bosnia y Herzegovina': ('Bosnia', 'Bosnia-Herzegovina', 'Bosnia and Herzegovina'),
        'Bulgaria': (), 'Croacia': ('Croatia',), 'Chipre': ('Cyprus',),
        'Chequia': ('República Checa', 'Czech Republic', 'Czechia'),
        'Dinamarca': ('Denmark',), 'Inglaterra': ('England',), 'Estonia': (),
        'Islas Feroe': ('Faroe Islands',), 'Finlandia': ('Finland',), 'Francia': ('France',),
        'Georgia': (), 'Alemania': ('Germany',), 'Gibraltar': (), 'Grecia': ('Greece',),
        'Hungría': ('Hungary',), 'Islandia': ('Iceland',), 'Israel': (), 'Italia': ('Italy',),
        'Kazajistán': ('Kazajiztan', 'Kazakhstan'), 'Kosovo': (), 'Letonia': ('Latvia',),
        'Liechtenstein': (), 'Lituania': ('Lithuania',), 'Luxemburgo': ('Luxembourg',),
        'Malta': (), 'Moldavia': ('Moldova',), 'Montenegro': (),
        'Países Bajos': ('Paises Bajos', 'Holanda', 'Netherlands'),
        'Macedonia del Norte': ('Macedonia Norte', 'North Macedonia'),
        'Irlanda del Norte': ('Northern Ireland',), 'Noruega': ('Norway',),
        'Polonia': ('Poland',), 'Portugal': (),
        'Irlanda': ('República Irlanda', 'República de Irlanda', 'Republic of Ireland', 'Ireland'),
        'Rumanía': ('Romania', 'Rumania'), 'Rusia': ('Russia', 'Russian Federation'),
        'San Marino': (), 'Escocia': ('Scotland',), 'Serbia': (), 'Eslovaquia': ('Slovakia',),
        'Eslovenia': ('Slovenia',), 'España': ('Spain',), 'Suecia': ('Sweden',),
        'Suiza': ('Switzerland',), 'Turquía': ('Turkey', 'Türkiye'),
        'Ucrania': ('Ukraine',), 'Gales': ('Wales',),
    },
    'CONMEBOL': {
        'Argentina': (), 'Bolivia': (), 'Brasil': ('Brazil',), 'Chile': (),
        'Colombia': (), 'Ecuador': (), 'Paraguay': (), 'Perú': ('Peru',),
        'Uruguay': (), 'Venezuela': (),
    },
    'CONCACAF': {
        'Anguila': ('Anguilla',), 'Antigua y Barbuda': ('Antigua and Barbuda',),
        'Aruba': (), 'Bahamas': (), 'Barbados': (), 'Belice': ('Belize',),
        'Bermudas': ('Bermuda',), 'Bonaire': (),
        'Islas Vírgenes Británicas': ('British Virgin Islands',), 'Canadá': ('Canada',),
        'Islas Caimán': ('Cayman Islands',), 'Costa Rica': (), 'Cuba': (),
        'Curazao': ('Curaçao', 'Curacao'), 'Dominica': (),
        'República Dominicana': ('Rep. Dominicana', 'Dominican Republic'),
        'El Salvador': (), 'Guayana Francesa': ('French Guiana',),
        'Granada': ('Grenada',), 'Guadalupe': ('Guadeloupe',), 'Guatemala': (),
        'Guyana': (), 'Haití': ('Haiti',), 'Honduras': (), 'Jamaica': (),
        'Martinica': ('Martinique',), 'México': ('Mexico',), 'Montserrat': (),
        'Nicaragua': (), 'Panamá': ('Panama',), 'Puerto Rico': (),
        'San Cristóbal y Nieves': ('Saint Kitts and Nevis', 'St Kitts and Nevis'),
        'Santa Lucía': ('Saint Lucia', 'St Lucia'), 'Saint-Martin': ('Saint Martin',),
        'San Vicente y Granadinas': ('Saint Vincent and the Grenadines',),
        'Sint Maarten': (), 'Surinam': ('Suriname',),
        'Trinidad y Tobago': ('Trinidad and Tobago',),
        'Islas Turcas y Caicos': ('Turks and Caicos Islands',),
        'Islas Vírgenes de Estados Unidos': ('US Virgin Islands', 'United States Virgin Islands'),
        'Estados Unidos': ('United States', 'United States of America', 'USA'),
    },
    # Deliberately partial outside the confederations relevant to this phase.
    'AFC': {
        'Japón': ('Japan',), 'Corea del Sur': ('South Korea', 'Korea Republic', 'Republic of Korea'),
        'Australia': (), 'Irán': ('Iran',), 'Irak': ('Iraq',),
        'Arabia Saudita': ('Saudi Arabia',), 'Qatar': ('Catar',),
        'Emiratos Árabes Unidos': ('United Arab Emirates', 'UAE'),
        'Singapur': ('Singapore',), 'Kuwait': (), 'Uzbekistán': ('Uzbekistan',),
        'China': ('China PR', 'PR China'),
    },
    'CAF': {
        'Marruecos': ('Morocco',), 'Senegal': (), 'Argelia': ('Algeria',),
        'Túnez': ('Tunisia',), 'Egipto': ('Egypt',), 'Camerún': ('Cameroon',),
        'Nigeria': (), 'Ghana': (), 'Costa de Marfil': ("Côte d'Ivoire", 'Ivory Coast'),
        'Sudáfrica': ('South Africa',), 'Mali': (), 'Cabo Verde': ('Cape Verde',),
    },
    'OFC': {'Nueva Zelanda': ('New Zealand',)},
}


def _key(value):
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize('NFKD', value).casefold()
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', text).split())


ALIASES = {name: aliases for teams in _GROUPS.values() for name, aliases in teams.items()}
UEFA_TEAMS = frozenset(_GROUPS['UEFA'])
CONMEBOL_TEAMS = frozenset(_GROUPS['CONMEBOL'])
_INDEX = {}
_CONFEDERATIONS = {}
for _confederation, _teams in _GROUPS.items():
    for _canonical, _aliases in _teams.items():
        _CONFEDERATIONS[_canonical] = _confederation
        for _alias in (_canonical, *_aliases):
            _normalized = _key(_alias)
            if _normalized in _INDEX and _INDEX[_normalized] != _canonical:
                raise ValueError('Ambiguous national-team alias catalog')
            _INDEX[_normalized] = _canonical


def national_key(name):
    """Return an explicit senior-team canonical name, or None; never strip qualifiers."""
    return _INDEX.get(_key(name))


def senior_pair(home, away):
    """Both exact identities must be known and different; no fixture is implied."""
    left, right = national_key(home), national_key(away)
    return left is not None and right is not None and left != right


def confederation(name):
    """Unknown, club and non-senior names have no inferred confederation."""
    return _CONFEDERATIONS.get(national_key(name))
