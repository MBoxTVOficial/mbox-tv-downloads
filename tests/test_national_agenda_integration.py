"""Senior internationals use the same agenda/evidence/groups pipeline, offline."""
from datetime import date, datetime
import json
from pathlib import Path
import unittest

from scripts import prioritized_agendas as guides, official_football_bot as bot
from scripts.broadcaster_sources import Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 12)


class NationalAgendaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.official = json.loads((ROOT / 'sports_official_sources.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))

    def records(self, index, home, away, competition, signals=(), day=DAY, hour=16, minute=45):
        listing = Listing(home, away, datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE), competition, signals)
        return guides.checked_records([listing], guides.PROVIDERS[index], day, self.config)

    def preview(self, records, day=DAY):
        rows, conflicts = guides.merge_rows([], records, day, self.official, self.config)
        feed, broadcasts, details, identity = bot.preview(rows, day, datetime(day.year, day.month, day.day, tzinfo=ZONE),
            self.official, self.groups, self.mapping, guide_records=records, guide_config=self.config)
        return feed, broadcasts, details

    def test_nations_fixture_aliases_reconcile_without_duplicate_or_manual_assignment(self):
        records = self.records(0, 'República Checa', 'España', 'UEFA Nations League', ('ESPN 3',))
        records += self.records(1, 'Chequia', 'España', 'Nations League', ('ESPN 3',))
        feed, daily, details = self.preview(records)
        section = next(s for s in feed['sections'] if s['id'] == 'uefa_nations')
        self.assertEqual(1, len(section['events']))
        self.assertEqual('CONFIRMED_MBOX', details[0]['state'])
        self.assertEqual(['espn_3'], daily['broadcasts'][0]['channelGroups'])
        self.assertTrue(section['events'][0]['channels'])
        self.assertNotIn('homeScore', section['events'][0])

    def test_uefa_and_concacaf_are_separate_senior_competitions(self):
        self.assertFalse(self.records(0, 'Canadá', 'México', 'UEFA Nations League'))
        self.assertFalse(self.records(1, 'Canadá', 'México', 'Nations League'))
        records = self.records(0, 'Canadá', 'México', 'CONCACAF Nations League')
        feed, _, _ = self.preview(records)
        self.assertEqual(1, len(next(s for s in feed['sections'] if s['id'] == 'international')['events']))
        self.assertFalse(next(s for s in feed['sections'] if s['id'] == 'uefa_nations')['events'])

    def test_argentina_friendly_gets_south_america_card_and_real_tyc_backups(self):
        records = self.records(0, 'Argentina', 'Uruguay', 'Amistoso', ('TyC Sports',))
        records += self.records(1, 'ARGENTINA', 'URUGUAY', 'Amistoso Internacional', ('TyC Sports',))
        feed, daily, _ = self.preview(records)
        events = next(s for s in feed['sections'] if s['id'] == 'south_america_national_teams')['events']
        self.assertEqual(1, len(events))
        self.assertEqual(['tyc_sports'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual([165660, 165659], [c['streamId'] for c in events[0]['channels']])

    def test_other_senior_friendlies_use_international_section(self):
        records = self.records(0, 'Estados Unidos', 'Japón', 'Amistosos')
        feed, _, _ = self.preview(records)
        self.assertEqual(1, len(next(s for s in feed['sections'] if s['id'] == 'international')['events']))

    def test_club_friendly_and_regional_side_cannot_be_senior_national_cards(self):
        for home, away in [('River Plate', 'Gimnasia La Plata'), ('País Vasco', 'Venezuela'),
                           ('Argentina Sub-20', 'Brasil Sub-20'), ('Argentina Femenino', 'Uruguay Femenino')]:
            with self.subTest(home=home):
                self.assertFalse(self.records(0, home, away, 'Amistoso'))

    def test_youth_and_womens_competition_labels_never_map_to_mens_nations(self):
        for name in ('Amistoso Sub-21', 'UEFA Nations League Femenina', 'Europeo Sub-19', 'National League'):
            self.assertFalse(self.records(0, 'Inglaterra', 'España', name))

    def test_conflicting_espn_variants_stay_review_even_with_shared_disney_platform(self):
        records = self.records(0, 'Turquía', 'Bélgica', 'UEFA Nations League', ('ESPN', 'Disney+'))
        records += self.records(1, 'Turquía', 'Bélgica', 'Nations League', ('ESPN 2', 'Disney+'))
        feed, broadcasts, details = self.preview(records)
        event = next(s for s in feed['sections'] if s['id'] == 'uefa_nations')['events'][0]
        self.assertEqual('REVIEW', details[0]['state'])
        self.assertFalse(event.get('channels'))
        self.assertFalse(broadcasts['broadcasts'])

    def test_next_day_only_contains_next_days_fixtures(self):
        tomorrow = date(2026, 11, 13)
        records = self.records(0, 'Argentina', 'Uruguay', 'Amistoso', ('TyC Sports',), day=tomorrow)
        records += self.records(1, 'Argentina', 'Uruguay', 'Amistoso Internacional', ('TyC Sports',), day=tomorrow)
        feed, _, _ = self.preview(records, day=DAY)
        self.assertFalse(any(s['events'] for s in feed['sections']))
        future, _, _ = self.preview(records, day=tomorrow)
        self.assertEqual(1, sum(len(s['events']) for s in future['sections']))
        self.assertEqual(tomorrow.isoformat(), future['date'])


if __name__ == '__main__':
    unittest.main()
