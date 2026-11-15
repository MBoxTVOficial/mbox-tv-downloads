"""One automatic public agenda/result refresh every two ART hours, without duplicate jobs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unittest

from scripts.broadcaster_sources import ZONE
from scripts import generate_sports_today as generator


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'


def active_crons(text):
    return re.findall(r"(?m)^    - cron: '([^']+)'\s*$", text)


class TwoHourSportsScheduleTests(unittest.TestCase):
    def setUp(self):
        self.general = (WORKFLOWS / 'update-sports-today.yml').read_text(encoding='utf8')
        self.states = (WORKFLOWS / 'update-sports-match-updates.yml').read_text(encoding='utf8')
        self.live = (WORKFLOWS / 'update-sports-live.yml').read_text(encoding='utf8')

    def test_general_cron_covers_exactly_twelve_even_art_hours(self):
        self.assertEqual(['0 1-23/2 * * *'], active_crons(self.general))
        utc_hours = set(range(1, 24, 2))
        for month in (1, 7, 11):
            with self.subTest(month=month):
                art_times = [datetime(2026, month, 14, hour, tzinfo=ZONE)
                             for hour in range(0, 24, 2)]
                self.assertEqual(12, len(art_times))
                self.assertTrue(all(value.astimezone(timezone.utc).hour in utc_hours
                                    and value.astimezone(timezone.utc).minute == 0
                                    for value in art_times))

    def test_general_runs_agenda_then_one_grouped_result_refresh(self):
        agenda = 'run: python -m scripts.generate_public_agenda'
        results = 'run: python -m scripts.generate_sports_match_updates'
        self.assertEqual(1, self.general.count(agenda))
        self.assertEqual(1, self.general.count(results))
        self.assertLess(self.general.index(agenda), self.general.index(results))
        self.assertIn('continue-on-error: true', self.general)
        self.assertIn('last-good states and agenda are retained', self.general)

    def test_separate_match_workflow_keeps_manual_dispatch_without_automatic_schedule(self):
        self.assertEqual([], active_crons(self.states))
        self.assertNotIn('\n  schedule:', self.states)
        self.assertIn('workflow_dispatch:', self.states)
        self.assertIn("#   - cron: '*/5 * * * *'", self.states)

    def test_old_api_football_live_remains_paused(self):
        self.assertEqual([], active_crons(self.live))
        self.assertNotIn('\n  schedule:', self.live)
        self.assertIn('workflow_dispatch:', self.live)
        self.assertEqual(90, generator.MAX_DAILY_API_CALLS)

    def test_general_never_binds_sports_account_keys(self):
        for name in ('API_FOOTBALL_KEY', 'FOOTBALL_DATA_TOKEN', 'FOOTBALL_SOCCER_API_KEY'):
            self.assertNotIn('secrets.' + name, self.general)

    def test_public_budget_per_run_remains_six_with_twelve_daily_windows(self):
        config = json.loads((ROOT / 'sports_official_sources.json').read_text(encoding='utf8'))
        self.assertEqual(6, config['limits']['maxRequests'])
        self.assertEqual(72, len(range(1, 24, 2)) * config['limits']['maxRequests'])


if __name__ == '__main__':
    unittest.main()
