"""Offline regressions from the public Argentina TV guides on 17 November 2026."""
import copy
from datetime import date, datetime
import json
from pathlib import Path
import unittest

from scripts import broadcaster_discovery as discovery
from scripts import official_football_bot as bot, prioritized_agendas as guides
from scripts.broadcaster_sources import Evidence, Fixture, Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 10, tzinfo=ZONE)


class ConfirmedGuideSignalsNovember17Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((ROOT / 'tests/fixtures/broadcast_guides_2026_11_17.json').read_text(encoding='utf8'))
        cls.config = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.official_config = json.loads((ROOT / 'sports_official_sources.json').read_text(encoding='utf8'))
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)

    def records(self, home=None):
        records = []
        for source in self.snapshot['sources']:
            provider = next(item for item in guides.PROVIDERS if item['id'] == source['guideId'])
            listings = [Listing(row['homeTeam'], row['awayTeam'], datetime.fromisoformat(row['startTime']),
                                row['competition'], tuple(row['broadcasters'])) for row in source['records']]
            records.extend(guides.checked_records(listings, provider, DAY, self.config))
        if home is None:
            return records
        return [row for row in records if guides.team_key(row['homeTeam'], row['competition'], self.config)
                == guides.team_key(home, row['competition'], self.config)]

    def preview(self, records):
        rows, conflicts = guides.merge_rows([], records, DAY, self.official_config, self.config)
        self.assertEqual([], conflicts)
        feed, daily, details, _ = bot.preview(rows, DAY, NOW, self.official_config, self.groups, self.mapping,
                                             guide_records=records, guide_config=self.config)
        events = [event for section in feed['sections'] for event in section['events']]
        return rows, events, daily, details

    def single(self, home, records=None):
        rows, events, daily, details = self.preview(records if records is not None else self.records(home))
        self.assertEqual(1, len(rows))
        self.assertEqual(1, len(events))
        self.assertEqual(1, len(details))
        return rows[0], events[0], daily, details[0]

    def decision(self, records, extra_evidence=()):
        rows, conflicts = guides.merge_rows([], records, DAY, self.official_config, self.config)
        self.assertEqual([], conflicts)
        self.assertEqual(1, len(rows))
        row = rows[0]
        fixture = Fixture(1, row['homeTeam'], row['awayTeam'], row['competition'],
                          datetime.fromisoformat(row['startTime']), country='Argentina')
        evidence = guides.evidence_for(row, 1, records, self.config) + list(extra_evidence)
        return discovery.confidence(fixture, evidence, self.mapping, self.groups)

    def test_snapshot_merges_exact_multilingual_fixtures_without_duplicates(self):
        rows, events, _, _ = self.preview(self.records())
        self.assertEqual('2026-11-17', self.snapshot['date'])
        self.assertEqual(7, len(rows))
        self.assertEqual(7, len({event['id'] for event in events}))
        self.assertEqual({'2026-11-17'}, {row['startTime'][:10] for row in rows})
        self.assertEqual({'-03:00'}, {row['startTime'][-6:] for row in rows})
        self.assertTrue(all(row['broadcasters'] == [] for row in rows))

    def test_chile_colombia_observed_dsports_alias_resolves_only_real_argentina_stream(self):
        _, event, daily, detail = self.single('Chile')
        self.assertEqual(('Chile', 'Colombia', '20:00'),
                         (event['homeTeam'], event['awayTeam'], event['startTime']))
        self.assertEqual([169947], [channel['streamId'] for channel in event['channels']])
        self.assertEqual(['dsports'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual('CONFIRMED_MBOX', detail['state'])
        self.assertEqual(2, detail['evidenceCount'])

    def test_wales_portugal_retains_two_real_espn_signals_despite_extra_ole_platform(self):
        records = self.records('Gales')
        _, event, daily, detail = self.single('Gales', records)
        self.assertEqual([165649, 169952], [channel['streamId'] for channel in event['channels']])
        self.assertEqual(['espn'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual('CONFIRMED_MBOX', detail['state'])
        self.assertEqual(['espn'], self.decision(records)['channelGroups'])

    def test_denmark_norway_does_not_publish_espn4_with_unverified_catalog_region(self):
        _, event, daily, detail = self.single('Dinamarca')
        self.assertEqual('', self.groups['espn_4']['signalRegion'])
        self.assertEqual('REVIEW', detail['state'])
        self.assertEqual([], detail['channelGroups'])
        self.assertEqual([], event.get('channels', []))
        self.assertEqual([], daily['broadcasts'])

    def test_ecuador_and_uruguay_without_broadcaster_evidence_remain_informational(self):
        for home in ('Ecuador', 'Uruguay'):
            with self.subTest(home=home):
                _, event, daily, detail = self.single(home)
                self.assertEqual('UNRESOLVED', detail['state'])
                self.assertEqual([], detail['evidence'])
                self.assertEqual([], event.get('channels', []))
                self.assertEqual([], daily['broadcasts'])

    def test_bosnia_disney_only_is_external_and_does_not_invent_linear_tv_streams(self):
        _, event, daily, detail = self.single('Bosnia')
        self.assertEqual('CONFIRMED_EXTERNAL', detail['state'])
        self.assertEqual([], event.get('channels', []))
        self.assertEqual([], daily['broadcasts'])

    def test_dgo_only_second_source_is_not_an_independent_confirmation_of_espn(self):
        records = self.records('Gales')
        for record in records:
            record['broadcasters'] = ['ESPN', 'DGO'] if record['guideId'] == 'futbol-en-vivo-ar' else ['DGO']
        _, event, daily, detail = self.single('Gales', records)
        self.assertEqual('CONFIRMED_EXTERNAL', detail['state'])
        self.assertEqual([], event.get('channels', []))
        self.assertEqual([], daily['broadcasts'])
        self.assertEqual([], self.decision(records)['channelGroups'])

    def test_explicit_platform_aliases_never_map_to_mbox_stream_groups(self):
        for name in ('Disney+ Premium', 'Disney + Premium', 'DGO'):
            with self.subTest(name=name):
                self.assertTrue(discovery.is_external_platform(name, 'Argentina', self.mapping))
                self.assertIsNone(discovery.map_signal(name, 'Argentina', self.mapping)[2])

    def test_shared_disney_and_dgo_cannot_hide_espn_versus_espn2_conflict(self):
        records = self.records('Gales')
        for record in records:
            signal = 'ESPN 2' if record['guideId'] == 'ole-agenda' else 'ESPN'
            record['broadcasters'] = [signal, 'Disney+ Premium', 'DGO']
        row, event, daily, detail = self.single('Gales', records)
        self.assertTrue(guides.signal_conflict(row, records, self.config, self.mapping))
        independent = [record for record in records if record['guideId'] != 'tyc-agenda']
        self.assertEqual('REVIEW', self.decision(independent)['state'])
        self.assertEqual('REVIEW', detail['state'])
        self.assertEqual([], event.get('channels', []))
        self.assertEqual([], daily['broadcasts'])

    def test_unknown_tv_name_is_not_assumed_to_be_a_platform(self):
        self.assertFalse(discovery.is_external_platform('Canal Deportivo Desconocido', 'Argentina', self.mapping))
        records = self.records('Gales')
        for record in records:
            signal = 'Canal Deportivo Desconocido' if record['guideId'] == 'ole-agenda' else 'ESPN'
            record['broadcasters'] = [signal, 'Disney+ Premium']
        row, event, _, detail = self.single('Gales', records)
        self.assertTrue(guides.signal_conflict(row, records, self.config, self.mapping))
        independent = [record for record in records if record['guideId'] != 'tyc-agenda']
        self.assertEqual('REVIEW', self.decision(independent)['state'])
        self.assertEqual('REVIEW', detail['state'])
        self.assertEqual([], event.get('channels', []))

    def test_argentina_one_owner_tv_claim_and_dgo_do_not_confirm_telefe_or_tyc(self):
        _, event, daily, detail = self.single('Argentina')
        self.assertEqual('REVIEW', detail['state'])
        self.assertEqual(1, detail['evidenceCount'])
        self.assertEqual([], event.get('channels', []))
        self.assertEqual([], daily['broadcasts'])

    def test_owned_tyc_article_confirms_tyc_only_without_promoting_unconfirmed_telefe(self):
        article = self.snapshot['ownedArticle']
        evidence = [Evidence(1, article['startTime'], signal, 'Argentina', 'tyc-owned-article',
                             'ole-tyc-conservative', True, True, article['url'], 'official_broadcaster')
                    for signal in article['explicitBroadcasters']]
        decision = self.decision(self.records('Argentina'), evidence)
        self.assertEqual('CONFIRMED_MBOX', decision['state'])
        self.assertEqual(['tyc_sports'], decision['channelGroups'])
        self.assertNotIn('Telefe', decision['broadcaster'])

    def test_preview_does_not_mutate_captured_source_claims(self):
        records = self.records()
        original = copy.deepcopy(records)
        self.preview(records)
        self.assertEqual(original, records)


if __name__ == '__main__':
    unittest.main()
