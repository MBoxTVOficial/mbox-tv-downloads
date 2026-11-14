"""A fresh agenda omission cannot retract exact same-day confirmed TV evidence."""
import copy
from datetime import date, datetime
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_official_agenda as agenda, prioritized_agendas as guides
from scripts.broadcaster_sources import Listing, ZONE


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 13, 21, 50, tzinfo=ZONE)


class ConfirmedTVRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / 'sports_today.json'
        for name in ('sports_official_sources.json', 'sports_agenda_guides.json',
                     'sports_broadcaster_mapping.json', 'mbox_sports_channels_clean.json'):
            shutil.copyfile(ROOT / name, self.root / name)
        self.config = json.loads((self.root / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        self.official = json.loads((self.root / 'sports_official_sources.json').read_text(encoding='utf8'))

    def records(self, first=('TyC Sports',), second=('TyC Sports',), second_hour=20):
        result = []
        for index, signals in enumerate((first, second)):
            if signals is None:
                continue  # This guide omitted the fixture entirely.
            item = Listing('Argentina', 'Uruguay', NOW.replace(hour=20 if index == 0 else second_hour, minute=0),
                           'Amistosos de selecciones', signals)
            result += guides.checked_records([item], guides.PROVIDERS[index], DAY, self.config)
        return result

    def generate(self, records, rows=None):
        if rows is None:
            rows, conflicts = guides.merge_rows([], records, DAY, self.official, self.config)
        else:
            conflicts = []
        report = {'publicRequests': 0, 'sportsApiRequests': 0,
                  'sources': [], 'guides': [{'source': provider['id'], 'state': 'OK', 'fixtures': len(records)}
                                           for provider in guides.PROVIDERS[:2]],
                  'conflicts': conflicts}
        with patch.object(agenda, 'acquire_all', return_value=(rows, {}, {}, records, report)):
            return agenda.generate_official_agenda(self.root, self.output, DAY, NOW)

    def assignment(self):
        return json.loads((self.root / 'sports_broadcasts_today.json').read_text(encoding='utf8'))['broadcasts'][0]

    def test_fresh_empty_tv_keeps_prior_confirmation_with_historical_provenance(self):
        self.generate(self.records())
        previous = copy.deepcopy(self.assignment())
        feed, report, _ = self.generate(self.records((), ()))
        self.assertEqual([165660, 165659], [channel['streamId'] for channel in agenda._events(feed)[0]['channels']])
        detail = report['fixtures'][0]
        self.assertEqual('UNRESOLVED', detail['currentEvidenceState'])
        self.assertEqual('INCOMPLETE_CURRENT_TV_EVIDENCE', detail['preservationReason'])
        self.assertEqual(previous, detail['preservedAssignment'])
        self.assertEqual([], detail['evidence'])
        self.assertEqual(previous, self.assignment())

    def test_one_guide_omitting_fixture_cannot_erase_prior_two_owner_confirmation(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(second=None))
        self.assertTrue(agenda._events(feed)[0].get('channels'))
        self.assertEqual('REVIEW', report['fixtures'][0]['currentEvidenceState'])
        self.assertEqual(2, self.assignment()['evidenceCount'])
        self.assertEqual(1, len({item['owner'] for item in report['fixtures'][0]['evidence']}))

    def test_fresh_subset_retains_other_previously_confirmed_group(self):
        signals = ('TyC Sports', 'TNT Sports')
        self.generate(self.records(signals, signals))
        feed, report, _ = self.generate(self.records())
        self.assertEqual(['tyc_sports', 'tnt_sports_premium'], self.assignment()['channelGroups'])
        self.assertEqual(['tnt_sports_premium'], report['fixtures'][0]['preservedChannelGroups'])
        identifiers = [channel['streamId'] for channel in agenda._events(feed)[0]['channels']]
        self.assertEqual([165660, 165664], identifiers[:2])

    def test_explicit_new_tv_disagreement_replaces_prior_group(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(('ESPN',), ('ESPN',)))
        self.assertEqual(['espn'], self.assignment()['channelGroups'])
        self.assertEqual('CURRENT_TV_CONTRADICTS_PRIOR', report['fixtures'][0]['preservationBlockedReason'])
        self.assertNotIn(165660, [channel['streamId'] for channel in agenda._events(feed)[0]['channels']])

    def test_current_guide_signal_conflict_stays_review(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(('TyC Sports',), ('ESPN',)))
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('REVIEW', report['fixtures'][0]['state'])
        self.assertEqual('CURRENT_SOURCE_CONFLICT', report['fixtures'][0]['preservationBlockedReason'])

    def test_current_guide_kickoff_conflict_stays_review(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(second_hour=21))
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('CURRENT_SOURCE_CONFLICT', report['fixtures'][0]['preservationBlockedReason'])

    def test_wrong_territory_claim_does_not_restore_prior_group(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(('ESPN Brasil',), ()))
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('AMBIGUOUS_CURRENT_SIGNAL', report['fixtures'][0]['preservationBlockedReason'])

    def test_unrecognized_positive_claim_cannot_masquerade_as_platform_only(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(('Canal 9',), None))
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('UNRECOGNIZED_CURRENT_SIGNAL', report['fixtures'][0]['preservationBlockedReason'])

    def test_previously_confirmed_unknown_name_is_exact_historical_evidence(self):
        signals = ('TyC Sports', 'Canal 9')
        self.generate(self.records(signals, signals))
        feed, report, _ = self.generate(self.records(('CANAL 9',), None))
        self.assertTrue(agenda._events(feed)[0].get('channels'))
        self.assertEqual('INCOMPLETE_CURRENT_TV_EVIDENCE', report['fixtures'][0]['preservationReason'])

    def test_known_external_platform_alone_does_not_retract_tv(self):
        self.generate(self.records())
        feed, report, _ = self.generate(self.records(('Disney+',), ('Disney+',)))
        self.assertTrue(agenda._events(feed)[0].get('channels'))
        self.assertEqual('CONFIRMED_EXTERNAL', report['fixtures'][0]['currentEvidenceState'])
        self.assertEqual(['tyc_sports'], self.assignment()['channelGroups'])

    def unavailable_group(self, change):
        self.generate(self.records())
        path = self.root / 'mbox_sports_channels_clean.json'
        clean = json.loads(path.read_text(encoding='utf8'))
        group = next(group for group in clean['groups'] if group['id'] == 'tyc_sports')
        if change == 'disabled':
            for stream in group['streams']:
                stream['enabled'] = False
        else:
            group['signalRegion'] = None
        path.write_text(json.dumps(clean), encoding='utf8')
        feed, report, _ = self.generate(self.records((), ()))
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('PRIOR_GROUP_UNAVAILABLE', report['fixtures'][0]['preservationBlockedReason'])

    def test_disabled_group_cannot_be_restored(self):
        self.unavailable_group('disabled')

    def test_unverified_region_group_cannot_be_restored(self):
        self.unavailable_group('region')

    def test_new_official_tv_claim_does_not_inherit_previous_signal(self):
        official = {'providerFixtureId': 'official-fixture', 'competition': 'Torneo Proyección',
                    'competitionId': 'lpf-proyeccion', 'source': 'lpf_official',
                    'homeTeam': 'Club Azul', 'awayTeam': 'Club Verde',
                    'startTime': DAY.isoformat() + 'T20:00:00-03:00',
                    'sourceUrl': 'https://www.ligaprofesional.ar/notas/proyeccion/2026/11/13/agenda/',
                    'broadcasters': ['TyC Sports']}
        self.generate([], [official])
        feed, report, _ = self.generate([], [{**official, 'broadcasters': ['LPF Play']}])
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('CONFIRMED_EXTERNAL', report['fixtures'][0]['state'])
        self.assertEqual('CURRENT_OFFICIAL_TV_REPLACES_PRIOR', report['fixtures'][0]['preservationBlockedReason'])

    def test_changed_exact_kickoff_does_not_inherit_tv(self):
        self.generate(self.records())
        records = self.records((), ())
        rows, _ = guides.merge_rows([], records, DAY, self.official, self.config)
        rows[0]['startTime'] = DAY.isoformat() + 'T21:00:00-03:00'
        feed, report, _ = self.generate([], rows)
        self.assertFalse(agenda._events(feed)[0].get('channels'))
        self.assertEqual('FIXTURE_IDENTITY_CHANGED', report['fixtures'][0]['preservationBlockedReason'])


if __name__ == '__main__':
    unittest.main()
