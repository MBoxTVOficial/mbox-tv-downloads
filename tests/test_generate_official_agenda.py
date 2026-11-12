"""Publication wrapper contracts with Temp roots and offline public-source mocks."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import generate_official_agenda as agenda
from scripts import generate_agenda_today as entrypoint
from scripts.broadcaster_sources import ZONE

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, tzinfo=ZONE)


def row(identifier='source-row', **changes):
    value = {'providerFixtureId': identifier, 'competition': 'Torneo Proyección',
             'competitionId': 'lpf-proyeccion', 'source': 'lpf_official',
             'homeTeam': 'Club Azul', 'awayTeam': 'Club Verde',
             'startTime': DAY.isoformat() + 'T19:00:00-03:00',
             'sourceUrl': 'https://www.ligaprofesional.ar/notas/proyeccion/2026/11/10/agenda/',
             'broadcasters': []}
    value.update(changes)
    return value


class GenerateOfficialAgendaTests(unittest.TestCase):
    def test_general_official_sites_flag_never_enters_legacy_api_pipeline(self):
        feed, report, _ = self.generate()
        with patch.object(entrypoint, 'generate_agenda') as legacy, \
                patch.object(agenda, 'generate_official_agenda', return_value=(feed, report, False)) as sites, \
                patch.object(entrypoint, 'datetime') as clock:
            clock.now.return_value = NOW
            self.assertEqual(0, entrypoint.main(['--official-sites', '--output', str(self.output)]))
        legacy.assert_not_called()
        sites.assert_called_once()

    def test_confirmed_channels_stay_in_feed_before_client_ten_minute_window(self):
        rows = [row(broadcasters=['ESPN Premium'])]
        for hour, minute in [(0, 0), (18, 49), (18, 50), (19, 0)]:
            report = {'publicRequests': 0, 'sportsApiRequests': 0, 'guides': [],
                      'sources': [{'competition': 'Torneo Proyección', 'state': 'OK', 'fixturesToday': 1}]}
            with patch.object(agenda, 'acquire_all', return_value=(rows, {}, {}, [], report)):
                feed, result, _ = agenda.generate_official_agenda(self.root, self.output, DAY,
                                                                 NOW.replace(hour=hour, minute=minute))
            event = agenda._events(feed)[0]
            self.assertEqual([183616, 183615, 183614], [c['streamId'] for c in event['channels']])
            self.assertEqual('SCHEDULED', event['status'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'sports_today.json'
        for name in ('sports_official_sources.json', 'sports_agenda_guides.json',
                     'sports_broadcaster_mapping.json', 'mbox_sports_channels_clean.json'):
            shutil.copyfile(ROOT / name, self.root / name)

    def generate(self, rows=None, report=None, **kwargs):
        rows = [row()] if rows is None else rows
        report = report or {'publicRequests': 0, 'sportsApiRequests': 0,
                           'sources': [{'competition': 'Torneo Proyección', 'state': 'OK',
                                        'fixturesToday': len(rows)}], 'guides': []}
        with patch.object(agenda, 'acquire_all', return_value=(rows, {}, {}, [], report)):
            return agenda.generate_official_agenda(self.root, self.output, DAY, NOW, **kwargs)

    def bytes(self):
        return {name: (self.root / name).read_bytes() for name in
                ('sports_today.json', 'sports_broadcasts_today.json', 'sports_fixture_identity.json')}

    def test_valid_agenda_persists_all_three_artifacts_without_scores_or_api_state(self):
        feed, report, changed = self.generate()
        self.assertTrue(changed)
        self.assertEqual('DEGRADED', report['state'])
        self.assertEqual(DAY.isoformat(), feed['date'])
        event = agenda._events(feed)[0]
        self.assertEqual('SCHEDULED', event['status'])
        for key in ('homeScore', 'awayScore', 'venue', 'channels'):
            self.assertNotIn(key, event)
        self.assertEqual(3, len(self.bytes()))

    def test_official_confirmed_group_has_primary_then_backups_from_midnight(self):
        feed, _, _ = self.generate([row(broadcasters=['ESPN Premium'])])
        self.assertEqual([183616, 183615, 183614], [c['streamId'] for c in agenda._events(feed)[0]['channels']])

    def test_total_failure_preserves_three_files_byte_for_byte(self):
        self.generate([row(broadcasters=['TNT Sports'])])
        before = self.bytes()
        with patch.object(agenda, 'acquire_all', side_effect=TimeoutError('secret-value')):
            feed, report, changed = agenda.generate_official_agenda(self.root, self.output, DAY, NOW)
        self.assertFalse(changed)
        self.assertEqual(before, self.bytes())
        self.assertEqual('KEEP_LAST_GOOD', report['state'])
        self.assertNotIn('secret-value', json.dumps(report))

    def test_empty_same_day_never_erases_nonempty_last_good(self):
        self.generate()
        before = self.bytes()
        _, report, changed = self.generate([])
        self.assertFalse(changed)
        self.assertEqual('EMPTY_AGENDA', report['reason'])
        self.assertEqual(before, self.bytes())

    def test_cold_failure_does_not_create_empty_feed(self):
        feed, report, changed = self.generate([], {'publicRequests': 6, 'sportsApiRequests': 0,
            'sources': [{'state': 'DEFERRED_BUDGET'}], 'guides': [{'state': 'ERROR'}]})
        self.assertIsNone(feed)
        self.assertFalse(changed)
        self.assertFalse(self.output.exists())
        self.assertEqual('NO_VALID_SOURCE', report['reason'])

    def test_recognized_empty_date_is_valid_on_cold_start(self):
        feed, report, changed = self.generate([])
        self.assertTrue(changed)
        self.assertEqual([], agenda._events(feed))
        self.assertEqual(DAY.isoformat(), feed['date'])

    def test_current_cache_can_prove_empty_date(self):
        feed, _, changed = self.generate([], {'publicRequests': 0, 'sportsApiRequests': 0,
            'sources': [{'state': 'CACHE_HIT', 'fixturesToday': 0}], 'guides': []})
        self.assertTrue(changed)
        self.assertIsNotNone(feed)

    def test_new_date_never_inherits_manual_assignment(self):
        yesterday = DAY - timedelta(days=1)
        daily = {'schemaVersion': 1, 'date': str(yesterday), 'timezone': agenda.generator.TIMEZONE,
                 'broadcasts': [{'fixtureId': 111, 'channelGroups': ['tnt_sports_premium'],
                                 'confidence': 'confirmed', 'source': 'manual'}]}
        agenda.atomic_json(self.root / 'sports_broadcasts_today.json', daily)
        feed, _, _ = self.generate()
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        saved = json.loads((self.root / 'sports_broadcasts_today.json').read_text(encoding='utf8'))
        self.assertEqual(str(DAY), saved['date'])
        self.assertEqual([], saved['broadcasts'])

    def test_manual_confirmed_wins_over_automatic_fixture_tv(self):
        feed, _, _ = self.generate()
        identifier = int(agenda._events(feed)[0]['id'].removeprefix('fixture-'))
        daily = {'schemaVersion': 1, 'date': str(DAY), 'timezone': agenda.generator.TIMEZONE,
                 'broadcasts': [{'fixtureId': identifier, 'channelGroups': ['espn_premium'],
                                 'confidence': 'confirmed', 'source': 'manual'}]}
        agenda.atomic_json(self.root / 'sports_broadcasts_today.json', daily)
        feed, _, _ = self.generate([row(broadcasters=['TNT Sports'])])
        self.assertEqual([183616, 183615, 183614], [c['streamId'] for c in agenda._events(feed)[0]['channels']])

    def test_changed_source_identity_does_not_transfer_confirmed_channels(self):
        self.generate([row(broadcasters=['TNT Sports'])])
        before = self.bytes()
        _, report, changed = self.generate([row(identifier='other-source-key', homeTeam='Other Club', broadcasters=['TNT Sports'])])
        self.assertFalse(changed)
        self.assertEqual('IDENTITY_RECONCILIATION_REQUIRED', report['reason'])
        self.assertEqual(before, self.bytes())

    def test_exact_fixture_reconciles_identity_without_transferring_old_broadcaster(self):
        old, _, _ = self.generate([row(broadcasters=['TNT Sports'])])
        new, report, _ = self.generate([row(identifier='new-source-row', broadcasters=['ESPN Premium'])])
        self.assertEqual(agenda._events(old)[0]['id'], agenda._events(new)[0]['id'])
        self.assertEqual(1, report['exactIdentityReconciliations'])
        self.assertEqual([183616, 183615, 183614], [c['streamId'] for c in agenda._events(new)[0]['channels']])

    def test_reversed_or_different_kickoff_is_never_reconciled(self):
        self.generate([row(broadcasters=['TNT Sports'])])
        before = self.bytes()
        for change in ({'startTime': DAY.isoformat() + 'T20:00:00-03:00'},
                       {'homeTeam': 'Club Verde', 'awayTeam': 'Club Azul'}):
            _, report, changed = self.generate([row(identifier='new-source-row', broadcasters=['ESPN Premium'], **change)])
            self.assertFalse(changed)
            self.assertEqual('IDENTITY_RECONCILIATION_REQUIRED', report['reason'])
            self.assertEqual(before, self.bytes())

    def test_unknown_lpfplay_never_gets_iptv_options(self):
        feed, report, _ = self.generate([row(broadcasters=['LPF Play'])])
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('CONFIRMED_EXTERNAL', report['fixtures'][0]['state'])

    def test_failed_tv_lookup_keeps_same_day_confirmed_group_from_daily_configuration(self):
        self.generate([row(broadcasters=['TNT Sports'])])
        feed, report, _ = self.generate([row()], {'publicRequests': 3, 'sportsApiRequests': 0,
            'sources': [{'competition': 'Torneo Proyección', 'state': 'OK', 'fixturesToday': 1}],
            'guides': [{'state': 'ERROR'}]})
        self.assertEqual([165664, 165666, 165665], [c['streamId'] for c in agenda._events(feed)[0]['channels']])
        self.assertTrue(report['fixtures'][0]['preservedConfirmed'])

    def test_official_conflict_does_not_restore_previous_confirmed_group(self):
        self.generate([row(broadcasters=['TNT Sports'])])
        feed, report, _ = self.generate([row()], {'publicRequests': 3, 'sportsApiRequests': 0,
            'blockedCompetitions': ['Torneo Proyección'],
            'sources': [{'competition': 'Torneo Proyección', 'state': 'REVIEW', 'fixturesToday': 1}],
            'guides': [{'state': 'ERROR'}]})
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('REVIEW', report['fixtures'][0]['state'])

    def test_identity_is_stable_on_second_generation(self):
        first, _, _ = self.generate()
        second, _, changed = self.generate()
        self.assertFalse(changed)
        self.assertEqual(agenda._events(first)[0]['id'], agenda._events(second)[0]['id'])

    def test_invalid_identity_file_is_not_silently_replaced(self):
        self.generate()
        identity = self.root / 'sports_fixture_identity.json'
        identity.write_text('{invalid}', encoding='utf8')
        before = self.bytes()
        _, report, changed = self.generate()
        self.assertFalse(changed)
        self.assertEqual('GENERATION_FAILED', report['reason'])
        self.assertEqual(before, self.bytes())

    def test_invalid_cache_does_not_prevent_fresh_acquisition(self):
        (self.root / 'official_source_cache.json').write_text('{invalid}', encoding='utf8')
        feed, report, _ = self.generate()
        self.assertIsNotNone(feed)
        self.assertIn('INVALID_CACHE_IGNORED', report['warnings'])

    def test_all_supported_official_sources_use_same_feed_contract(self):
        specs = json.loads((self.root / 'sports_official_sources.json').read_text(encoding='utf8'))['sources']
        rows = [row(identifier='source-'+spec['id'], competition=spec['competition'],
                    competitionId=spec['id'], source=spec['provider'], sourceUrl=spec['url'],
                    homeTeam='Club Local ' + spec['id'], awayTeam='Club Visitante '+spec['id'])
                for spec in specs]
        feed, report, changed = self.generate(rows)
        self.assertTrue(changed)
        self.assertEqual(5, len(agenda._events(feed)))
        self.assertEqual(0, report['sportsApiRequests'])
        self.assertTrue(all(not event.get('channels') for event in agenda._events(feed)))

    def test_guide_only_fixture_is_editorial_not_official(self):
        feed, report, _ = self.generate([row(source='argentina_agendas',
            sourceUrl='https://www.futbolenvivoargentina.com/', broadcasters=[])])
        self.assertEqual('EDITORIAL_AGENDA', report['fixtures'][0]['sourceConfidence'])
        self.assertFalse(agenda._events(feed)[0].get('channels'))

    def test_official_review_quarantine_cannot_reactivate_channels(self):
        feed, report, _ = self.generate([row(broadcasters=['TNT Sports'])], {
            'publicRequests': 0, 'sportsApiRequests': 0,
            'blockedCompetitions': ['Torneo Proyección'],
            'sources': [{'state': 'REVIEW', 'competition': 'Torneo Proyección', 'fixturesToday': 1}], 'guides': []})
        self.assertEqual('REVIEW', report['fixtures'][0]['state'])
        self.assertFalse(agenda._events(feed)[0].get('channels'))

    def test_unmapped_manual_is_preserved_even_without_previous_feed(self):
        daily = {'schemaVersion': 1, 'date': str(DAY), 'timezone': agenda.generator.TIMEZONE,
                 'broadcasts': [{'fixtureId': 123456, 'channelGroups': ['espn_premium'],
                                 'confidence': 'confirmed', 'source': 'manual'}]}
        path = self.root / 'sports_broadcasts_today.json'
        agenda.atomic_json(path, daily)
        before = path.read_bytes()
        feed, report, changed = self.generate()
        self.assertIsNone(feed)
        self.assertFalse(changed)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual('IDENTITY_RECONCILIATION_REQUIRED', report['reason'])

    def test_mismatched_target_date_fails_closed(self):
        feed, report, changed = agenda.generate_official_agenda(self.root, self.output, DAY,
                                                             NOW + timedelta(days=1), offline=True)
        self.assertIsNone(feed)
        self.assertFalse(changed)
        self.assertEqual('CLOCK_DATE_MISMATCH', report['reason'])

    def test_output_cannot_overwrite_configuration_or_assignments(self):
        config = self.root / 'sports_official_sources.json'
        before = config.read_bytes()
        feed, report, changed = agenda.generate_official_agenda(self.root, config, DAY, NOW, offline=True)
        self.assertIsNone(feed)
        self.assertFalse(changed)
        self.assertEqual('INVALID_OUTPUT_TARGET', report['reason'])
        self.assertEqual(before, config.read_bytes())

    def test_valid_partial_agenda_warns_without_erasing_available_rows(self):
        feed, report, changed = self.generate(report={'publicRequests': 3, 'sportsApiRequests': 0,
            'sources': [{'state': 'ERROR'}], 'guides': [{'state': 'OK', 'fixtures': 1}]})
        self.assertTrue(changed)
        self.assertEqual(1, len(agenda._events(feed)))
        self.assertIn('PUBLIC_SOURCES_PARTIAL', report['warnings'])

    def test_shared_public_budget_never_calls_sports_api(self):
        network = Mock()
        network.fetch.side_effect = TimeoutError('private raw error')
        feed, report, changed = agenda.generate_official_agenda(self.root, self.output, DAY, NOW, transport=network)
        self.assertIsNone(feed)
        self.assertFalse(changed)
        self.assertLessEqual(network.fetch.call_count, 6)
        self.assertEqual(network.fetch.call_count, report['publicRequests'])
        self.assertEqual(0, report['sportsApiRequests'])
        self.assertNotIn('private raw error', json.dumps(report))

    def test_publish_rolls_back_all_three_files_on_write_failure(self):
        self.generate()
        before = self.bytes()
        original = agenda._replace_bytes
        count = 0
        def failing_write(path, data):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('private filesystem detail')
            return original(path, data)
        with patch.object(agenda, '_replace_bytes', side_effect=failing_write):
            _, report, changed = self.generate([row(identifier='new-row', broadcasters=['TNT Sports'])])
        self.assertFalse(changed)
        self.assertEqual('GENERATION_FAILED', report['reason'])
        self.assertEqual(before, self.bytes())

    def test_cli_old_date_feed_returns_failure_without_dispatch(self):
        stale = {'date': str(DAY - timedelta(days=1))}
        with patch.object(agenda, 'generate_official_agenda', return_value=(stale, {}, False)):
            self.assertEqual(1, agenda.main(['--root', str(self.root), '--offline']))

    def test_cli_current_date_keep_last_good_is_success(self):
        with patch.object(agenda, 'datetime') as clock, patch.object(agenda, 'generate_official_agenda',
                return_value=({'date': str(DAY)}, {'state': 'KEEP_LAST_GOOD'}, False)):
            clock.now.return_value = NOW
            self.assertEqual(0, agenda.main(['--root', str(self.root), '--offline']))


if __name__ == '__main__':
    unittest.main()
