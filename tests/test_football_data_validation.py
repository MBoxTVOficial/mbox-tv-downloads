"""Recorded official comparison; no network or credentials."""
import json
from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo
from scripts.competition_source_router import POLICIES


class RecordedValidationTests(unittest.TestCase):
    def test_twenty_kickoffs_match_after_art_conversion(self):
        report = json.loads((Path(__file__).parent / 'data/football_data_validation.json').read_text(encoding='utf8'))
        count = 0
        for code in ('PD', 'PL', 'SA', 'FL1'):
            self.assertEqual('LIMITED_AUXILIARY', report['competitions'][code]['state'])
            for row in report['competitions'][code]['matches']:
                official = datetime.fromisoformat(row['officialLocal']).replace(tzinfo=ZoneInfo(row['officialTimezone']))
                provider = datetime.fromisoformat(row['providerUTC'].replace('Z', '+00:00'))
                self.assertEqual(official.astimezone(ZoneInfo('America/Argentina/Buenos_Aires')),
                                 provider.astimezone(ZoneInfo('America/Argentina/Buenos_Aires')))
                count += 1
        self.assertEqual(20, count)

    def test_validated_routes(self):
        for competition in ('LaLiga', 'Premier League', 'Serie A', 'Ligue 1'):
            self.assertEqual('LIMITED_AUXILIARY', POLICIES[competition][1])
            self.assertEqual('football_data', POLICIES[competition][2])

    def test_champions_stays_blocked(self):
        self.assertEqual('UNVALIDATED', POLICIES['UEFA Champions League'][1])
