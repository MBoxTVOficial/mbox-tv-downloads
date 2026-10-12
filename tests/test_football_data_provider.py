import unittest
from datetime import date
from unittest.mock import Mock, patch
from scripts.football_data_provider import FootballDataProvider, ProviderError, normalize_matches, COMPETITIONS
from scripts import competition_source_router as router

DAY = date(2026, 10, 11)


def payload(code='PD', kickoff='2026-10-11T22:00:00Z'):
    return {'matches': [{'id': 501, 'competition': {'code': code}, 'utcDate': kickoff,
                        'homeTeam': {'name': 'Club Azul'}, 'awayTeam': {'name': 'Club Verde'},
                        'status': 'FINISHED', 'score': {'winner': 'HOME_TEAM'}, 'venue': 'ignored'}]}


class FootballDataTests(unittest.TestCase):
    def test_team_mapping_and_id(self):
        rows = normalize_matches(payload(), 'PD', DAY)
        self.assertEqual('Club Azul', rows[0]['homeTeam'])
        self.assertEqual('football-data:501', rows[0]['providerFixtureId'])
        self.assertNotIn('score', rows[0])
        self.assertNotIn('status', rows[0])
        self.assertNotIn('venue', rows[0])

    def test_art_time(self):
        self.assertEqual('2026-10-11T19:00:00-03:00', normalize_matches(payload(), 'PD', DAY)[0]['startTime'])

    def test_previous_art_day_filtered(self):
        self.assertEqual([], normalize_matches(payload(kickoff='2026-10-11T00:30:00Z'), 'PD', DAY))

    def test_invalid_competition(self):
        with self.assertRaises(ProviderError):
            normalize_matches(payload(), 'UNKNOWN', DAY)

    def test_response_competition_mismatch(self):
        with self.assertRaises(ProviderError):
            normalize_matches(payload('PL'), 'PD', DAY)

    def test_unavailable_safe(self):
        client = FootballDataProvider('mock', fetch=Mock(side_effect=ProviderError('HTTP_403')))
        with self.assertRaisesRegex(ProviderError, 'HTTP_403'):
            client.matches('PD', DAY)
        self.assertEqual({}, client.cache)

    def test_failure_redacted(self):
        client = FootballDataProvider('mock', fetch=Mock(side_effect=RuntimeError('sensitive content')))
        with self.assertRaisesRegex(ProviderError, '^REQUEST_FAILED$'):
            client.matches('PD', DAY)

    def test_daily_cache(self):
        fetch = Mock(return_value=(payload(), {}))
        client = FootballDataProvider('mock', fetch=fetch, sleep=lambda _: None)
        client.matches('PD', DAY)
        client.matches('PD', DAY)
        self.assertEqual(1, fetch.call_count)

    def test_budget_separate(self):
        client = FootballDataProvider('mock', fetch=Mock(return_value=({'competitions': []}, {})), sleep=lambda _: None)
        for _ in range(6):
            client.competitions()
        with self.assertRaisesRegex(ProviderError, 'REQUEST_BUDGET_EXHAUSTED'):
            client.competitions()
        self.assertEqual(6, client.requests)

    def test_header_zero_stops(self):
        client = FootballDataProvider('mock', fetch=Mock(return_value=({'competitions': []}, {'X-Requests-Available-Minute': '0'})))
        client.competitions()
        with self.assertRaises(ProviderError):
            client.competitions()

    def test_each_european_route_blocked_until_validated(self):
        for code, competition in COMPETITIONS.items():
            with self.subTest(code=code):
                rows = normalize_matches(payload(code), code, DAY)
                with patch.dict(router.POLICIES, {competition: (code, 'UNVALIDATED', 'football_data')}):
                    self.assertEqual([], router.route(DAY, rows)[0])
                with patch.dict(router.POLICIES, {competition: (code, 'LIMITED_AUXILIARY', 'football_data')}):
                    normalized, _ = router.route(DAY, rows)
                    self.assertEqual('football_data', normalized[0]['source'])

    def test_official_conflict_wins(self):
        rows = normalize_matches(payload(), 'PD', DAY)
        official = dict(rows[0], startTime='2026-10-11T20:00:00-03:00')
        with patch.dict(router.POLICIES, {'LaLiga': ('PD', 'LIMITED_AUXILIARY', 'football_data')}):
            normalized, conflicts = router.route(DAY, rows, [official])
            self.assertEqual('official', normalized[0]['source'])
            self.assertEqual('KICKOFF_CONFLICT', conflicts[0]['reason'])

    def test_duplicate_id_consolidated(self):
        data = payload()
        data['matches'] *= 2
        self.assertEqual(1, len(normalize_matches(data, 'PD', DAY)))

    def test_libertadores_rejected(self):
        with self.assertRaises(ProviderError):
            normalize_matches(payload(), 'CLI', DAY)
