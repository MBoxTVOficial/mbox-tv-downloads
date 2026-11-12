"""Local GENERAL-ready wrapper for public agenda sources; no sports API or credentials.

This module is not selected by any published workflow. All evidence is validated
before the feed, assignments and persistent identities are committed together.
"""
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from . import generate_sports_today as generator
from . import official_football_bot as bot
from .broadcaster_discovery import atomic_json, read_json
from .broadcaster_sources import ZONE, instant
from .prioritized_agendas import acquire_all, pair_key, validate_guides
from .sports_channel_rules import load_clean_groups
from .sports_daily_broadcasts import read_broadcasts, validate_broadcasts, resolve_broadcasts, apply_daily_assignments
from .sports_playback_policy import apply_playback_policy
from .sports_fixture_identity import IdentityStore


def _events(feed):
    return [event for section in (feed or {}).get('sections', []) for event in section['events']]


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8')


def _replace_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', delete=False) as temporary:
            name = Path(temporary.name)
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def _publish(values):
    """Encode the entire validated transaction first; restore prior bytes on failure."""
    payloads = [(path, _encoded(value)) for path, value in values]
    previous = {path: path.read_bytes() if path.exists() else None for path, _ in payloads}
    changed = []
    try:
        for path, payload in payloads:
            if previous[path] != payload:
                _replace_bytes(path, payload)
                changed.append(path)
    except OSError:
        for path in reversed(changed):
            if previous[path] is None:
                path.unlink(missing_ok=True)
            else:
                _replace_bytes(path, previous[path])
        raise
    return {path for path in changed}


def _valid_empty(report):
    # A failed/deferred fetch does not prove that the date has no fixtures.
    return any(item.get('state') in {'OK', 'NO_MATCHES', 'CACHE_HIT'} and
               item.get('fixturesToday', item.get('fixtures', 0)) == 0
               for item in report.get('sources', []) + report.get('guides', []))


def _reconcile_exact_rows(rows, previous, identity, guides, report, day):
    """Persist an exact fixture association, never a partial name/broadcaster transfer."""
    store = IdentityStore(copy.deepcopy(identity))
    if not previous or previous['date'] != day.isoformat():
        return store.value
    for row in rows:
        new_key = row['source'] + ':' + row['providerFixtureId']
        if new_key in store.value['entries'] or new_key in store.value.get('reconciliations', {}):
            continue
        exact = [event for event in _events(previous) if pair_key(event, guides) == pair_key(row, guides)
                 and event['startTime'] == instant(row['startTime']).strftime('%H:%M')
                 and instant(row['startTime']).date() == day]
        if len(exact) != 1:
            continue
        identifier = int(exact[0]['id'].removeprefix('fixture-'))
        old_key = store.reverse.get(identifier)
        if old_key is None:
            continue
        old_provider, _, old_id = old_key.partition(':')
        store.reconcile_provider(old_provider, old_id, row['source'], row['providerFixtureId'], confirmed=True)
        report['exactIdentityReconciliations'] = report.get('exactIdentityReconciliations', 0) + 1
    return store.value


def generate_official_agenda(root, output, day, now, *, transport=None, offline=False):
    root, output = Path(root), Path(output)
    previous = generator.previous_feed(output)
    report = {'state': 'KEEP_LAST_GOOD', 'requestedDate': day.isoformat(),
              'feedDate': (previous or {}).get('date'), 'publicRequests': 0,
              'sportsApiRequests': 0, 'warnings': []}

    def keep(reason):
        report.update(state='KEEP_LAST_GOOD', reason=reason, changed=False)
        report['warnings'].append(reason)
        _diagnostic(root, report)
        return previous, report, False

    def cache(name):
        try:
            return read_json(root / name, optional=True)
        except (ValueError, OSError):
            report['warnings'].append('INVALID_CACHE_IGNORED')
            return None

    try:
        protected = {'sports_fixture_identity.json', 'sports_broadcasts_today.json',
                     'mbox_sports_channels_clean.json', 'sports_broadcaster_mapping.json',
                     'sports_official_sources.json', 'sports_agenda_guides.json',
                     'mbox_config.json', 'update.json'}
        if output.resolve() in {(root / name).resolve() for name in protected}:
            return keep('INVALID_OUTPUT_TARGET')
        local_now = instant(now.isoformat())
        if local_now.date() != day:
            return keep('CLOCK_DATE_MISMATCH')
        config = bot.validate_registry(read_json(root / bot.REGISTRY))
        guides = read_json(root / 'sports_agenda_guides.json')
        validate_guides(guides)
        groups = load_clean_groups(root / 'mbox_sports_channels_clean.json', generator.normalize)
        mapping = read_json(root / 'sports_broadcaster_mapping.json')
        identity = read_json(root / 'sports_fixture_identity.json', optional=True)
        IdentityStore(identity)  # Invalid identity files must never be replaced silently.
        daily = read_broadcasts(root / 'sports_broadcasts_today.json')
        rows, official_cache, guide_cache, records, acquired = acquire_all(
            config, guides, day, local_now, groups, mapping,
            cache('official_source_cache.json'), cache('argentina_guide_cache.json'),
            transport=transport, offline=offline)
        report.update(acquired)
        if type(report['publicRequests']) is not int or not 0 <= report['publicRequests'] <= 6 or report.get('sportsApiRequests') != 0:
            return keep('REQUEST_BUDGET_INVALID')
        if any(item.get('state') in {'ERROR', 'ERROR_USING_CACHE', 'DEFERRED_BUDGET', 'REVIEW',
                                    'CACHE_STALE', 'OFFLINE_MISS'}
               for item in report.get('sources', []) + report.get('guides', [])):
            report['warnings'].append('PUBLIC_SOURCES_PARTIAL')
        # Only public cache and diagnostics can change before the publishing gate.
        for name, value in (('official_source_cache.json', official_cache),
                            ('argentina_guide_cache.json', guide_cache)):
            try:
                atomic_json(root / name, value)
            except OSError:
                report['warnings'].append('CACHE_WRITE_FAILED')
        blocked = set(report.get('blockedCompetitions', [])) | {
            item['competition'] for item in report.get('sources', [])
            if item.get('state') == 'REVIEW' and 'competition' in item}
        identity = _reconcile_exact_rows(rows, previous, identity, guides, report, day)
        feed, broadcasts, details, updated_identity = bot.preview(
            rows, day, local_now, config, groups, mapping, identity=identity,
            daily=daily if daily and daily['date'] == day.isoformat() else None,
            guide_records=records, guide_config=guides, blocked_competitions=blocked)
        incoming, old = _events(feed), _events(previous)
        # A failed TV lookup cannot erase today's previously confirmed exact fixture.
        # A new conflict/quarantine is different: it must remain non-playable.
        if daily and daily['date'] == day.isoformat() and previous and previous['date'] == day.isoformat() and any(
                source.get('state') in {'ERROR', 'ERROR_USING_CACHE', 'DEFERRED_BUDGET'}
                for source in report.get('sources', []) + report.get('guides', [])):
            by_old = {event['id']: event for event in old}
            decisions = {f"fixture-{item['fixtureId']}": item for item in details}
            prior_auto = {f"fixture-{item['fixtureId']}": item for item in daily['broadcasts']
                          if item.get('source') == 'auto' and item['confidence'] == 'confirmed'}
            for event in incoming:
                prior, assignment, decision = by_old.get(event['id']), prior_auto.get(event['id']), decisions[event['id']]
                if not event.get('channels') and prior and assignment and decision['state'] == 'UNRESOLVED' and \
                        event['competition'] not in blocked and event['startTime'] == prior['startTime'] and \
                        pair_key(event, guides) == pair_key(prior, guides):
                    broadcasts['broadcasts'].append(copy.deepcopy(assignment))
                    decision.update(state='CONFIRMED_MBOX', channelGroups=assignment['channelGroups'],
                                    broadcaster=assignment.get('broadcaster', []), preservedConfirmed=True)
            apply_daily_assignments(feed, resolve_broadcasts(broadcasts, feed, groups, generator.normalize))
            apply_playback_policy(feed, local_now, ZONE, groups, log=lambda message: None)
        if not incoming:
            if previous and previous['date'] == day.isoformat() and old:
                return keep('EMPTY_AGENDA')
            if not _valid_empty(report):
                return keep('NO_VALID_SOURCE')
        if previous and previous['date'] == day.isoformat():
            identifiers = {event['id'] for event in incoming}
            if any(event.get('channels') and event['id'] not in identifiers for event in old):
                return keep('IDENTITY_RECONCILIATION_REQUIRED')
        if daily and daily['date'] == day.isoformat():
            manual_ids = {f"fixture-{entry['fixtureId']}" for entry in daily['broadcasts']
                          if entry.get('source', 'manual') == 'manual' and entry['confidence'] == 'confirmed'}
            if manual_ids - {event['id'] for event in incoming}:
                return keep('IDENTITY_RECONCILIATION_REQUIRED')
        # No fuzzy transfer of either channels or identities. Current evidence wins.
        generator.validate_feed(feed)
        validate_broadcasts(broadcasts)
        IdentityStore(updated_identity)
        changed_paths = _publish([(root / 'sports_fixture_identity.json', updated_identity),
                                  (root / 'sports_broadcasts_today.json', broadcasts), (output, feed)])
        changed = output in changed_paths
        report.update(state='DEGRADED', feedDate=feed['date'], changed=changed,
                      fixtures=details, totalFixtures=len(incoming),
                      withChannels=sum(bool(event.get('channels')) for event in incoming))
        _diagnostic(root, report)
        return feed, report, changed
    except (ValueError, KeyError, TypeError, OSError, generator.GenerationError):
        return keep('GENERATION_FAILED')  # Never log raw source errors, URLs or credentials.


def _diagnostic(root, report):
    try:
        atomic_json(root / 'official_agenda_generation_report.json', report)
    except OSError:
        report.setdefault('warnings', []).append('DIAGNOSTIC_WRITE_FAILED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=generator.ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    now = datetime.now(ZONE)
    feed, report, _ = generate_official_agenda(args.root, args.output or args.root / 'sports_today.json',
                                             now.date(), now, offline=args.offline)
    print('SPORTS_OFFICIAL ' + json.dumps({key: report.get(key) for key in
          ('state', 'reason', 'requestedDate', 'feedDate', 'changed', 'publicRequests', 'sportsApiRequests', 'warnings')}))
    return 0 if feed is not None and feed['date'] == now.date().isoformat() else 1


if __name__ == '__main__':
    raise SystemExit(main())
