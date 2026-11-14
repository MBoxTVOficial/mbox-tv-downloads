"""Verified Telefe identities remain separate from fixture broadcast evidence."""
from datetime import datetime
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from scripts import broadcaster_discovery as discovery
from scripts import broadcaster_sources as sources
from scripts.sports_channel_rules import load_clean_groups
from scripts.sports_daily_broadcasts import resolve_broadcasts, TIMEZONE


ROOT = Path(__file__).resolve().parents[1]
KICKOFF = datetime(2026, 11, 13, 20, tzinfo=sources.ZONE)


class TelefeBroadcasterMappingTests(unittest.TestCase):
    def setUp(self):
        self.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', sources.norm)
        self.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        self.fixture = sources.Fixture(7001, 'Argentina', 'Uruguay', 'Amistosos de selecciones',
                                       KICKOFF, 'international', 'Argentina')
        self.feed = {'schemaVersion': 1, 'date': '2026-11-13', 'timezone': TIMEZONE,
                     'sections': [{'id': 'international', 'events': [{
                         'id': 'fixture-7001', 'competition': self.fixture.competition,
                         'homeTeam': self.fixture.home, 'awayTeam': self.fixture.away,
                         'startTime': '20:00', 'status': 'SCHEDULED'}]}]}
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('Offline tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def evidence(self, signals=('Telefe',), owners=('football-on-tv', 'ole-tyc-conservative'),
                 territory='Argentina'):
        return [sources.Evidence(self.fixture.fixture_id, KICKOFF.isoformat(), signal, territory,
                    owner, owner, False, True, f'https://example.com/{owner}', 'trusted_guide')
                for owner in owners for signal in signals]

    def resolve(self, decision):
        if decision['state'] != 'CONFIRMED_MBOX':
            return []
        daily = {'schemaVersion': 1, 'date': self.feed['date'], 'timezone': TIMEZONE,
                 'broadcasts': [{'fixtureId': self.fixture.fixture_id, 'confidence': 'confirmed',
                     'source': 'auto', 'channelGroups': decision['channelGroups']}]}
        return resolve_broadcasts(daily, self.feed, self.groups, sources.norm)['fixture-7001']

    def test_two_independent_fixture_claims_resolve_only_verified_telefe_streams(self):
        decision = discovery.confidence(self.fixture, self.evidence(), self.mapping, self.groups)
        self.assertEqual('CONFIRMED_MBOX', decision['state'])
        self.assertEqual(['telefe'], decision['channelGroups'])
        self.assertEqual('C', decision['evidenceLevel'])
        self.assertEqual([165581, 165582], [item['streamId'] for item in self.resolve(decision)])

    def test_one_guide_does_not_confirm_a_telefe_fixture(self):
        decision = discovery.confidence(self.fixture, self.evidence(owners=('football-on-tv',)),
                                         self.mapping, self.groups)
        self.assertEqual('REVIEW', decision['state'])
        self.assertEqual([], self.resolve(decision))

    def test_channel_identity_without_fixture_evidence_never_assigns(self):
        decision = discovery.confidence(self.fixture, [], self.mapping, self.groups)
        self.assertEqual('UNRESOLVED', decision['state'])
        self.assertEqual([], self.resolve(decision))

    def test_telefe_is_exact_and_argentina_only(self):
        self.assertEqual('telefe', discovery.map_signal('TELEFE', 'Argentina', self.mapping)[2])
        for name, territory in (('Telefe', 'Chile'), ('Telefe', 'Mexico'),
                                ('Telefe', ''), ('Telefe Córdoba', 'Argentina'),
                                ('Telefe Internacional', 'Argentina')):
            with self.subTest(name=name, territory=territory):
                self.assertIsNone(discovery.map_signal(name, territory, self.mapping)[2])

    def test_wrong_territory_fixture_evidence_stays_non_playable(self):
        decision = discovery.confidence(self.fixture, self.evidence(territory='Chile'),
                                         self.mapping, self.groups)
        self.assertEqual('REVIEW', decision['state'])
        self.assertEqual([], self.resolve(decision))

    def test_backup_order_and_deduplication_exclude_unverified_third_signal(self):
        evidence = self.evidence(('Telefe', 'TELEFE', 'Telefe'))
        decision = discovery.confidence(self.fixture, evidence, self.mapping, self.groups)
        options = self.resolve(decision)
        self.assertEqual([165581, 165582], [item['streamId'] for item in options])
        self.assertEqual([1, 2], [item['priority'] for item in options])
        self.assertNotIn(165583, [item['streamId'] for item in options])

    def test_tyc_and_telefe_keep_primaries_then_real_backups_with_existing_limit(self):
        decision = discovery.confidence(self.fixture, self.evidence(('TyC Sports', 'Telefe')),
                                         self.mapping, self.groups)
        self.assertEqual(['tyc_sports', 'telefe'], decision['channelGroups'])
        options = self.resolve(decision)
        self.assertEqual([165660, 165581, 165659, 165582], [item['streamId'] for item in options])
        self.assertEqual(len(options), len({item['streamId'] for item in options}))
        self.assertLessEqual(len(options), 5)

    def test_wrong_fixture_or_kickoff_cannot_transfer_telefe_confirmation(self):
        from dataclasses import replace
        for updates in ({'fixture_id': 7002}, {'kickoff': '2026-11-13T20:01:00-03:00'}):
            with self.subTest(updates=updates):
                evidence = [replace(item, **updates) for item in self.evidence()]
                decision = discovery.confidence(self.fixture, evidence, self.mapping, self.groups)
                self.assertEqual('UNRESOLVED', decision['state'])
                self.assertEqual([], self.resolve(decision))


if __name__ == '__main__':
    unittest.main()
