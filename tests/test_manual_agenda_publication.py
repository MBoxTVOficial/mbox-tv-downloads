"""Offline publication contracts for reversible date-scoped editorial changes."""
import contextlib
import copy
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_official_agenda as agenda
from scripts import manual_agenda_publication as publication
from scripts import sports_agenda_diagnostics as diagnostics
from scripts.broadcaster_sources import ZONE
from scripts.sports_agenda_window import validate_window


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 12, tzinfo=ZONE)
EVENT_CHANNEL = 777001  # Synthetic catalog reference, used only in isolated tests.


class ManualAgendaPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('sports_official_sources.json', 'sports_agenda_guides.json',
                     'sports_broadcaster_mapping.json', 'mbox_sports_channels_clean.json'):
            shutil.copyfile(ROOT / name, self.root / name)
        self.output = self.root / 'sports_today.json'
        self.catalog = {'schemaVersion': 1, 'channels': [
            {'streamId': 165660, 'name': 'ARG: TYC SPORTS HD', 'category': 'ARGENTINA'},
            {'streamId': 165659, 'name': 'ARG: TYC SPORTS HD*', 'category': 'ARGENTINA'},
            {'streamId': 165664, 'name': 'ARG: TNT SPORTS PREMIUM FHD', 'category': 'ARGENTINA'},
            {'streamId': EVENT_CHANNEL, 'name': 'EVENTO TEST 03', 'category': 'EVENTOS DIARIOS'},
        ]}
        self.write(publication.CATALOG, self.catalog)
        self.write(publication.MANUAL, publication.empty_config())

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')

    def read(self, name):
        return json.loads((self.root / name).read_text(encoding='utf8'))

    def published_bytes(self):
        names = ('sports_today.json', 'sports_agenda_window.json', publication.BASE,
                 'sports_fixture_identity.json', 'sports_broadcasts_today.json',
                 'sports_match_updates.json', 'sports_match_updates_state.json')
        return {name: (self.root / name).read_bytes() for name in names if (self.root / name).exists()}

    @staticmethod
    def events(feed):
        return [event for section in feed['sections'] for event in section['events']]

    def current_events(self):
        return self.events(self.read('sports_today.json'))

    @staticmethod
    def channel_ids(event):
        return [channel['streamId'] for channel in event.get('channels', [])]

    def manual_event(self, *, target=DAY, identifier='argentina-australia-sub17'):
        return {'id': identifier, 'date': target.isoformat(), 'sectionId': 'international',
                'competition': 'Mundial Sub-17', 'homeTeam': 'Argentina Sub-17',
                'awayTeam': 'Australia Sub-17', 'startTime': '13:00',
                'channelIds': [EVENT_CHANNEL, 165660]}

    def configure(self, *, events=(), overrides=()):
        value = publication.empty_config()
        value.update(events=list(events), channelOverrides=list(overrides))
        self.write(publication.MANUAL, value)
        return value

    def generate(self, now=NOW, *, targets=(0,), signal='TyC Sports'):
        by_day = {}
        for offset in targets:
            target = now.date() + timedelta(days=offset)
            by_day[target.isoformat()] = [{
                'providerFixtureId': 'automatic-reserve-' + target.isoformat(),
                'competition': 'Torneo Proyección', 'competitionId': 'lpf-proyeccion',
                'source': 'lpf_official', 'homeTeam': 'Club Azul Reserva',
                'awayTeam': 'Club Verde Reserva', 'startTime': target.isoformat() + 'T19:00:00-03:00',
                'sourceUrl': 'https://www.ligaprofesional.ar/notas/proyeccion/2026/11/19/agenda/',
                'broadcasters': [signal] if signal else [],
            }]
        rows = by_day[now.date().isoformat()]
        report = {'publicRequests': 0, 'sportsApiRequests': 0, 'guides': [],
                  'sources': [{'source': 'lpf-proyeccion', 'competition': 'Torneo Proyección',
                               'state': 'OK', 'fixturesToday': len(rows)}],
                  'windowRows': by_day, 'windowRecords': {key: [] for key in by_day},
                  'windowGuides': {key: [] for key in by_day}}
        with patch.object(agenda, 'acquire_all', return_value=(rows, {}, {}, [], report)) as acquisition:
            feed, report, changed = agenda.generate_official_agenda(
                self.root, self.output, now.date(), now)
        acquisition.assert_called_once()
        self.assertEqual('DEGRADED', report['state'], report)
        self.assertEqual(now.date().isoformat(), feed['date'])
        return feed, report, changed

    def test_empty_manual_file_keeps_automatic_feed_and_confirmed_channel_order(self):
        feed, report, _ = self.generate()
        self.assertEqual(1, len(self.events(feed)))
        self.assertEqual([165660, 165659], self.channel_ids(self.events(feed)[0]))
        self.assertEqual(0, report['manual']['eventsAdded'])
        base = publication.read_base(self.root)
        self.assertEqual(feed, base['automaticWindow']['days'][DAY.isoformat()])
        self.assertEqual(publication.empty_config(), base['lastGoodManualConfig'])

    def test_general_saves_pure_automatic_base_before_manual_additions_or_replacements(self):
        automatic, _, _ = self.generate()
        original = self.events(automatic)[0]
        identifier = int(original['id'].removeprefix('fixture-'))
        config = self.configure(events=[self.manual_event()], overrides=[{
            'date': DAY.isoformat(), 'fixtureId': identifier, 'channelIds': [165664]}])
        public, _, _ = self.generate(NOW + timedelta(minutes=1))
        self.assertEqual(2, len(self.events(public)))
        base = publication.read_base(self.root)
        automatic_events = self.events(base['automaticWindow']['days'][DAY.isoformat()])
        self.assertEqual([original], automatic_events)
        self.assertEqual(config, base['lastGoodManualConfig'])
        self.assertEqual([165660, 165659], self.channel_ids(automatic_events[0]))
        public_original = next(event for event in self.events(public) if event['id'] == original['id'])
        self.assertEqual([165664], self.channel_ids(public_original))

    def test_manual_publication_never_calls_providers_http_or_iptv_when_sources_are_unavailable(self):
        self.generate()
        self.configure(events=[self.manual_event()])
        with patch.object(agenda, 'acquire_all', side_effect=TimeoutError('provider unavailable')) as sources, \
                patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')) as http, \
                patch('scripts.broadcaster_sources.PublicTransport.fetch', side_effect=AssertionError('transport forbidden')) as transport:
            report = publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        sources.assert_not_called()
        http.assert_not_called()
        transport.assert_not_called()
        self.assertEqual((0, 0, 0), tuple(report[field] for field in
                         ('publicRequests', 'sportsApiRequests', 'iptvRequests')))
        self.assertEqual(2, len(self.current_events()))

    def test_editor_can_add_sub17_card_using_exact_catalog_id_outside_curated_groups(self):
        automatic, _, _ = self.generate()
        automatic_event = self.events(automatic)[0]
        self.configure(events=[self.manual_event()])
        publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        events = self.current_events()
        added = next(event for event in events if event['competition'] == 'Mundial Sub-17')
        self.assertNotEqual(automatic_event['id'], added['id'])
        self.assertEqual([EVENT_CHANNEL, 165660], self.channel_ids(added))
        self.assertEqual(['EVENTO TEST 03', 'ARG: TYC SPORTS HD'], [c['name'] for c in added['channels']])
        self.assertEqual('SCHEDULED', added['status'])
        self.assertNotIn('homeScore', added)
        self.assertNotIn('awayScore', added)
        identity = self.read('sports_fixture_identity.json')
        key = 'manual_agenda:2026-11-20:argentina-australia-sub17'
        self.assertEqual(added['id'], 'fixture-' + str(identity['entries'][key]))
        publication.publish_manual(self.root, NOW + timedelta(minutes=2))
        self.assertEqual({event['id'] for event in events}, {event['id'] for event in self.current_events()})

    def test_override_requires_exact_fixture_id_and_art_date(self):
        feed, _, _ = self.generate()
        original = self.events(feed)[0]
        identifier = int(original['id'].removeprefix('fixture-'))
        self.configure(overrides=[{'date': DAY.isoformat(), 'fixtureId': identifier + 1,
                                   'channelIds': [EVENT_CHANNEL]}])
        report = publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        self.assertEqual([165660, 165659], self.channel_ids(self.current_events()[0]))
        self.assertEqual(1, report['manual']['pendingOverrides'])
        self.configure(overrides=[{'date': '2026-11-19', 'fixtureId': identifier,
                                   'channelIds': [EVENT_CHANNEL]}])
        report = publication.publish_manual(self.root, NOW + timedelta(minutes=2))
        self.assertEqual(0, report['manual']['channelOverridesApplied'])
        self.assertEqual([165660, 165659], self.channel_ids(self.current_events()[0]))
        self.configure(overrides=[{'date': DAY.isoformat(), 'fixtureId': identifier,
                                   'channelIds': [EVENT_CHANNEL]}])
        report = publication.publish_manual(self.root, NOW + timedelta(minutes=3))
        self.assertEqual(1, report['manual']['channelOverridesApplied'])
        self.assertEqual([EVENT_CHANNEL], self.channel_ids(self.current_events()[0]))

    def test_removing_override_restores_automatic_options_with_newer_update_timestamp(self):
        feed, _, _ = self.generate()
        identifier = int(self.events(feed)[0]['id'].removeprefix('fixture-'))
        self.configure(overrides=[{'date': DAY.isoformat(), 'fixtureId': identifier,
                                   'channelIds': [EVENT_CHANNEL]}])
        publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        prior = self.read('sports_today.json')
        self.configure()
        publication.publish_manual(self.root, NOW + timedelta(minutes=2))
        restored = self.read('sports_today.json')
        self.assertEqual([165660, 165659], self.channel_ids(self.events(restored)[0]))
        self.assertGreater(datetime.fromisoformat(restored['updatedAt']), datetime.fromisoformat(prior['updatedAt']))
        self.assertEqual((NOW + timedelta(minutes=2)).isoformat(timespec='seconds'), restored['updatedAt'])
        self.assertEqual('2026-11-20T12:00:00-03:00', publication.read_base(self.root)['automaticWindow']['days'][DAY.isoformat()]['updatedAt'])

    def test_deleting_confirmed_manual_card_does_not_block_next_general_refresh(self):
        self.configure(events=[self.manual_event()])
        feed, _, _ = self.generate()
        self.assertEqual(2, len(self.events(feed)))
        self.configure()
        feed, report, changed = self.generate(NOW + timedelta(hours=2))
        self.assertTrue(changed)
        self.assertEqual(1, len(self.events(feed)))
        self.assertNotIn('IDENTITY_RECONCILIATION_REQUIRED', report.get('warnings', []))
        self.assertEqual('Torneo Proyección', self.events(feed)[0]['competition'])

    def test_corrupt_base_after_manual_card_does_not_block_valid_general_recovery(self):
        self.configure(events=[self.manual_event()])
        first, _, _ = self.generate()
        self.assertEqual(2, len(self.events(first)))
        original = next(event for event in self.events(first) if event['competition'] == 'Torneo Proyección')
        (self.root / publication.BASE).write_text('{"broken":', encoding='utf8')
        self.configure()
        recovered, report, _ = self.generate(NOW + timedelta(hours=2))
        self.assertEqual([original['id']], [event['id'] for event in self.events(recovered)])
        self.assertIn('MANUAL_BASE_INVALID', report['warnings'])
        automatic = publication.read_base(self.root)['automaticWindow']['days'][DAY.isoformat()]
        self.assertEqual([original['id']], [event['id'] for event in self.events(automatic)])

    def test_missing_base_after_manual_card_does_not_reclassify_card_as_automatic(self):
        self.configure(events=[self.manual_event()])
        first, _, _ = self.generate()
        original = next(event for event in self.events(first) if event['competition'] == 'Torneo Proyección')
        manual_id = next(event['id'] for event in self.events(first) if event['competition'] == 'Mundial Sub-17')
        (self.root / publication.BASE).unlink()
        self.configure()
        recovered, _, _ = self.generate(NOW + timedelta(hours=2))
        self.assertEqual([original['id']], [event['id'] for event in self.events(recovered)])
        automatic = publication.read_base(self.root)['automaticWindow']['days'][DAY.isoformat()]
        self.assertNotIn(manual_id, [event['id'] for event in self.events(automatic)])
        self.assertEqual(1, len(self.events(automatic)))

    def test_corrupt_base_does_not_promote_override_to_automatic_tv_and_restores_dated_daily_evidence(self):
        first, _, _ = self.generate(signal='TNT Sports')
        automatic_event = self.events(first)[0]
        original_ids = self.channel_ids(automatic_event)
        self.assertEqual([165664, 165666, 165665], original_ids)
        identifier = int(automatic_event['id'].removeprefix('fixture-'))
        self.configure(overrides=[{'date': DAY.isoformat(), 'fixtureId': identifier,
                                   'channelIds': [165660]}])
        overlaid, _, _ = self.generate(NOW + timedelta(minutes=1), signal='TNT Sports')
        self.assertEqual([165660], self.channel_ids(self.events(overlaid)[0]))
        self.assertEqual(['tnt_sports_premium'], self.read('sports_broadcasts_today.json')['broadcasts'][0]['channelGroups'])
        (self.root / publication.BASE).write_text('{"broken":', encoding='utf8')
        self.configure()
        # Fresh fixture metadata omits TV; only the dated automatic broadcaster
        # record may restore its original TNT options. The public TyC overlay
        # must not become source evidence when the separate base is damaged.
        recovered, _, _ = self.generate(NOW + timedelta(hours=2), signal='')
        self.assertEqual(original_ids, self.channel_ids(self.events(recovered)[0]))
        automatic = publication.read_base(self.root)['automaticWindow']['days'][DAY.isoformat()]
        self.assertEqual(original_ids, self.channel_ids(self.events(automatic)[0]))
        self.assertNotIn(165660, self.channel_ids(self.events(automatic)[0]))

    def test_general_uses_last_valid_manual_config_when_editor_json_is_broken(self):
        valid = self.configure(events=[self.manual_event()])
        self.generate()
        (self.root / publication.MANUAL).write_text('{"broken":', encoding='utf8')
        feed, report, _ = self.generate(NOW + timedelta(hours=2))
        self.assertEqual(2, len(self.events(feed)))
        self.assertIn('MANUAL_CONFIG_INVALID', report['manual']['warnings'])
        self.assertIn('MANUAL_USING_LAST_GOOD_CONFIG', report['manual']['warnings'])
        self.assertEqual(valid, publication.read_base(self.root)['lastGoodManualConfig'])

    def test_invalid_manual_only_edit_leaves_all_published_artifacts_byte_identical(self):
        self.generate()
        before = self.published_bytes()
        (self.root / publication.MANUAL).write_text('{"secret":"must-not-be-logged",', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'MANUAL_CONFIG_INVALID'):
            publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        self.assertEqual(before, self.published_bytes())
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            status = publication.main(['--root', str(self.root)])
        self.assertEqual(1, status)
        self.assertIn('MANUAL_EDIT_NOT_PUBLISHED', captured.getvalue())
        self.assertNotIn('must-not-be-logged', captured.getvalue())
        self.assertEqual(before, self.published_bytes())

    def test_general_manual_warning_annotations_never_include_raw_editor_input(self):
        self.configure(events=[self.manual_event()])
        self.generate()
        (self.root / publication.MANUAL).write_text('{"broken":', encoding='utf8')
        feed, report, _ = self.generate(NOW + timedelta(hours=2))
        report = copy.deepcopy(report)
        report['manual']['rawEditorInput'] = 'private-editor-value'
        report['manual']['warnings'].extend(['private-editor-value', 'MANUAL_BAD\nannotation'])
        report['manual']['eventsRemoved'] = True  # A bool is not an integer diagnostic count.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            result = diagnostics.emit_result(feed, report, True, DAY, public=True)
        self.assertEqual(0, result)
        text = captured.getvalue()
        self.assertIn('::warning title=Manual football agenda::MANUAL_CONFIG_INVALID', text)
        self.assertIn('::warning title=Manual football agenda::MANUAL_USING_LAST_GOOD_CONFIG', text)
        self.assertNotIn('private-editor-value', text)
        self.assertNotIn('MANUAL_BAD', text)
        line = next(line for line in text.splitlines() if line.startswith('SPORTS_AGENDA MANUAL '))
        counts = json.loads(line.removeprefix('SPORTS_AGENDA MANUAL '))
        self.assertEqual(1, counts['eventsAdded'])
        self.assertNotIn('eventsRemoved', counts)

    def test_new_manual_identity_and_channel_edit_leave_result_sidecars_untouched(self):
        self.generate()
        for name in ('sports_match_updates.json', 'sports_match_updates_state.json'):
            self.write(name, {'preserved': name, 'date': DAY.isoformat(), 'results': [{'fixtureId': 123, 'score': [2, 1]}]})
        original = {name: (self.root / name).read_bytes() for name in
                    ('sports_match_updates.json', 'sports_match_updates_state.json')}
        self.configure(events=[self.manual_event()])
        report = publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        self.assertFalse(set(original).intersection(report['changedFiles']))
        self.assertEqual(original, {name: (self.root / name).read_bytes() for name in original})

    def test_missing_automatic_base_requires_bootstrap_and_never_adopts_current_overlay_as_source(self):
        self.generate()
        (self.root / publication.BASE).unlink()
        self.configure(events=[self.manual_event()])
        before = self.published_bytes()
        with self.assertRaisesRegex(ValueError, 'MANUAL_AUTOMATIC_BASE_REQUIRED'):
            publication.publish_manual(self.root, NOW + timedelta(minutes=1))
        self.assertEqual(before, self.published_bytes())
        self.assertFalse((self.root / publication.BASE).exists())

    def test_forecast_from_nineteenth_can_publish_twentieth_and_expires_prior_day_edits(self):
        yesterday = datetime(2026, 11, 19, 23, 55, tzinfo=ZONE)
        self.configure(events=[self.manual_event(target=date(2026, 11, 19), identifier='yesterday-only'),
                               self.manual_event(target=DAY, identifier='today-future')])
        self.generate(yesterday, targets=(0, 1, 2))
        future = self.read('sports_agenda_window.json')['days'][DAY.isoformat()]
        expected_id = next(event['id'] for event in self.events(future) if event['competition'] == 'Mundial Sub-17')
        report = publication.publish_manual(self.root, datetime(2026, 11, 20, 0, 3, tzinfo=ZONE))
        current = self.read('sports_today.json')
        self.assertEqual(DAY.isoformat(), current['date'])
        self.assertEqual(2, len(self.events(current)))
        self.assertEqual([expected_id], [event['id'] for event in self.events(current) if event['competition'] == 'Mundial Sub-17'])
        self.assertEqual(1, report['manual']['inactiveEvents'])
        self.assertNotIn('2026-11-19', self.read('sports_agenda_window.json')['days'])
        validate_window(self.read('sports_agenda_window.json'))

    def test_rollover_without_current_automatic_coverage_never_fabricates_today(self):
        yesterday = datetime(2026, 11, 19, 23, 55, tzinfo=ZONE)
        self.generate(yesterday)
        self.configure(events=[self.manual_event()])
        before = self.published_bytes()
        with self.assertRaisesRegex(ValueError, 'MANUAL_NO_CURRENT_AUTOMATIC_AGENDA'):
            publication.publish_manual(self.root, datetime(2026, 11, 20, 0, 3, tzinfo=ZONE))
        self.assertEqual(before, self.published_bytes())
        self.assertEqual('2026-11-19', self.read('sports_today.json')['date'])


class ManualAgendaWorkflowTests(unittest.TestCase):
    def test_manual_workflow_is_push_only_for_exact_two_inputs_without_any_sports_calls(self):
        workflow = (ROOT / '.github/workflows/apply-manual-sports-agenda.yml').read_text(encoding='utf8')
        self.assertRegex(workflow, r'(?m)^  push:\s*$')
        self.assertEqual({'sports_manual_agenda.json', 'sports_manual_channel_catalog.json'},
                         set(re.findall(r'(?m)^      - (sports_manual[^\s]+\.json)$', workflow)))
        self.assertNotRegex(workflow, r'(?m)^  (?:schedule|workflow_dispatch):')
        self.assertIn('python -m scripts.manual_agenda_publication', workflow)
        for forbidden in ('API_FOOTBALL_KEY', 'FOOTBALL_DATA_TOKEN', 'FOOTBALL_SOCCER_API_KEY',
                          'generate_public_agenda', 'generate_sports_match_updates'):
            self.assertNotIn(forbidden, workflow)
        files = re.search(r'files=\(([^)]+)\)', workflow).group(1).split()
        self.assertEqual({'sports_today.json', 'sports_agenda_window.json',
                          'sports_agenda_automatic.json', 'sports_fixture_identity.json'}, set(files))

    def test_manual_and_general_share_writer_queue_and_general_keeps_seven_artifact_whitelist(self):
        manual = (ROOT / '.github/workflows/apply-manual-sports-agenda.yml').read_text(encoding='utf8')
        general = (ROOT / '.github/workflows/update-sports-today.yml').read_text(encoding='utf8')
        for workflow in (manual, general):
            self.assertIn('group: sports-today-main', workflow)
            self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn("cron: '0 1-23/2 * * *'", general)
        expected = {'sports_today.json', 'sports_broadcasts_today.json', 'sports_fixture_identity.json',
                    'sports_match_updates.json', 'sports_match_updates_state.json',
                    'sports_agenda_window.json', 'sports_agenda_automatic.json'}
        actual = set(re.findall(r'\bsports_[a-z_]+\.json\b', general))
        self.assertEqual(expected, actual)


if __name__ == '__main__':
    unittest.main()
