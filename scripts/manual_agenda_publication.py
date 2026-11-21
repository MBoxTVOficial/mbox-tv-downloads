"""Offline manual publication, separate from every public fixture acquisition."""
import argparse
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path

from .broadcaster_sources import ZONE, instant
from .sports_agenda_window import validate_window
from .sports_fixture_identity import IdentityStore
from .sports_manual_agenda import apply_manual_agenda, validate_manual_catalog, validate_manual_config

MANUAL = 'sports_manual_agenda.json'
CATALOG = 'sports_manual_channel_catalog.json'
BASE = 'sports_agenda_automatic.json'


def empty_config():
    return {'schemaVersion': 1, 'timezone': 'America/Argentina/Buenos_Aires',
            'events': [], 'channelOverrides': []}


def read_json(path, optional=False):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError('MANUAL_INPUT_MISSING') from None
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('MANUAL_INPUT_TOO_LARGE')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('MANUAL_DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise ValueError('MANUAL_JSON_INVALID') from None


def read_base(root):
    value = read_json(Path(root) / BASE, optional=True)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'schemaVersion', 'automaticWindow', 'lastGoodManualConfig'} \
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 \
            or not isinstance(value['lastGoodManualConfig'], dict):
        raise ValueError('MANUAL_BASE_INVALID')
    validate_window(value['automaticWindow'])
    return value


def make_base(window, config):
    validate_window(window)
    return {'schemaVersion': 1, 'automaticWindow': copy.deepcopy(window),
            'lastGoodManualConfig': copy.deepcopy(config)}


def select_inputs(root, base=None, *, strict=False):
    """GENERAL can retain the last valid manual config while sources continue."""
    warnings = []
    try:
        raw_catalog = read_json(Path(root) / CATALOG, optional=True)
        catalog = validate_manual_catalog(raw_catalog or {'schemaVersion': 1, 'channels': []})
    except (ValueError, KeyError, TypeError, OSError):
        if strict:
            raise ValueError('MANUAL_CATALOG_INVALID') from None
        catalog = validate_manual_catalog({'schemaVersion': 1, 'channels': []})
        warnings.append('MANUAL_CATALOG_INVALID')
    try:
        raw = read_json(Path(root) / MANUAL, optional=True)
        selected = validate_manual_config(empty_config() if raw is None else raw, catalog)
    except (ValueError, KeyError, TypeError, OSError):
        if strict:
            raise ValueError('MANUAL_CONFIG_INVALID') from None
        warnings.append('MANUAL_CONFIG_INVALID')
        try:
            selected = validate_manual_config(base['lastGoodManualConfig'], catalog)
            warnings.append('MANUAL_USING_LAST_GOOD_CONFIG')
        except (ValueError, KeyError, TypeError):
            selected = validate_manual_config(empty_config(), catalog)
            warnings.append('MANUAL_CONFIG_IGNORED')
    return selected, catalog, warnings


def stamp_public_window(window, previous, now):
    """Removing an override is a new update, never a rollback to an old timestamp."""
    result = copy.deepcopy(window)
    timestamp = instant(now.isoformat()).isoformat(timespec='seconds')
    for day, feed in result['days'].items():
        prior = (previous or {}).get('days', {}).get(day)
        if prior is None:
            continue
        content = lambda item: {key: value for key, value in item.items() if key != 'updatedAt'}
        if content(feed) != content(prior):
            feed['updatedAt'] = timestamp
        elif instant(prior['updatedAt']) > instant(feed['updatedAt']):
            feed['updatedAt'] = prior['updatedAt']
    if any(instant(feed['updatedAt']) > instant(result['generatedAt']) for feed in result['days'].values()):
        result['generatedAt'] = timestamp
    validate_window(result)
    return result


def overlay_for_generation(root, window, identity, now, *, base=None, previous=None):
    selected, catalog, warnings = select_inputs(root, base)
    try:
        public, updated, report = apply_manual_agenda(window, selected, catalog, identity, now)
    except (ValueError, KeyError, TypeError):
        warnings.append('MANUAL_APPLICATION_INVALID')
        selected = empty_config()
        if base is not None:
            try:
                selected = validate_manual_config(base['lastGoodManualConfig'], catalog)
            except (ValueError, KeyError, TypeError):
                pass
        try:
            public, updated, report = apply_manual_agenda(window, selected, catalog, identity, now)
        except (ValueError, KeyError, TypeError):
            selected, public, updated = empty_config(), copy.deepcopy(window), copy.deepcopy(identity)
            report = {'state': 'IGNORED', 'warnings': ['MANUAL_APPLICATION_INVALID']}
    report.setdefault('warnings', []).extend(warnings)
    return stamp_public_window(public, previous, now), updated, make_base(window, selected), report


def _current_window(window, now):
    current = instant(now.isoformat()).date()
    dates = {(current + timedelta(days=offset)).isoformat() for offset in range(3)}
    if current.isoformat() not in window['days']:
        raise ValueError('MANUAL_NO_CURRENT_AUTOMATIC_AGENDA')
    result = copy.deepcopy(window)
    if instant(result['generatedAt']).date() != current:
        result['generatedAt'] = instant(now.isoformat()).isoformat(timespec='seconds')
        result['days'] = {day: feed for day, feed in result['days'].items() if day in dates}
    validate_window(result)
    return result


def publish_manual(root, now=None):
    """Apply user edits immediately using persisted evidence; zero network calls."""
    from .generate_official_agenda import _publish
    root = Path(root)
    now = now or datetime.now(ZONE)
    base = read_base(root)
    if base is None:
        raise ValueError('MANUAL_AUTOMATIC_BASE_REQUIRED')
    window = _current_window(base['automaticWindow'], now)
    selected, catalog, _ = select_inputs(root, base, strict=True)
    identity = read_json(root / 'sports_fixture_identity.json', optional=True)
    IdentityStore(identity)
    previous = read_json(root / 'sports_agenda_window.json', optional=True)
    if previous is not None:
        validate_window(previous)
    public, updated, report = apply_manual_agenda(window, selected, catalog, identity, now)
    public = stamp_public_window(public, previous, now)
    today = instant(now.isoformat()).date().isoformat()
    values = [(root / BASE, make_base(base['automaticWindow'], selected)),
              (root / 'sports_fixture_identity.json', updated),
              (root / 'sports_agenda_window.json', public),
              (root / 'sports_today.json', public['days'][today])]
    changed = _publish(values)
    return {'state': 'PUBLISHED' if changed else 'UNCHANGED', 'date': today,
            'changedFiles': sorted(path.name for path in changed),
            'publicRequests': 0, 'sportsApiRequests': 0, 'iptvRequests': 0, 'manual': report}


def main(argv=None):
    from .generate_sports_today import ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        report = publish_manual(args.root)
    except (ValueError, KeyError, TypeError, OSError):
        print('::error title=Manual football agenda::MANUAL_EDIT_NOT_PUBLISHED; previous agenda retained')
        return 1
    print('SPORTS_MANUAL ' + json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
