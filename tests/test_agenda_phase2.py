import copy
from datetime import date, datetime
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
from scripts import generate_agenda_today as agenda
from scripts import generate_sports_today as generator
from scripts.broadcaster_sources import ZONE
from scripts.football_data_provider import ProviderError
from scripts.sports_fixture_identity import IdentityStore

ROOT = Path(__file__).parents[1]
DAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 12, tzinfo=ZONE)


def sample(**changes):
    row = {'competition': 'LaLiga', 'competitionId': 'PD', 'providerFixtureId': 'football-data:sample',
           'homeTeam': 'Club Azul', 'awayTeam': 'Club Verde', 'startTime': '2026-10-12T16:00:00-03:00'}
    row.update(changes)
    return row


class AgendaPhase2Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'tests/data').mkdir(parents=True)
        for path in ('sports_agenda_providers.json', 'tests/data/football_data_validation.json', 'tests/data/lpf_agenda_validation.json'):
            shutil.copy(ROOT / path, self.root / path)
        self.output = self.root / 'sports_today.json'
        self.context = patch.object(generator, 'daily_context', return_value=(None, {}))
        self.context.start()
        self.addCleanup(self.context.stop)

    def generate(self, rows=None, **kwargs):
        return agenda.generate_agenda(self.root, self.output, DAY, NOW, provider_rows=[sample()] if rows is None else rows, **kwargs)

    def test_partial_agenda_no_scores(self):
        feed, report, changed = self.generate()
        self.assertTrue(changed)
        self.assertEqual('DEGRADED', feed['sportsDataMode'])
        event = next(e for s in feed['sections'] for e in s['events'])
        self.assertNotIn('homeScore', event)
        self.assertNotIn('channels', event)
        self.assertEqual('SCHEDULED', event['status'])
        self.assertEqual('football_data', report['fixtures'][0]['provider'])

    def test_identity_stable_across_kickoff_change(self):
        _, first, _ = self.generate()
        _, second, _ = self.generate([sample(startTime='2026-10-12T17:00:00-03:00')])
        self.assertEqual(first['fixtures'][0]['fixtureId'], second['fixtures'][0]['fixtureId'])
        self.assertTrue((self.root / 'sports_fixture_identity.json').exists())

    def test_empty_never_overwrites_last_good(self):
        self.generate()
        before = self.output.read_bytes()
        _, report, changed = self.generate([])
        self.assertFalse(changed)
        self.assertEqual('KEEP_LAST_GOOD', report['state'])
        self.assertEqual(before, self.output.read_bytes())

    def test_provider_failure_keeps_last_good(self):
        self.generate()
        before = self.output.read_bytes()
        with patch.object(agenda, 'acquire', side_effect=ProviderError('HTTP_403')):
            _, report, changed = agenda.generate_agenda(self.root, self.output, DAY, NOW)
        self.assertFalse(changed)
        self.assertEqual('KEEP_LAST_GOOD', report['state'])
        self.assertEqual(before, self.output.read_bytes())

    def test_confirmed_channels_preserved_exact_identity(self):
        feed, _, _ = self.generate()
        event = next(e for s in feed['sections'] for e in s['events'])
        event['channels'] = [{'name': 'Mock TV', 'streamId': 11, 'aliases': [], 'priority': 1, 'enabled': True}]
        generator.write_feed_atomic(feed, self.output)
        updated, _, _ = self.generate()
        self.assertEqual(event['channels'], next(e for s in updated['sections'] for e in s['events'])['channels'])

    def test_no_transfer_when_provider_changes(self):
        store = IdentityStore()
        self.assertNotEqual(store.resolve('provider-a', '123'), store.resolve('provider-b', '123'))

    def test_unmapped_confirmed_legacy_fixture_keeps_whole_feed(self):
        feed, _, _ = self.generate()
        event = next(e for s in feed['sections'] for e in s['events'])
        event['id'] = 'fixture-901'
        event['channels'] = [{'name': 'Mock TV', 'streamId': 11, 'aliases': [], 'priority': 1, 'enabled': True}]
        generator.write_feed_atomic(feed, self.output)
        before = self.output.read_bytes()
        _, report, changed = self.generate()
        self.assertFalse(changed)
        self.assertEqual('CONFIRMED_IDENTITY_RECONCILIATION_REQUIRED', report['reason'])
        self.assertEqual(before, self.output.read_bytes())

    def test_collision_fails_closed(self):
        with self.assertRaises(ValueError):
            IdentityStore({'schemaVersion': 1, 'entries': {'a': 1000000000, 'b': 1000000000}})

    def test_explicit_provider_reconciliation_preserves_identity(self):
        store = IdentityStore()
        original = store.resolve('first-provider', 'first-id')
        with self.assertRaises(ValueError):
            store.reconcile_provider('first-provider', 'first-id', 'second-provider', 'second-id')
        store.reconcile_provider('first-provider', 'first-id', 'second-provider', 'second-id', confirmed=True)
        restored = IdentityStore(copy.deepcopy(store.value))
        self.assertEqual(original, restored.resolve('second-provider', 'second-id'))

    def test_kickoff_change_needs_explicit_reconciliation(self):
        store = IdentityStore()
        identifier = store.resolve('football_data', 'sample')
        before, after = '2026-10-12T16:00:00-03:00', '2026-10-12T17:00:00-03:00'
        self.assertFalse(store.kickoff_reconciled(identifier, before, after))
        store.reconcile_kickoff(identifier, before, after, confirmed=True)
        self.assertTrue(IdentityStore(copy.deepcopy(store.value)).kickoff_reconciled(identifier, before, after))

    def test_daily_assignment_not_reused_after_unconfirmed_kickoff_change(self):
        feed, report, _ = self.generate()
        identifier = report['fixtures'][0]['fixtureId']
        groups = {'mock': {'id': 'mock', 'canonicalName': 'Mock TV', 'aliases': [],
                          'streams': [{'name': 'Mock TV', 'streamId': 11, 'priority': 1, 'enabled': True}]}}
        daily = {'schemaVersion': 1, 'date': str(DAY), 'timezone': 'America/Argentina/Buenos_Aires',
                 'broadcasts': [{'fixtureId': identifier, 'channelGroups': ['mock'], 'confidence': 'confirmed'}]}
        with patch.object(generator, 'daily_context', return_value=(daily, groups)):
            moved, _, _ = self.generate([sample(startTime='2026-10-12T17:00:00-03:00')])
        self.assertNotIn('channels', next(e for s in moved['sections'] for e in s['events']))

    def test_lpf_gate_missing_fails(self):
        path = self.root / 'tests/data/lpf_agenda_validation.json'
        value = json.loads(path.read_text())
        value['exactMatches'] = 0
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            agenda.validated_config(self.root)

    def test_champions_disabled(self):
        feed, _, changed = self.generate([sample(competition='UEFA Champions League', competitionId='CL')])
        self.assertIsNone(feed)
        self.assertFalse(changed)

    def test_four_refresh_windows(self):
        config = agenda.validated_config(self.root)
        self.assertEqual(['00:05', '12:00', '17:00', '21:00'], config['refreshTimesART'])
        self.assertEqual(40, agenda.MAX_FOOTBALL_SOCCER_API_CALLS)
        self.assertEqual(90, generator.MAX_DAILY_API_CALLS)

    def test_live_disabled_success_without_api(self):
        with patch.object(generator, 'ROOT', self.root), patch.object(generator, 'generate', side_effect=AssertionError('API queried')):
            self.assertEqual(0, generator.main(['--live']))

    def test_duplicate_rows_one_fixture(self):
        feed, _, _ = self.generate([sample(), sample()])
        self.assertEqual(1, sum(len(s['events']) for s in feed['sections']))

    def test_request_budget_failure_counts(self):
        agenda.reserve(self.root, 'football_data', '2026-10-12', 1)
        with self.assertRaises(ProviderError):
            agenda.reserve(self.root, 'football_data', '2026-10-12', 1)

    def test_competition_sections_preserved(self):
        rows = [sample(competition=name, competitionId=code, providerFixtureId='fd:'+code)
                for name, code in [('LaLiga', 'PD'), ('Premier League', 'PL'), ('Serie A', 'SA'), ('Ligue 1', 'FL1')]]
        feed, _, _ = self.generate(rows)
        counts = {s['id']: len(s['events']) for s in feed['sections']}
        self.assertEqual([1, 1, 1, 1], [counts[s] for s in ('spain', 'england', 'italy', 'france')])
