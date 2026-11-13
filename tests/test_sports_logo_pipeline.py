"""Team images survive the agenda/cache boundary without becoming TV evidence."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import official_football_bot as bot, prioritized_agendas as guides
from scripts import sports_agenda_window as window
from scripts.broadcaster_sources import Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 12, tzinfo=ZONE)
HOME = 'https://static.futbolenlatv.com/img/32/20121219051310_boca-juniors.webp'
AWAY = 'https://static.futbolenlatv.com/img/32/20121219051705_river-plate.webp'


class LogoPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT/'sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.official = json.loads((ROOT/'sports_official_sources.json').read_text(encoding='utf8'))
        cls.mapping = json.loads((ROOT/'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT/'mbox_sports_channels_clean.json', bot.generator.normalize)

    def records(self, day=DAY, home_logo=HOME):
        kickoff = NOW.replace(year=day.year, month=day.month, day=day.day, hour=19)
        listing = Listing('Club Azul', 'Club Verde', kickoff, 'Reserva', (), home_logo, AWAY)
        return guides.checked_records([listing], guides.PROVIDERS[0], day, self.config)

    def feed(self, day, records):
        rows, _ = guides.merge_rows([], records, day, self.official, self.config)
        feed, daily, details, identity = bot.preview(rows, day, NOW, self.official, self.groups,
            self.mapping, guide_records=records, guide_config=self.config)
        return feed, daily, details, identity, rows

    def test_safe_logos_survive_record_and_daily_cache_roundtrip(self):
        records = self.records()
        cached = guides.cached_records(json.loads(json.dumps(records)), guides.PROVIDERS[0], DAY, self.config)
        self.assertEqual(records, cached)
        self.assertEqual(HOME, cached[0]['homeLogo'])
        self.assertEqual(AWAY, cached[0]['awayLogo'])

    def test_images_reach_cards_without_creating_channels_or_scores(self):
        feed, daily, details, _, _ = self.feed(DAY, self.records())
        event = next(e for s in feed['sections'] for e in s['events'])
        self.assertEqual((HOME, AWAY), (event['homeLogo'], event['awayLogo']))
        self.assertFalse(event.get('channels'))
        self.assertNotIn('homeScore', event)
        self.assertNotIn('awayScore', event)
        self.assertEqual([], daily['broadcasts'])
        self.assertEqual('UNRESOLVED', details[0]['state'])

    def test_old_cache_without_logos_remains_compatible(self):
        records = [{k: v for k, v in record.items() if k not in ('homeLogo', 'awayLogo')}
                   for record in self.records()]
        cached = guides.cached_records(records, guides.PROVIDERS[0], DAY, self.config)
        self.assertEqual(records, cached)
        feed, _, _, _, _ = self.feed(DAY, cached)
        event = next(e for s in feed['sections'] for e in s['events'])
        self.assertEqual(('', ''), (event['homeLogo'], event['awayLogo']))

    def test_cache_cannot_inject_private_or_foreign_image_url(self):
        records = self.records()
        records[0].update(homeLogo='https://127.0.0.1/private.png', awayLogo='https://unrelated.example/logo.png')
        cached = guides.cached_records(records, guides.PROVIDERS[0], DAY, self.config)
        self.assertNotIn('homeLogo', cached[0])
        self.assertNotIn('awayLogo', cached[0])

    def test_future_window_preserves_exact_date_images(self):
        today, _, _, identity, _ = self.feed(DAY, self.records())
        tomorrow = DAY + timedelta(days=1)
        records = self.records(tomorrow)
        rows, _ = guides.merge_rows([], records, tomorrow, self.official, self.config)
        report = {'windowRows': {tomorrow.isoformat(): rows},
                  'windowRecords': {tomorrow.isoformat(): records}, 'sources': []}
        bundle, _, _ = window.build_agenda_window(DAY, NOW, today, self.official,
            self.config, self.groups, self.mapping, identity, report)
        event = next(e for s in bundle['days'][tomorrow.isoformat()]['sections'] for e in s['events'])
        self.assertEqual((HOME, AWAY), (event['homeLogo'], event['awayLogo']))
        self.assertFalse(event.get('channels'))
        self.assertEqual(NOW.isoformat(), bundle['days'][tomorrow.isoformat()]['updatedAt'])

    def test_adding_images_does_not_add_network_requests(self):
        pages = bot.PublicPages(bot.RequestBudget(type('Transport', (), {
            'fetch': lambda self, url: 'captured public page'})(), 6))
        listing = Listing('Club Azul', 'Club Verde', NOW.replace(hour=19), 'Reserva', (), HOME, AWAY)
        with patch.object(guides, 'parse_page', return_value=[listing]):
            records, _, _ = guides.acquire_guides(self.config, DAY, NOW, pages, [], self.official,
                self.groups, self.mapping)
        self.assertEqual(3, pages.budget.requests)
        self.assertEqual(HOME, records[0]['homeLogo'])


if __name__ == '__main__':
    unittest.main()
