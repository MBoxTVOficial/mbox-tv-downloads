"""Real public Olé page contracts; HTML screens are not independent result votes."""
from datetime import date, datetime
import json
from pathlib import Path
import unittest

from scripts.broadcaster_sources import ZONE
from scripts.public_results_ole import (
    AGENDA_URL, OPTA_URL, SOURCE_PAGES, SOURCE_URLS, STATISTICS_URL,
    parse_ole_agenda_results, parse_ole_statistics_results,
)
from scripts.sports_match_updates import MatchUpdatesError, parse_ole_bulk

FIXTURES = Path(__file__).with_name('fixtures')
DAY = date(2026, 11, 15)


class OlePublicResultsTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / name).read_text(encoding='utf8')

    def test_statistics_reuses_actual_ole_bulk_json_without_changing_provenance(self):
        payload = json.loads(self.fixture('ole_match_updates_bulk_2026_11_13.json'))
        day = date(2026, 11, 13)
        expected = parse_ole_bulk(payload, day)
        self.assertTrue(expected)
        self.assertEqual(expected, parse_ole_statistics_results(payload, day))
        self.assertEqual(expected, parse_ole_statistics_results(json.dumps(payload), day))
        self.assertEqual(expected, parse_ole_statistics_results(json.dumps(payload).encode(), day))
        self.assertEqual({'ole-opta': OPTA_URL}, SOURCE_URLS)
        self.assertEqual(AGENDA_URL, SOURCE_PAGES['ole-agenda'])
        self.assertEqual(STATISTICS_URL, SOURCE_PAGES['ole-statistics'])

    def test_actual_statistics_html_shell_never_counts_as_successful_results(self):
        raw = self.fixture('ole_result_statistics_shell_2026_11_15.html')
        with self.assertRaisesRegex(MatchUpdatesError, 'requires_shared_feed'):
            parse_ole_statistics_results(raw, DAY)

    def test_actual_agenda_empty_mams_scores_do_not_infer_live_or_final(self):
        raw = self.fixture('ole_result_agenda_2026_11_15.html')
        for now in (datetime(2026, 11, 15, 9, tzinfo=ZONE),
                    datetime(2026, 11, 15, 23, tzinfo=ZONE)):
            with self.subTest(now=now):
                with self.assertRaisesRegex(MatchUpdatesError, 'not_embedded'):
                    parse_ole_agenda_results(raw, DAY, now)

    def test_unverified_added_status_score_fields_cannot_create_result(self):
        raw = self.fixture('ole_result_agenda_2026_11_15.html')
        for fragment in ('"status": "FINISHED", "homeScore": 4, "awayScore": 1,',
                         '"matchStatus": "Played", "scores": {"home": 4, "away": 1},'):
            changed = raw.replace('"type": "event",', '"type": "event", ' + fragment, 1)
            with self.subTest(fragment=fragment):
                with self.assertRaisesRegex(MatchUpdatesError, 'not_embedded'):
                    parse_ole_agenda_results(changed, DAY)

    def test_wrong_route_canonical_duplicate_script_and_non_json_fail_closed(self):
        raw = self.fixture('ole_result_statistics_shell_2026_11_15.html')
        variants = [raw.replace(STATISTICS_URL, 'https://example.invalid/statistics'),
                    raw.replace('"home.html"', '"other.html"'),
                    raw + raw, '<html>changed layout</html>', b'\xff']
        for value in variants:
            with self.subTest(value=str(value)[:70]):
                with self.assertRaises(MatchUpdatesError):
                    parse_ole_statistics_results(value, DAY)

    def test_invalid_dates_or_oversize_payload_are_rejected(self):
        for value in (None, '2026-11-15', datetime(2026, 11, 15, tzinfo=ZONE)):
            with self.subTest(value=value):
                with self.assertRaises(MatchUpdatesError):
                    parse_ole_statistics_results({}, value)
        with self.assertRaises(MatchUpdatesError):
            parse_ole_statistics_results(' ' * 2_000_001, DAY)

    def test_captured_catalog_retains_ambiguous_lpf_labels_without_enabling_them(self):
        meta = json.loads(self.fixture('ole_result_pages_metadata_2026_11_15.json'))
        self.assertEqual(OPTA_URL, meta['sharedResultEndpoint'])
        self.assertEqual('ole-opta', meta['sharedResultProvider'])
        lpf = [row for row in meta['catalog'] if row['id'] == '581t4mywybx21wcpmpykhyzr3']
        self.assertEqual({'Liga Profesional', 'Copa de la Liga'}, {row['name'] for row in lpf})
        self.assertEqual({'Argentina'}, {row['country'] for row in lpf})
        serie_a = next(row for row in meta['catalog'] if row['id'] == '1r097lpxe0xn03ihb7wi98kao')
        self.assertEqual(('Serie A', 'Italy'), (serie_a['name'], serie_a['country']))


if __name__ == '__main__':
    unittest.main()
