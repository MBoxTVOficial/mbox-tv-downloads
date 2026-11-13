"""Three dated agenda payloads, one public GET budget, no live APIs."""
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import generate_official_agenda as agenda
from scripts import sports_agenda_window as window
from scripts import prioritized_agendas as guides
from scripts.broadcaster_sources import ZONE, Listing
from scripts.sports_channel_rules import load_clean_groups

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 12, tzinfo=ZONE)


def row(target=DAY, signal='ESPN Premium'):
    return {'competition': 'Liga Profesional Argentina', 'competitionId': 'lpf-primera',
            'providerFixtureId': 'offline-'+target.isoformat(), 'source': 'lpf_official',
            'homeTeam': 'Club Azul', 'awayTeam': 'Club Verde',
            'startTime': target.isoformat()+'T19:00:00-03:00',
            'sourceUrl': 'https://www.ligaprofesional.ar/notas/primera/2026/11/10/agenda/',
            'broadcasters': [signal] if signal else []}


class AgendaWindowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / 'sports_today.json'
        for name in ('sports_official_sources.json', 'sports_agenda_guides.json',
                     'sports_broadcaster_mapping.json', 'mbox_sports_channels_clean.json'):
            shutil.copyfile(ROOT / name, self.root / name)

    def generate(self, targets=(0, 1, 2), signal='ESPN Premium', now=NOW, acquire_report=None):
        current = now.date()
        by_day = {(current+timedelta(days=offset)).isoformat(): [row(current+timedelta(days=offset), signal)]
                  for offset in targets}
        report = {'publicRequests': 0, 'sportsApiRequests': 0,
                  'sources': [{'state': 'OK', 'fixturesToday': len(by_day.get(current.isoformat(), []))}],
                  'guides': [], 'windowRows': by_day,
                  'windowRecords': {key: [] for key in by_day},
                  'windowGuides': {key: [] for key in by_day}}
        report.update(acquire_report or {})
        with patch.object(agenda, 'acquire_all', return_value=(by_day.get(current.isoformat(), []), {}, {}, [], report)):
            result = agenda.generate_official_agenda(self.root, self.output, current, now)
        stored = self.root / 'sports_agenda_window.json'
        return result, json.loads(stored.read_text(encoding='utf8')) if stored.exists() else None

    def test_generates_three_date_keys_with_legacy_feed_unchanged_contract(self):
        (feed, report, changed), bundle = self.generate()
        self.assertTrue(changed)
        self.assertEqual(['2026-11-12', '2026-11-13', '2026-11-14'], sorted(bundle['days']))
        self.assertEqual(feed, bundle['days'][DAY.isoformat()])
        self.assertEqual('2026-11-12T12:00:00-03:00', bundle['generatedAt'])
        window.validate_window(bundle)

    def test_future_confirmed_channels_survive_without_faking_metadata_clock(self):
        (_, _, _), bundle = self.generate()
        for key, feed in bundle['days'].items():
            self.assertEqual(key, feed['date'])
            self.assertEqual('2026-11-12T12:00:00-03:00', feed['updatedAt'])
            event = agenda._events(feed)[0]
            self.assertEqual('SCHEDULED', event['status'])
            self.assertNotIn('homeScore', event)
            self.assertNotIn('awayScore', event)
            self.assertEqual([183616, 183615, 183614], [c['streamId'] for c in event['channels']])

    def test_unknown_future_tv_stays_without_channels(self):
        (_, _, _), bundle = self.generate(signal=None)
        self.assertTrue(all(not event.get('channels') for feed in bundle['days'].values() for event in agenda._events(feed)))

    def test_missing_future_date_is_omitted_not_fake_empty(self):
        (_, _, _), bundle = self.generate(targets=(0,))
        self.assertEqual([DAY.isoformat()], list(bundle['days']))

    def test_last_good_future_is_preserved_by_exact_date(self):
        (_, _, _), first = self.generate()
        (_, report, _), second = self.generate(targets=(0,), now=NOW+timedelta(minutes=1))
        self.assertEqual(first['days']['2026-11-13'], second['days']['2026-11-13'])
        self.assertEqual(first['days']['2026-11-14'], second['days']['2026-11-14'])
        self.assertEqual('KEEP_LAST_GOOD_NO_COVERAGE', report['windowDays'][0]['state'])

    def test_retained_future_removes_disabled_and_unknown_catalog_streams(self):
        self.generate()
        catalog = self.root/'mbox_sports_channels_clean.json'
        value = json.loads(catalog.read_text(encoding='utf8'))
        group = next(group for group in value['groups'] if group['id']=='espn_premium')
        group['streams'][0]['enabled'] = False
        group['streams'] = [stream for stream in group['streams'] if stream['streamId'] != 183615]
        agenda.atomic_json(catalog,value)
        (_,_,_), second = self.generate(targets=(0,),signal=None)
        for target in ('2026-11-13','2026-11-14'):
            channels = agenda._events(second['days'][target])[0]['channels']
            self.assertEqual([183614],[channel['streamId'] for channel in channels])
            self.assertEqual([1],[channel['priority'] for channel in channels])

    def test_disabled_old_channels_are_not_copied_during_partial_future_failure(self):
        self.generate()
        catalog = self.root/'mbox_sports_channels_clean.json'
        value = json.loads(catalog.read_text(encoding='utf8'))
        for group in value['groups']:
            if group['id']=='espn_premium':
                for stream in group['streams']:
                    stream['enabled'] = False
        agenda.atomic_json(catalog,value)
        # Exercise window reuse, not the separate wrapper's previous automatic
        # assignment resolver (which correctly refuses an entirely empty group).
        agenda.atomic_json(self.root/'sports_broadcasts_today.json', {'schemaVersion':1,
            'date':DAY.isoformat(),'timezone':window.TIMEZONE,'broadcasts':[]})
        (_,_,_),second = self.generate(signal=None,acquire_report={
            'sources':[{'state':'ERROR','fixturesToday':1}],
            'windowGuides':{'2026-11-13':[{'state':'ERROR'}],'2026-11-14':[{'state':'ERROR'}]}})
        self.assertTrue(all(not event.get('channels') for key,feed in second['days'].items()
                            if key != DAY.isoformat() for event in agenda._events(feed)))

    def test_explicit_official_reschedule_new_identity_replaces_old_without_tv_transfer(self):
        self.generate()
        target = '2026-11-13'
        changed = row(date.fromisoformat(target),signal=None)
        changed.update(providerFixtureId='official-rescheduled-key',startTime=target+'T20:00:00-03:00')
        (_,report,_),second = self.generate(targets=(0,),signal=None,acquire_report={
            'windowRows':{target:[changed]},'windowRecords':{target:[]}})
        event = agenda._events(second['days'][target])[0]
        self.assertEqual('20:00',event['startTime'])
        self.assertFalse(event.get('channels'))
        self.assertEqual('GENERATED',report['windowDays'][0]['state'])

    def test_rescheduled_fixture_gets_only_its_new_explicit_official_signal(self):
        self.generate()
        target = '2026-11-13'
        changed = row(date.fromisoformat(target),signal='TNT Sports')
        changed.update(providerFixtureId='official-rescheduled-key',startTime=target+'T20:00:00-03:00')
        (_,_,_),second = self.generate(targets=(0,),signal=None,acquire_report={
            'windowRows':{target:[changed]},'windowRecords':{target:[]}})
        event = agenda._events(second['days'][target])[0]
        self.assertEqual('20:00',event['startTime'])
        self.assertEqual([165664,165666,165665],[channel['streamId'] for channel in event['channels']])

    def test_all_sources_fail_preserves_four_artifacts_byte_for_byte(self):
        self.generate()
        names = ('sports_today.json', 'sports_broadcasts_today.json', 'sports_fixture_identity.json', 'sports_agenda_window.json')
        before = {name: (self.root/name).read_bytes() for name in names}
        with patch.object(agenda, 'acquire_all', side_effect=TimeoutError('raw-private-error')):
            feed, report, changed = agenda.generate_official_agenda(self.root, self.output, DAY, NOW)
        self.assertFalse(changed)
        self.assertEqual(before, {name: (self.root/name).read_bytes() for name in names})
        self.assertNotIn('raw-private-error', json.dumps(report))

    def test_new_date_drops_yesterday_and_retains_only_exact_previous_future(self):
        (_, _, _), first = self.generate()
        (_, _, _), second = self.generate(targets=(0,), now=NOW+timedelta(days=1))
        self.assertNotIn('2026-11-12', second['days'])
        self.assertEqual(first['days']['2026-11-14'], second['days']['2026-11-14'])
        self.assertNotIn('2026-11-15', second['days'])

    def test_persistent_identity_contains_all_days_and_second_run_stable(self):
        (_, _, _), first = self.generate()
        identity = json.loads((self.root/'sports_fixture_identity.json').read_text())
        self.assertEqual(3, len(identity['entries']))
        (_, _, _), second = self.generate()
        self.assertEqual(first, second)

    def test_future_manual_applies_only_exact_target_and_file_bytes_preserved(self):
        (_, _, _), first = self.generate(signal=None)
        target = '2026-11-13'
        identifier = int(agenda._events(first['days'][target])[0]['id'].removeprefix('fixture-'))
        daily = {'schemaVersion':1,'date':target,'timezone':window.TIMEZONE,
                 'broadcasts':[{'fixtureId':identifier,'channelGroups':['espn_premium'],
                                'confidence':'confirmed','source':'manual'}]}
        path = self.root/'sports_broadcasts_today.json'
        agenda.atomic_json(path,daily)
        before = path.read_bytes()
        (_,report,_), second = self.generate(signal=None)
        self.assertFalse(agenda._events(second['days']['2026-11-12'])[0].get('channels'))
        self.assertEqual([183616,183615,183614], [c['streamId'] for c in agenda._events(second['days'][target])[0]['channels']])
        self.assertFalse(agenda._events(second['days']['2026-11-14'])[0].get('channels'))
        self.assertEqual(before,path.read_bytes())
        self.assertIn('FUTURE_MANUAL_CONFIGURATION_PRESERVED',report['warnings'])

    def test_current_manual_never_leaks_to_future_dates(self):
        (_, _, _), first = self.generate(signal=None)
        identifier = int(agenda._events(first['days'][DAY.isoformat()])[0]['id'].removeprefix('fixture-'))
        agenda.atomic_json(self.root/'sports_broadcasts_today.json', {'schemaVersion':1,'date':DAY.isoformat(),
            'timezone':window.TIMEZONE,'broadcasts':[{'fixtureId':identifier,'channelGroups':['espn_premium'],
            'confidence':'confirmed','source':'manual'}]})
        (_,_,_),second = self.generate(signal=None)
        self.assertTrue(agenda._events(second['days'][DAY.isoformat()])[0].get('channels'))
        self.assertTrue(all(not e.get('channels') for k,f in second['days'].items() if k!=DAY.isoformat() for e in agenda._events(f)))

    def test_future_review_removes_old_confirmed_signal(self):
        self.generate()
        (_, _, _), second = self.generate(acquire_report={'blockedCompetitions': ['Liga Profesional Argentina']})
        self.assertTrue(all(not event.get('channels') for feed in second['days'].values() for event in agenda._events(feed)))

    def test_future_quarantine_also_removes_tv_when_future_rows_missing(self):
        self.generate()
        (_, _, _), second = self.generate(targets=(0,), acquire_report={'blockedCompetitions':['Liga Profesional Argentina']})
        self.assertTrue(all(not event.get('channels') for feed in second['days'].values() for event in agenda._events(feed)))

    def test_withdrawn_future_tv_has_real_new_timestamp_for_client_merge(self):
        (_,_,_),first = self.generate()
        next_now = NOW+timedelta(minutes=1)
        (_,_,_),second = self.generate(targets=(0,),now=next_now,
            acquire_report={'blockedCompetitions':['Liga Profesional Argentina']})
        for target in ('2026-11-13','2026-11-14'):
            self.assertFalse(agenda._events(second['days'][target])[0].get('channels'))
            self.assertEqual(next_now.isoformat(timespec='seconds'),second['days'][target]['updatedAt'])
            self.assertNotEqual(first['days'][target]['updatedAt'],second['days'][target]['updatedAt'])
            self.assertTrue(second['days'][target]['updatedAt'].startswith(DAY.isoformat()))

    def test_future_utc_date_is_not_mistaken_for_art_date(self):
        future = row(DAY+timedelta(days=1))
        future['startTime'] = '2026-11-13T01:00:00Z'  # Still Nov12 ART.
        report = {'publicRequests': 0, 'sportsApiRequests': 0, 'sources': [{'state':'OK','fixturesToday':1}],
                  'windowRows': {'2026-11-13':[future]}, 'windowRecords':{}, 'guides':[]}
        with patch.object(agenda, 'acquire_all', return_value=([row()], {}, {}, [], report)):
            agenda.generate_official_agenda(self.root, self.output, DAY, NOW)
        bundle = json.loads((self.root/'sports_agenda_window.json').read_text(encoding='utf8'))
        self.assertNotIn('2026-11-13', bundle['days'])

    def test_invalid_nested_date_timezone_or_future_updatedat_is_rejected(self):
        (_, _, _), bundle = self.generate()
        for change in ({'date':'2026-11-14'}, {'timezone':'UTC'}, {'updatedAt':'2026-11-13T00:00:00-03:00'}):
            bad = copy.deepcopy(bundle)
            bad['days']['2026-11-13'].update(change)
            with self.assertRaises(ValueError):
                window.validate_window(bad)

    def test_empty_and_oversized_envelopes_are_rejected(self):
        (_, _, _), bundle = self.generate()
        empty = {**bundle, 'days':{}}
        with self.assertRaises(ValueError):
            window.validate_window(empty)
        with patch.object(window, 'MAX_WINDOW_BYTES', 10), self.assertRaises(ValueError):
            window.validate_window(bundle)

    def test_bundle_write_failure_rolls_back_all_four_artifacts(self):
        self.generate()
        names = ('sports_today.json', 'sports_broadcasts_today.json', 'sports_fixture_identity.json', 'sports_agenda_window.json')
        before = {name:(self.root/name).read_bytes() for name in names}
        original = agenda._replace_bytes
        def fail_bundle(path, payload):
            if path.name == 'sports_agenda_window.json':
                raise OSError('storage failure')
            return original(path, payload)
        with patch.object(agenda, '_replace_bytes', side_effect=fail_bundle):
            (_, report, changed), _ = self.generate(now=NOW+timedelta(minutes=1))
        self.assertFalse(changed)
        self.assertEqual('GENERATION_FAILED', report['reason'])
        self.assertEqual(before, {name:(self.root/name).read_bytes() for name in names})


class SharedAcquisitionWindowTests(unittest.TestCase):
    def setUp(self):
        self.official = json.loads((ROOT/'sports_official_sources.json').read_text(encoding='utf8'))
        self.config = json.loads((ROOT/'sports_agenda_guides.json').read_text(encoding='utf8'))
        self.mapping = json.loads((ROOT/'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        self.groups = load_clean_groups(ROOT/'mbox_sports_channels_clean.json', agenda.generator.normalize)

    def test_three_days_reuse_exact_same_guide_gets_and_single_six_budget(self):
        network = Mock()
        network.fetch.return_value = 'page'
        def listings(adapter, html, target):
            return [Listing('Club Azul','Club Verde',datetime.combine(target, datetime.min.time(),ZONE).replace(hour=19),
                            'Liga Profesional Argentina',('TNT Sports',))]
        def official_source(source, target, pages):
            pages.fetch(source['url'])
            return []
        with patch.object(guides, 'parse_page', side_effect=listings), \
                patch.object(agenda.bot, 'source_rows', side_effect=official_source):
            result = guides.acquire_all(self.official,self.config,DAY,NOW,self.groups,self.mapping,
                                         transport=network,window_days=3)
        urls = [call.args[0] for call in network.fetch.call_args_list]
        self.assertLessEqual(len(urls), 6)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(3,len(result[-1]['windowRows']))
        self.assertEqual(0,result[-1]['sportsApiRequests'])

    def test_future_day_cache_is_separate_and_never_relabeled(self):
        network = Mock()
        network.fetch.return_value = 'page'
        def listings(adapter, html, target):
            return [Listing('Club Azul','Club Verde',datetime.combine(target,datetime.min.time(),ZONE).replace(hour=19),
                            'Liga Profesional Argentina',('LPF Play',))]
        with patch.object(guides,'parse_page',side_effect=listings), patch.object(agenda.bot,'source_rows',return_value=[]):
            first = guides.acquire_all(self.official,self.config,DAY,NOW,self.groups,self.mapping,
                                        transport=network,window_days=3)
            second = guides.acquire_all(self.official,self.config,DAY,NOW,self.groups,self.mapping,
                                         official_cache=first[1],guide_cache=first[2],transport=network,window_days=3,offline=True)
        self.assertEqual(0,second[-1]['publicRequests'])
        for key, records in second[-1]['windowRecords'].items():
            self.assertTrue(all(record['startTime'].startswith(key) for record in records))


if __name__ == '__main__':
    unittest.main()
