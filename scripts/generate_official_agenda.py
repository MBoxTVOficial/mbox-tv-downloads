"""Local GENERAL-ready wrapper for public agenda sources; no sports API or credentials.

This module is not selected by any published workflow. All evidence is validated
before the feed, assignments and persistent identities are committed together.
"""
import argparse
import copy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile

from . import generate_sports_today as generator
from . import official_football_bot as bot
from .broadcaster_discovery import atomic_json, map_signal, read_json
from .broadcaster_sources import ZONE, instant, norm
from .prioritized_agendas import acquire_all, guide_conflict, pair_key, signal_conflict, validate_guides
from .sports_channel_rules import load_clean_groups
from .sports_competition_broadcasters import territory
from .sports_daily_broadcasts import read_broadcasts, validate_broadcasts, resolve_broadcasts, apply_daily_assignments
from .sports_fixture_identity import IdentityStore
from .sports_agenda_window import build_agenda_window, validate_window


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


def _retain_confirmed_tv(feed, broadcasts, details, baseline, daily, records, guides, groups, mapping, blocked):
    """An omitted TV claim does not retract this date's exact prior confirmation.

    Current positive contradictions still win. Historical assignments remain
    identifiable in diagnostics instead of being presented as fresh evidence.
    """
    if not daily or daily['date'] != feed['date'] or not baseline or baseline['date'] != feed['date']:
        return False
    previous = {event['id']: event for event in _events(baseline)}
    prior_auto = {f"fixture-{item['fixtureId']}": item for item in daily['broadcasts']
                  if item.get('source') == 'auto' and item['confidence'] == 'confirmed'}
    current = {f"fixture-{item['fixtureId']}": item for item in broadcasts['broadcasts']}
    decisions = {f"fixture-{item['fixtureId']}": item for item in details}
    changed = False
    for event in _events(feed):
        prior, assignment = previous.get(event['id']), prior_auto.get(event['id'])
        present, decision = current.get(event['id']), decisions[event['id']]
        if not prior or not assignment or not prior.get('channels') or \
                (present and present.get('source', 'manual') == 'manual'):
            continue
        missing = [group for group in assignment['channelGroups']
                   if group not in (present or {}).get('channelGroups', [])]
        if not missing:
            continue
        reason = None
        if event['startTime'] != prior['startTime'] or pair_key(event, guides) != pair_key(prior, guides):
            reason = 'FIXTURE_IDENTITY_CHANGED'
        elif event['competition'] in blocked:
            reason = 'OFFICIAL_SOURCE_REVIEW'
        elif guide_conflict(decision, records, guides) or signal_conflict(decision, records, guides, mapping):
            reason = 'CURRENT_SOURCE_CONFLICT'
        evidence = decision.get('evidence', [])
        if reason is None and any(map_signal(item['broadcaster'], item['territory'], mapping)[3] for item in evidence):
            reason = 'AMBIGUOUS_CURRENT_SIGNAL'
        known_names = {norm(signal['name']) for signal in mapping['signals']}
        previous_names = {norm(name) for name in assignment.get('broadcaster', [])}
        if reason is None and any(norm(map_signal(item['broadcaster'], item['territory'], mapping)[0])
                                  not in known_names | previous_names for item in evidence):
            reason = 'UNRECOGNIZED_CURRENT_SIGNAL'
        if reason is None and any(item['official'] and item['source_type'] == 'official_league' for item in evidence):
            reason = 'CURRENT_OFFICIAL_TV_REPLACES_PRIOR'
        # Each current page's explicit TV list can contradict a prior assignment;
        # an external platform or an empty list alone cannot do so.
        claims = {}
        for item in evidence:
            group = map_signal(item['broadcaster'], item['territory'], mapping)[2]
            if group is not None:
                claims.setdefault((item['owner'], item['source_name']), set()).add(group)
        if reason is None and any(not claim.intersection(assignment['channelGroups']) for claim in claims.values()):
            reason = 'CURRENT_TV_CONTRADICTS_PRIOR'
        prior_ids = {channel['streamId'] for channel in prior['channels'] if channel.get('enabled', True)}
        retained = [identifier for identifier in missing if identifier in groups and
                    groups[identifier].get('signalRegion') and
                    any(stream['enabled'] for stream in groups[identifier]['streams']) and
                    any(stream['streamId'] in prior_ids for stream in groups[identifier]['streams']) and
                    any(signal['channelGroup'] == identifier and
                        territory(signal['territory']) == territory(groups[identifier]['signalRegion'])
                        for signal in mapping['signals'])]
        if reason is not None or not retained:
            decision['preservationBlockedReason'] = reason or 'PRIOR_GROUP_UNAVAILABLE'
            continue
        restored = copy.deepcopy(present or assignment)
        restored['channelGroups'] = list(dict.fromkeys((present or {}).get('channelGroups', []) + retained))
        historical_names = [name for name in assignment.get('broadcaster', [])
                            if map_signal(name, 'Argentina', mapping)[2] in retained]
        restored['broadcaster'] = list(dict.fromkeys((present or {}).get('broadcaster', []) + historical_names))
        restored['evidenceCount'] = max((present or {}).get('evidenceCount', 0), assignment.get('evidenceCount', 0))
        if present:
            broadcasts['broadcasts'].remove(present)
        broadcasts['broadcasts'].append(restored)
        decision.update(currentEvidenceState=decision['state'], state='CONFIRMED_MBOX',
                        channelGroups=restored['channelGroups'], broadcaster=restored['broadcaster'],
                        evidenceCount=restored['evidenceCount'], preservedConfirmed=True,
                        preservationReason='INCOMPLETE_CURRENT_TV_EVIDENCE',
                        preservedChannelGroups=retained, preservedAssignment=copy.deepcopy(assignment))
        changed = True
    return changed


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
                     'sports_agenda_window.json',
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
        previous_window = read_json(root / 'sports_agenda_window.json', optional=True)
        if previous_window is not None:
            validate_window(previous_window)
        baseline = previous if previous and previous['date'] == day.isoformat() else \
            (previous_window or {}).get('days', {}).get(day.isoformat())
        daily = read_broadcasts(root / 'sports_broadcasts_today.json')
        rows, official_cache, guide_cache, records, acquired = acquire_all(
            config, guides, day, local_now, groups, mapping,
            cache('official_source_cache.json'), cache('argentina_guide_cache.json'),
            transport=transport, offline=offline, window_days=3)
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
        identity = _reconcile_exact_rows(rows, baseline, identity, guides, report, day)
        feed, broadcasts, details, updated_identity = bot.preview(
            rows, day, local_now, config, groups, mapping, identity=identity,
            daily=daily if daily and daily['date'] == day.isoformat() else None,
            guide_records=records, guide_config=guides, blocked_competitions=blocked)
        incoming, old = _events(feed), _events(baseline)
        if _retain_confirmed_tv(feed, broadcasts, details, baseline, daily, records,
                                guides, groups, mapping, blocked):
            apply_daily_assignments(feed, resolve_broadcasts(broadcasts, feed, groups, generator.normalize))
        if not incoming:
            if baseline and old:
                return keep('EMPTY_AGENDA')
            if not _valid_empty(report):
                return keep('NO_VALID_SOURCE')
        if baseline:
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
        window, updated_identity, window_report = build_agenda_window(day, local_now, feed,
            config, guides, groups, mapping, updated_identity, report, previous_window,
            reconcile=_reconcile_exact_rows, daily=daily)
        values = [(root / 'sports_fixture_identity.json', updated_identity)]
        if daily and day.isoformat() < daily['date'] <= (day + timedelta(days=2)).isoformat() and \
                any(item.get('source', 'manual') == 'manual' for item in daily['broadcasts']):
            report['warnings'].append('FUTURE_MANUAL_CONFIGURATION_PRESERVED')
        else:
            values.append((root / 'sports_broadcasts_today.json', broadcasts))
        values.extend([(root / 'sports_agenda_window.json', window), (output, feed)])
        changed_paths = _publish(values)
        changed = output in changed_paths
        report.update(state='DEGRADED', feedDate=feed['date'], changed=changed,
                      fixtures=details, totalFixtures=len(incoming),
                      windowDays=window_report, availableDates=sorted(window['days']),
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
