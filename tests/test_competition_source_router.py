import copy
import unittest
from datetime import date, datetime
from scripts import competition_source_router as router
from scripts import generate_sports_today as generator
from scripts.broadcaster_sources import ZONE

DAY = date(2026, 10, 11)
NOW = datetime(2026, 10, 11, 12, tzinfo=ZONE)


def row(**changes):
    value = dict(competition='Liga Profesional Argentina', competitionId='lg_22EEYKR',
                 providerFixtureId='mt_test', homeTeam='Club Azul', awayTeam='Club Verde',
                 startTime='2026-10-11T19:30:00-03:00')
    value.update(changes)
    return value


class RouterTests(unittest.TestCase):
    def test_exact_official_match(self):
        rows, conflicts = router.route(DAY, [row()], [row(fixtureId=901)])
        self.assertEqual(901, rows[0]['fixtureId'])
        self.assertEqual('mt_test', rows[0]['providerFixtureId'])
        self.assertEqual('official', rows[0]['source'])
        self.assertEqual([], conflicts)

    def test_previous_art_day(self):
        self.assertEqual([], router.route(DAY, [row(startTime='2026-10-11T00:30:00Z')])[0])

    def test_missing_not_invented(self):
        self.assertEqual(1, len(router.route(DAY, [row()])[0]))
        self.assertEqual([], router.route(DAY)[0])

    def test_official_time_wins(self):
        rows, conflicts = router.route(DAY, [row(startTime='2026-10-11T19:31:00-03:00')], [row()])
        self.assertEqual('2026-10-11T19:30:00-03:00', rows[0]['startTime'])
        self.assertIsNone(rows[0]['providerFixtureId'])
        self.assertEqual('KICKOFF_CONFLICT', conflicts[0]['reason'])

    def test_secondary_id_and_forbidden_fields(self):
        rows, _ = router.route(DAY, [row(score=99, status='LIVE', venue='unknown', channels=[123], broadcaster='hint')])
        self.assertEqual('mt_test', rows[0]['providerFixtureId'])
        for field in ('score', 'status', 'venue', 'channels', 'broadcaster'):
            self.assertNotIn(field, rows[0])

    def test_lpf_uses_argentina_provider(self):
        self.assertEqual('football_soccer_argentina', router.route(DAY, [row()])[0][0]['source'])

    def test_laliga_unvalidated_blocked(self):
        from unittest.mock import patch
        with patch.dict(router.POLICIES, {'LaLiga': ('PD', 'UNVALIDATED', 'football_data')}):
            self.assertEqual([], router.route(DAY, [row(competition='LaLiga', competitionId='PD')])[0])

    def test_laliga_validated_route_and_art(self):
        from unittest.mock import patch
        with patch.dict(router.POLICIES, {'LaLiga': ('PD', 'LIMITED_AUXILIARY', 'football_data')}):
            rows, _ = router.route(DAY, [row(competition='LaLiga', competitionId='PD', startTime='2026-10-11T22:30:00Z')])
            self.assertEqual('football_data', rows[0]['source'])
            self.assertEqual('2026-10-11T19:30:00-03:00', rows[0]['startTime'])

    def test_libertadores_no_fallback(self):
        self.assertEqual([], router.route(DAY, [row(competition='CONMEBOL Libertadores')])[0])

    def test_primary_unchanged_except_mode(self):
        feed, _ = generator.build_feed({'errors': [], 'response': [], 'results': 0}, DAY, NOW)
        original = copy.deepcopy(feed)
        actual, _ = router.generate_with_fallback(DAY, NOW, lambda: feed)
        self.assertEqual('NORMAL', actual.pop('sportsDataMode'))
        self.assertEqual(original, actual)

    def test_suspended_degraded_no_scores_channels_live(self):
        def unavailable():
            raise router.PrimaryUnavailable('suspended')
        feed, _ = router.generate_with_fallback(DAY, NOW, unavailable, [row(status='LIVE', score=1)])
        self.assertEqual('DEGRADED', feed['sportsDataMode'])
        events = [e for s in feed['sections'] for e in s['events']]
        self.assertEqual('SCHEDULED', events[0]['status'])
        self.assertNotIn('homeScore', events[0])
        self.assertNotIn('channels', events[0])

    def test_unknown_competition_empty(self):
        self.assertEqual([], router.route(DAY, [row(competition='Premier League')])[0])

    def test_programming_error_not_swallowed(self):
        def broken():
            raise ValueError('bug')
        with self.assertRaises(ValueError):
            router.generate_with_fallback(DAY, NOW, broken)

    def test_past_unknown_status_not_invented(self):
        rows, _ = router.route(DAY, [row(startTime='2026-10-11T10:00:00-03:00')])
        feed = router.degraded_feed(DAY, NOW, rows)
        self.assertFalse(any(s['events'] for s in feed['sections']))

    def test_identity_stable(self):
        self.assertEqual(router.route(DAY, [row()])[0], router.route(DAY, [row()])[0])

    def test_optional_mode_feed_valid(self):
        feed = router.degraded_feed(DAY, NOW, router.route(DAY, [row()])[0])
        generator.validate_feed(feed)
        self.assertEqual(1, feed['schemaVersion'])
        feed['sportsDataMode'] = 'INVALID'
        with self.assertRaises(generator.GenerationError):
            generator.validate_feed(feed)

    def test_snapshot_discards_status_score(self):
        rows = router.football_soccer_snapshot([{'league_id': 'lg_22EEYKR', 'match_id': 'mt_mock',
            'home_team_name': 'Club Azul', 'away_team_name': 'Club Verde',
            'kickoff_utc': '2026-10-11T22:30:00Z', 'match_status': 'finished', 'score': 9}])
        self.assertEqual('2026-10-11T19:30:00-03:00', rows[0]['startTime'])
        self.assertNotIn('score', rows[0])
        self.assertNotIn('match_status', rows[0])

    def test_laliga_official_conflict_when_validated(self):
        from unittest.mock import patch
        with patch.dict(router.POLICIES, {'LaLiga': ('PD', 'LIMITED_AUXILIARY', 'football_data')}):
            official = row(competition='LaLiga', competitionId='PD')
            provider = dict(official, startTime='2026-10-11T20:00:00-03:00')
            rows, conflicts = router.route(DAY, [provider], [official])
            self.assertEqual('official', rows[0]['source'])
            self.assertEqual(1, len(conflicts))

    def test_discovery_receives_only_safe_feed(self):
        seen = []
        def discovery(feed):
            seen.append(copy.deepcopy(feed))
            return feed
        feed = router.degraded_feed(DAY, NOW, router.route(DAY, [row()])[0], discovery)
        self.assertEqual(1, len(seen))
        self.assertNotIn('sportsDataMode', seen[0])
        self.assertNotIn('channels', next(e for s in feed['sections'] for e in s['events']))

    def test_budget_and_live_guard_unchanged(self):
        self.assertEqual(90, generator.MAX_DAILY_API_CALLS)
        from pathlib import Path
        source = Path(generator.__file__).read_text()
        self.assertIn('if live_only and discover_broadcasts:', source)
