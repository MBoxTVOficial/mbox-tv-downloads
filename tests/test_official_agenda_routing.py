"""Official LPF integration and truthful current-day empty agendas; no network."""
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import generate_agenda_today as agenda
from scripts import generate_sports_today as generator
from scripts import competition_source_router as router
from scripts import broadcaster_discovery as discovery
from scripts.football_data_provider import ProviderError
from scripts.broadcaster_sources import ZONE

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 12, tzinfo=ZONE)


def official():
    return {'competition': 'Liga Profesional Argentina', 'competitionId': 'lpf-primera',
            'source': 'lpf_official', 'providerFixtureId': 'lpf:offline-source-row',
            'sourceUrl': 'https://www.ligaprofesional.ar/notas/primera/2026/11/10/agenda/',
            'homeTeam': 'Club Azul', 'awayTeam': 'Club Verde', 'startTime': '2026-11-11T17:00:00-03:00'}


class OfficialAgendaRoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'tests/data').mkdir(parents=True)
        for path in ('sports_agenda_providers.json', 'tests/data/lpf_agenda_validation.json',
                     'tests/data/football_data_validation.json'):
            shutil.copyfile(ROOT / path, self.root / path)
        self.output = self.root / 'sports_today.json'
        context = patch.object(generator, 'daily_context', return_value=(None, {}))
        context.start(); self.addCleanup(context.stop)

    def clients(self, lpf_rows=()):
        return {'lpf_official': Mock(matches=Mock(return_value=list(lpf_rows))),
                'football_data': Mock(matches=Mock(return_value=[])),
                'football_soccer_argentina': Mock()}

    def generate(self, clients):
        return agenda.generate_agenda(self.root, self.output, DAY, NOW, clients=clients)

    def old_feed(self):
        day = DAY - timedelta(days=1)
        feed, _ = generator.build_feed({'errors': [], 'response': [], 'results': 0}, day, NOW - timedelta(days=1))
        generator.write_feed_atomic(feed, self.output)
        return self.output.read_bytes()

    def test_production_routes_lpf_to_official_and_disables_paid_auxiliary(self):
        config = agenda.validated_config(self.root)
        self.assertEqual('lpf_official', config['competitions']['Liga Profesional Argentina']['provider'])
        self.assertFalse(config['providers']['football_soccer_argentina']['enabled'])
        clients = self.clients([official()])
        with patch.object(agenda, 'build_opener', side_effect=AssertionError('Paid API forbidden')):
            feed, report, _ = self.generate(clients)
        clients['football_soccer_argentina'].matches.assert_not_called()
        self.assertEqual('official', report['fixtures'][0]['source'])
        self.assertEqual('lpf_official', report['fixtures'][0]['provider'])
        event = next(e for s in feed['sections'] for e in s['events'])
        self.assertEqual('17:00', event['startTime'])
        self.assertEqual('SCHEDULED', event['status'])
        self.assertNotIn('channels', event)
        self.assertNotIn('homeScore', event)

    def test_official_teams_and_kickoff_override_auxiliary(self):
        authoritative = official()
        auxiliary = {**authoritative, 'competitionId': 'lg_22EEYKR', 'source': 'football_soccer_argentina',
                     'providerFixtureId': 'aux:offline', 'startTime': '2026-11-11T18:00:00-03:00'}
        rows, _ = router.route(DAY, [auxiliary], [authoritative])
        self.assertEqual('2026-11-11T17:00:00-03:00', rows[0]['startTime'])

    def test_official_timestamp_filters_by_art_day(self):
        rows, _ = router.route(DAY, [{**official(), 'startTime': '2026-11-11T00:30:00Z'}])
        self.assertEqual([], rows)

    def test_official_lpf_wins_regardless_of_input_order(self):
        auxiliary = {**official(), 'competitionId': 'lg_22EEYKR', 'source': 'football_soccer_argentina',
                     'providerFixtureId': 'aux:offline', 'startTime': '2026-11-11T18:00:00-03:00'}
        rows, conflicts = router.route(DAY, [auxiliary, official()])
        self.assertEqual(1, len(rows))
        self.assertEqual('official', rows[0]['source'])
        self.assertEqual('2026-11-11T17:00:00-03:00', rows[0]['startTime'])
        self.assertEqual('KICKOFF_CONFLICT', conflicts[0]['reason'])

    def test_successful_empty_sources_replace_only_an_old_date(self):
        self.old_feed()
        feed, report, changed = self.generate(self.clients())
        self.assertTrue(changed)
        self.assertEqual(str(DAY), feed['date'])
        self.assertEqual('DEGRADED', feed['sportsDataMode'])
        self.assertEqual(0, sum(len(s['events']) for s in feed['sections']))
        self.assertEqual('DEGRADED', report['state'])

    def test_unavailable_sources_preserve_old_bytes(self):
        before = self.old_feed()
        clients = self.clients()
        clients['lpf_official'].matches.side_effect = ProviderError('LPF_OFFICIAL_UNAVAILABLE')
        clients['football_data'].matches.side_effect = ProviderError('HTTP_401')
        _, report, changed = self.generate(clients)
        self.assertFalse(changed)
        self.assertEqual('KEEP_LAST_GOOD', report['state'])
        self.assertEqual(before, self.output.read_bytes())

    def test_empty_refresh_never_erases_same_day_cards(self):
        self.generate(self.clients([official()]))
        before = self.output.read_bytes()
        _, report, changed = self.generate(self.clients())
        self.assertFalse(changed)
        self.assertEqual('KEEP_LAST_GOOD', report['state'])
        self.assertEqual(before, self.output.read_bytes())

    def test_discovery_respects_remaining_shared_public_budget(self):
        sources = {'limits': {'maxRequests': 6}}
        transport = Mock()
        with patch.object(discovery, 'read_json', side_effect=[sources, {}, None, None]), \
             patch('scripts.sports_channel_rules.load_clean_groups', return_value={}), \
             patch.object(discovery, 'discover', return_value=({}, {}, {'providerWarnings': []})) as discover:
            discovery.discover_from_root({}, self.root, request_limit=3, transport=transport)
        self.assertEqual(3, discover.call_args.args[2]['limits']['maxRequests'])
        self.assertIs(transport, discover.call_args.kwargs['transport'])
        self.assertEqual(6, agenda.MAX_GENERAL_PUBLIC_REQUESTS)


if __name__ == '__main__':
    unittest.main()
