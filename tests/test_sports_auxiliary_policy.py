import unittest
from datetime import date
from scripts.sports_auxiliary_policy import compare, local_start


class AuxiliaryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.official = dict(competition='Liga Profesional Argentina', homeTeam='Club Azul',
                             awayTeam='Club Verde', startTime='2026-10-11T19:30:00-03:00')
        self.provider = dict(self.official, competitionId='lg_22EEYKR', providerFixtureId='mt_sample',
                             status='LIVE', score=99, venue='unverified', broadcaster='unverified')

    def test_lpf_limited_only_reference(self):
        result = compare(self.official, self.provider, date(2026, 10, 11))
        self.assertEqual('LIMITED_AUXILIARY', result['state'])
        self.assertTrue(result['match'])
        self.assertEqual({'providerFixtureId': 'mt_sample'}, result['allowedFields'])

    def test_conflicting_time_no_enrichment(self):
        self.provider['startTime'] = '2026-10-11T19:31:00-03:00'
        self.assertEqual({}, compare(self.official, self.provider, date(2026, 10, 11))['allowedFields'])

    def test_previous_art_day(self):
        self.assertEqual(date(2026, 10, 10), local_start('2026-10-11T00:30:00Z').date())
        self.official['startTime'] = self.provider['startTime'] = '2026-10-11T00:30:00Z'
        self.assertFalse(compare(self.official, self.provider, date(2026, 10, 11))['match'])

    def test_libertadores_warning(self):
        self.provider['competitionId'] = 'lg_20B3GRJ'
        self.official['competition'] = 'CONMEBOL Libertadores'
        self.provider['startTime'] = '2026-10-13T20:00:00Z'
        result = compare(self.official, self.provider, date(2026, 10, 11))
        self.assertEqual('DATA_QUALITY_WARNING', result['state'])
        self.assertEqual({}, result['allowedFields'])

    def test_partial_team_rejected(self):
        self.provider['homeTeam'] = 'Club'
        self.assertFalse(compare(self.official, self.provider, date(2026, 10, 11))['match'])

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            local_start('2026-10-11T19:30:00')

    def test_unknown_competition_unvalidated(self):
        self.provider['competitionId'] = 'unknown'
        self.assertEqual('UNVALIDATED', compare(self.official, self.provider, date(2026, 10, 11))['state'])
