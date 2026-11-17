"""The optional owned-TV supplement must not displace official acquisition."""
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import official_football_bot as bot, prioritized_agendas as guides
from scripts.argentina_tv_sources import TYC_AGENDA_URL
from scripts.broadcaster_sources import Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 14, tzinfo=ZONE)
ARTICLE_URL = ('https://www.tycsports.com/amistosos-de-selecciones/'
               'amistosos-internacionales-masc-2026-argentina-vs-costa-rica-amistosos-id771718.html')


class TycFixtureTvAcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.official_config = json.loads((ROOT / 'sports_official_sources.json').read_text(encoding='utf8'))
        cls.guide_config = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)
        snapshot = json.loads((ROOT / 'tests/fixtures/broadcast_guides_2026_11_17.json').read_text(encoding='utf8'))
        cls.sources = {provider['adapter']: next(source for source in snapshot['sources']
                       if source['guideId'] == provider['id']) for provider in guides.PROVIDERS}
        cls.article = (ROOT / 'tests/fixtures/tyc_fixture_tv_argentina_costa_rica_2026_11_17.html').read_text(encoding='utf8')
        cls.agenda = (ROOT / 'tests/fixtures/tyc_agenda_fixture_link_2026_11_17.html').read_text(encoding='utf8')

    def parse_guide(self, adapter, html, target):
        return [Listing(row['homeTeam'], row['awayTeam'], datetime.fromisoformat(row['startTime']),
                        row['competition'], tuple(row['broadcasters'])) for row in self.sources[adapter]['records']]

    def official_source(self, source, target, pages):
        pages.fetch(source['url'])
        if source['id'] == 'lpf-primera':
            # Offline stand-ins for the two linked articles allowed by the existing registry.
            for suffix in ('one', 'two'):
                pages.fetch('https://www.ligaprofesional.ar/notas/primera/2026/11/17/offline-proof-' + suffix + '/')
        return []

    def acquire(self, **kwargs):
        network = Mock()
        network.fetch.side_effect = lambda url: self.article if url == ARTICLE_URL else self.agenda if url == TYC_AGENDA_URL else 'offline-page'
        with patch.object(guides, 'parse_page', side_effect=self.parse_guide), \
                patch.object(bot, 'source_rows', side_effect=self.official_source):
            result = guides.acquire_all(self.official_config, self.guide_config, DAY, NOW,
                                        self.groups, self.mapping, transport=network, window_days=3, **kwargs)
        return result, network

    def fresh_official_cache(self):
        fingerprint = hashlib.sha256(json.dumps(self.official_config, sort_keys=True).encode()).hexdigest()
        return {'schemaVersion': 1, 'date': DAY.isoformat(), 'registryHash': fingerprint,
                'sources': {source['id']: {'fetchedAt': NOW.isoformat(), 'attemptedAt': NOW.isoformat(), 'rows': []}
                            for source in self.official_config['sources']}}

    def proof_guide_cache(self):
        provider = guides.PROVIDERS[1]
        source = self.sources[provider['adapter']]
        record = next(row for row in source['records'] if row['homeTeam'] == 'ARGENTINA')
        fingerprint = hashlib.sha256(json.dumps(self.guide_config, sort_keys=True).encode()).hexdigest()
        return {'schemaVersion': 1, 'date': DAY.isoformat(), 'configHash': fingerprint, 'sources': {},
                'fixtureTv': {'schemaVersion': 1, 'date': DAY.isoformat(), 'entry': {
                    'fixtureKey': list(guides.exact_key(record, self.guide_config)), 'sourceUrl': ARTICLE_URL,
                    'fetchedAt': (NOW - timedelta(hours=1)).isoformat(),
                    'articleHtml': self.article, 'agendaHtml': self.agenda}}}

    def assert_current_day_only_supplement(self, report):
        for target, records in report['windowRecords'].items():
            self.assertTrue(all(record['startTime'][:10] == target for record in records))
            if target != DAY.isoformat():
                self.assertFalse(any('ownedTvUrl' in record for record in records))

    def test_three_guides_and_three_primera_requests_keep_official_slots_and_defer_article(self):
        result, network = self.acquire()
        report = result[-1]
        primera = next(source for source in report['sources'] if source['source'] == 'lpf-primera')
        self.assertEqual(('OK', 3), (primera['state'], primera['requests']))
        self.assertEqual('DEFERRED_BUDGET', report['fixtureTv']['state'])
        self.assertEqual((0, 0), (report['fixtureTv']['requests'], report['fixtureTv']['articleRequests']))
        self.assertEqual(6, report['publicRequests'])
        urls = [call.args[0] for call in network.fetch.call_args_list]
        self.assertEqual(6, len(urls))
        self.assertEqual(6, len(set(urls)))
        self.assertNotIn(ARTICLE_URL, urls)
        self.assertFalse(any('ownedTvUrl' in record for record in result[3]))
        self.assert_current_day_only_supplement(report)

    def test_fresh_official_cache_leaves_budget_for_one_exact_owned_article(self):
        result, network = self.acquire(official_cache=self.fresh_official_cache())
        report = result[-1]
        self.assertTrue(all(source['state'] == 'CACHE_HIT' and source['requests'] == 0 for source in report['sources']))
        self.assertEqual(('OK', 1), (report['fixtureTv']['state'], report['fixtureTv']['articleRequests']))
        self.assertEqual(4, report['publicRequests'])
        urls = [call.args[0] for call in network.fetch.call_args_list]
        self.assertEqual(1, urls.count(ARTICLE_URL))
        self.assertEqual(1, urls.count(TYC_AGENDA_URL))
        supplements = [record for record in result[3] if 'ownedTvUrl' in record]
        self.assertEqual(1, len(supplements))
        self.assertEqual(['TyC Sports'], supplements[0]['broadcasters'])
        self.assert_current_day_only_supplement(report)

    def test_fresh_raw_proof_still_revalidates_at_exhausted_budget_without_extending_ttl(self):
        previous = self.proof_guide_cache()
        result, network = self.acquire(guide_cache=previous)
        report = result[-1]
        primera = next(source for source in report['sources'] if source['source'] == 'lpf-primera')
        self.assertEqual(('OK', 3), (primera['state'], primera['requests']))
        self.assertEqual(6, report['publicRequests'])
        self.assertEqual(('CACHE_HIT', 0, 0), (report['fixtureTv']['state'], report['fixtureTv']['requests'],
                                              report['fixtureTv']['articleRequests']))
        self.assertNotIn(ARTICLE_URL, [call.args[0] for call in network.fetch.call_args_list])
        self.assertEqual(previous['fixtureTv']['entry']['fetchedAt'], result[2]['fixtureTv']['entry']['fetchedAt'])
        supplements = [record for record in result[3] if 'ownedTvUrl' in record]
        self.assertEqual(1, len(supplements))
        self.assertEqual(['TyC Sports'], supplements[0]['broadcasters'])
        self.assert_current_day_only_supplement(report)


if __name__ == '__main__':
    unittest.main()
