"""Source-supplied public badges only; cosmetic metadata never confirms TV."""
import re
from urllib.parse import unquote, urlsplit


def accepted_image_url(provider, url):
    """Return the unchanged raster URL from an allowlisted source, or empty.

    Never construct badge URLs, upgrade a guessed resolution, access the network
    or accept credentials. TyC's existing v query is public image versioning.
    """
    if not isinstance(url, str) or not url or len(url) > 1024 or any(ord(c) <= 32 for c in url):
        return ''
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment:
            return ''
        path = unquote(parts.path)
        if '\\' in path or any(part in ('.', '..') for part in path.split('/')) or any(ord(c) <= 32 for c in path):
            return ''
        if any(label in path.casefold() for label in ('placeholder', 'escudo-err', 'escudo_err',
                'sin-escudo', 'sin_escudo', 'sin-imagen', 'sin_imagen', 'default', 'no-image', 'no_image', 'blank')):
            return ''
        if provider == 'futbol-en-vivo-ar':
            valid = (parts.hostname == 'static.futbolenlatv.com' and not parts.query and
                     re.fullmatch(r'/img/32/[^/]+\.(?:png|webp|jpe?g)', path, re.I))
        elif provider == 'tyc-agenda':
            valid = (parts.hostname == 'statics-files.tycsports.com' and
                     re.fullmatch(r'/escudos/\d+\.(?:png|webp|jpe?g)', path, re.I) and
                     (not parts.query or re.fullmatch(r'v=\d+(?:[?&]v=\d+)*', parts.query)))
        elif provider == 'ole-agenda':
            valid = (parts.hostname in ('ole.com.ar', 'www.ole.com.ar') and not parts.query and
                     re.fullmatch(r'/collections/escudos/\d+\.(?:png|webp|jpe?g)', path, re.I))
        else:
            return ''
        return url if valid else ''
    except (TypeError, ValueError):
        return ''


def fixture_logos(row, guide_records, config):
    """Use a badge only from the same ordered fixture, competition and ART kickoff.

    Existing explicit team aliases are shared with fixture reconciliation. A
    broadcaster confirmation is unnecessary for cosmetic, source-supplied art.
    """
    from .prioritized_agendas import exact_key, PROVIDERS
    result = {'homeLogo': '', 'awayLogo': ''}
    if not config or not isinstance(guide_records, list):
        return result
    try:
        target = exact_key(row, config)
    except (KeyError, TypeError, ValueError):
        return result
    for provider in PROVIDERS:
        for record in guide_records:
            try:
                if record.get('guideId') != provider['id'] or exact_key(record, config) != target:
                    continue
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            for field in result:
                if not result[field]:
                    result[field] = accepted_image_url(provider['id'], record.get(field))
    return result
