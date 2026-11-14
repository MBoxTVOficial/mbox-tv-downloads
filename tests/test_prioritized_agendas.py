"""Offline Argentina guide order, exact identity and official-authority regressions."""
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import prioritized_agendas as guides, official_football_bot as bot
from scripts.broadcaster_sources import DiscoveryError, Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 14, tzinfo=ZONE)


def listing(home='Club Azul', away='Club Verde', signal='TNT Sports', hour=19, competition='Reserva'):
    return Listing(home, away, NOW.replace(hour=hour), competition, (signal,) if signal else ())


class GuidePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.official_config = json.loads((ROOT / 'sports_official_sources.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))

    def records(self, index=0, item=None):
        return guides.checked_records([item or listing()], guides.PROVIDERS[index], DAY, self.config)

    def official(self, signals=None):
        return {'competition': 'Torneo Proyección', 'competitionId': 'lpf-proyeccion',
                'source': 'lpf_official', 'providerFixtureId': 'official-row', 'homeTeam': 'Club Azul',
                'awayTeam': 'Club Verde', 'startTime': NOW.replace(hour=19).isoformat(),
                'sourceUrl': 'https://www.ligaprofesional.ar/notas/proyeccion/2026/11/09/agenda/',
                'broadcasters': signals or []}

    def feed(self, records, official=None):
        rows, conflicts = guides.merge_rows(official or [], records, DAY, self.official_config, self.config)
        feed, daily, details, _ = bot.preview(rows, DAY, NOW, self.official_config, self.groups, self.mapping,
                                             guide_records=records, guide_config=self.config)
        events = [event for section in feed['sections'] for event in section['events']]
        return events, daily, details, conflicts

    def acquire(self, responses, official=None, cache=None, offline=False, network=None, now=NOW):
        network = network or Mock()
        pages = bot.PublicPages(bot.RequestBudget(network, 6))
        with patch.object(guides, 'parse_page', side_effect=lambda adapter, html, day: responses.get(adapter, [])):
            records, cache, report = guides.acquire_guides(self.config, DAY, now, pages, official or [],
                self.official_config, self.groups, self.mapping, cache, offline)
        return records, cache, report, network

    def test_exact_provider_order_is_configuration_validated(self):
        self.assertEqual(['futbol-en-vivo-ar', 'ole-agenda', 'tyc-agenda'], [p['id'] for p in guides.validate_guides(self.config)['providers']])
        bad = copy.deepcopy(self.config)
        bad['providers'].reverse()
        with self.assertRaises(DiscoveryError):
            guides.validate_guides(bad)

    def test_first_empty_falls_to_ole_then_tyc_if_confirmation_missing(self):
        records, _, report, network = self.acquire({'ole': [listing()]})
        self.assertEqual(1, len(records))
        self.assertEqual([p['url'] for p in guides.PROVIDERS], [call.args[0] for call in network.fetch.call_args_list])
        self.assertEqual('NO_MATCHES', report[0]['state'])

    def test_first_timeout_falls_back_without_sensitive_error(self):
        network = Mock()
        network.fetch.side_effect = [TimeoutError('sensitive-response'), 'page', 'page']
        records, _, report, _ = self.acquire({'ole': [listing()]}, network=network)
        self.assertEqual('ERROR', report[0]['state'])
        self.assertEqual('ole-agenda', records[0]['guideId'])
        self.assertNotIn('sensitive-response', json.dumps(report))

    def test_first_and_second_confirm_skip_unneeded_third(self):
        _, _, report, network = self.acquire({'argentina_tv': [listing()], 'ole': [listing()]})
        self.assertEqual(2, network.fetch.call_count)
        self.assertEqual('SKIP_RESOLVED', report[2]['state'])

    def test_official_fixture_confirmation_can_stop_after_first_guide(self):
        _, _, report, network = self.acquire({'argentina_tv': [listing()]}, [self.official(['TNT Sports'])])
        self.assertEqual(1, network.fetch.call_count)
        self.assertEqual(['OK', 'SKIP_RESOLVED', 'SKIP_RESOLVED'], [r['state'] for r in report])

    def test_one_guide_does_not_autoassign(self):
        events, daily, details, _ = self.feed(self.records())
        self.assertNotIn('channels', events[0])
        self.assertEqual([], daily['broadcasts'])
        self.assertEqual('REVIEW', details[0]['state'])
        self.assertEqual('EDITORIAL_AGENDA', details[0]['sourceConfidence'])

    def test_two_independent_matching_guides_confirm_group_with_real_backups(self):
        events, daily, details, _ = self.feed(self.records() + self.records(1))
        self.assertEqual([165664, 165666, 165665], [c['streamId'] for c in events[0]['channels']])
        self.assertEqual(['tnt_sports_premium'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual('CONFIRMED_MBOX', details[0]['state'])
        self.assertEqual(1, len(events))

    def test_ole_and_tyc_do_not_count_as_two_independent_guide_votes(self):
        events, _, details, _ = self.feed(self.records(1, listing(signal='ESPN')) + self.records(2, listing(signal='ESPN')))
        self.assertNotIn('channels', events[0])
        self.assertEqual('REVIEW', details[0]['state'])

    def test_tyc_own_signal_is_official_but_not_espn_or_tnt(self):
        events, _, details, _ = self.feed(self.records(2, listing(signal='TyC Sports')))
        self.assertTrue(events[0]['channels'])
        self.assertEqual('CONFIRMED_MBOX', details[0]['state'])
        events, _, details, _ = self.feed(self.records(2, listing(signal='ESPN')))
        self.assertNotIn('channels', events[0])

    def test_conflicting_signals_require_review(self):
        events, _, details, _ = self.feed(self.records() + self.records(1, listing(signal='ESPN Premium')))
        self.assertNotIn('channels', events[0])
        self.assertEqual('REVIEW', details[0]['state'])

    def test_shared_owner_does_not_hide_one_of_its_sources_conflicting_signal(self):
        records = self.records() + self.records(1, listing(signal='ESPN Premium')) + self.records(2)
        events, _, details, _ = self.feed(records)
        self.assertNotIn('channels', events[0])
        self.assertEqual('REVIEW', details[0]['state'])

    def test_official_fixture_tv_overrides_priority_guide_claim(self):
        records = self.records(0, listing(signal='ESPN Premium')) + self.records(1, listing(signal='ESPN Premium'))
        events, daily, _, _ = self.feed(records, [self.official(['TNT Sports'])])
        self.assertEqual(['tnt_sports_premium'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual(165664, events[0]['channels'][0]['streamId'])

    def test_official_kickoff_is_not_overwritten_or_duplicated(self):
        records = self.records(0, listing(hour=20)) + self.records(1, listing(hour=20))
        events, _, _, conflicts = self.feed(records, [self.official()])
        self.assertEqual(1, len(events))
        self.assertEqual('19:00', events[0]['startTime'])
        self.assertNotIn('channels', events[0])
        self.assertTrue(all(c['reason'] == 'KICKOFF_CONFLICT' for c in conflicts))

    def test_reversed_teams_keep_only_official_identity(self):
        events, _, _, conflicts = self.feed(self.records(item=listing('Club Verde', 'Club Azul')), [self.official()])
        self.assertEqual(1, len(events))
        self.assertEqual('Club Azul', events[0]['homeTeam'])
        self.assertEqual('TEAM_IDENTITY_REVIEW', conflicts[0]['reason'])

    def test_wrong_guide_time_without_official_is_review_not_silent_merge(self):
        records = self.records() + self.records(1, listing(hour=20)) + self.records(2)
        events, _, details, conflicts = self.feed(records)
        self.assertEqual(1, len(events))
        self.assertEqual('19:00', events[0]['startTime'])
        self.assertEqual('REVIEW', details[0]['state'])
        self.assertNotIn('channels', events[0])
        self.assertTrue(conflicts)

    def test_unsupported_female_competition_not_mens_fixture(self):
        self.assertEqual([], self.records(item=listing(competition='Campeonato Femenino')))
        self.assertEqual([], self.records(item=listing(competition='Champions League Femenina')))
        self.assertIsNone(guides.canonical_competition('UEFA Champions League', self.config))

    def copa_records(self, source_indexes=(0, 1), signals=True):
        # Sanitized rows parsed from both public guides on 14 November 2026.
        snapshot = json.loads((ROOT / 'tests/fixtures/copa_argentina_guides_2026_11_14.json').read_text(encoding='utf8'))
        day = date.fromisoformat(snapshot['date'])
        records = []
        for index in source_indexes:
            source = snapshot['sources'][index]
            provider = next(provider for provider in guides.PROVIDERS if provider['id'] == source['guideId'])
            listings = [Listing(row['homeTeam'], row['awayTeam'], datetime.fromisoformat(row['startTime']),
                                row['competition'], tuple(row['broadcasters']) if signals else ())
                        for row in source['records']]
            records.extend(guides.checked_records(listings, provider, day, self.config))
        return day, records

    def november_14_feed(self, records):
        day = date(2026, 11, 14)
        now = datetime(2026, 11, 14, 13, tzinfo=ZONE)
        rows, conflicts = guides.merge_rows([], records, day, self.official_config, self.config)
        feed, daily, details, _ = bot.preview(rows, day, now, self.official_config, self.groups, self.mapping,
                                             guide_records=records, guide_config=self.config)
        events = [event for section in feed['sections'] for event in section['events']]
        return rows, feed, events, daily, details, conflicts

    def test_copa_argentina_retains_real_banfield_boca_fixture_and_explicit_tv(self):
        day, records = self.copa_records()
        self.assertEqual(2, len(records))
        self.assertEqual({'2026-11-14T19:00:00-03:00'}, {row['startTime'] for row in records})
        self.assertEqual({'Copa Argentina'}, {row['competition'] for row in records})
        rows, feed, events, daily, details, conflicts = self.november_14_feed(records)
        self.assertEqual([], conflicts)
        self.assertEqual(1, len(rows))
        self.assertEqual('agenda-copa-argentina', rows[0]['competitionId'])
        self.assertEqual([], rows[0]['broadcasters'])  # Guide evidence stays separate from fixture authority.
        self.assertEqual(day.isoformat(), feed['date'])
        self.assertEqual(1, len(events))
        self.assertEqual(('Banfield', 'Boca Juniors', '19:00'),
                         (events[0]['homeTeam'], events[0]['awayTeam'], events[0]['startTime']))
        self.assertEqual([events[0]], next(section['events'] for section in feed['sections'] if section['id'] == 'argentina'))
        self.assertEqual(['tyc_sports'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual('CONFIRMED_MBOX', details[0]['state'])

    def test_copa_one_guide_and_unconfirmed_extra_platform_do_not_assign_channels(self):
        _, records = self.copa_records((0,))
        _, _, events, daily, details, _ = self.november_14_feed(records)
        self.assertNotIn('channels', events[0])
        self.assertEqual([], daily['broadcasts'])
        self.assertEqual('REVIEW', details[0]['state'])

    def test_copa_without_explicit_tv_retains_fixture_without_inventing_channels(self):
        _, records = self.copa_records(signals=False)
        _, _, events, daily, details, _ = self.november_14_feed(records)
        self.assertEqual(1, len(events))
        self.assertNotIn('channels', events[0])
        self.assertEqual([], daily['broadcasts'])
        self.assertEqual('UNRESOLVED', details[0]['state'])

    def test_copa_scope_does_not_enable_other_cups_or_categories(self):
        self.assertIs(self.config, guides.validate_guides(self.config))
        self.assertEqual('Copa Argentina', guides.canonical_competition('Argentina - Copa Argentina', self.config))
        for competition in ('Copa Argentina Femenina', 'Copa Argentina Reserva', 'Supercopa Argentina', 'Copa de la Liga'):
            with self.subTest(competition=competition):
                self.assertEqual([], self.records(item=listing(competition=competition)))
        bad = copy.deepcopy(self.config)
        bad['competitionAliases']['Supercopa Argentina'] = []
        with self.assertRaisesRegex(DiscoveryError, 'ARGENTINA_GUIDE_SCOPE_INVALID'):
            guides.validate_guides(bad)

    def test_copa_team_aliases_are_exact_and_reserve_aliases_stay_scoped(self):
        self.assertEqual(guides.team_key('Boca', 'Copa Argentina', self.config),
                         guides.team_key('Boca Juniors', 'Copa Argentina', self.config))
        self.assertNotEqual(guides.team_key('Boca Juniors Reserva', 'Copa Argentina', self.config),
                            guides.team_key('Boca Juniors', 'Copa Argentina', self.config))
        self.assertNotEqual(guides.team_key('Gimnasia', 'Copa Argentina', self.config),
                            guides.team_key('Gimnasia LP', 'Copa Argentina', self.config))
        self.assertNotEqual(guides.team_key('Boca', 'CONMEBOL Libertadores', self.config),
                            guides.team_key('Boca Juniors', 'CONMEBOL Libertadores', self.config))

    def test_primera_nacional_exact_club_aliases_merge_explicit_lpf_play_evidence(self):
        # Exact team labels and clocks from the saved 14 November FEV and Olé pages.
        matches = [(('CA Atlanta', 'Estudiantes BA'), ('Atlanta', 'Estudiantes Caseros'), 15),
                   (('CA Colón', 'Midland'), ('Colón Santa Fe', 'Ferrocarril Midland'), 18)]
        for first, second, hour in matches:
            with self.subTest(home=first[0]):
                records = []
                start = datetime(2026, 11, 14, hour, tzinfo=ZONE)
                for provider, pair in zip(guides.PROVIDERS, (first, second)):
                    item = Listing(*pair, start, 'Primera Nacional', ('LPF Play',))
                    records.extend(guides.checked_records([item], provider, start.date(), self.config))
                rows, _, events, daily, details, conflicts = self.november_14_feed(records)
                self.assertEqual([], conflicts)
                self.assertEqual(1, len(rows))
                self.assertEqual(1, len(events))
                self.assertEqual(start.strftime('%H:%M'), events[0]['startTime'])
                self.assertNotIn('channels', events[0])
                self.assertEqual([], daily['broadcasts'])
                self.assertEqual('CONFIRMED_EXTERNAL', details[0]['state'])
                self.assertEqual(['LPF Play'], details[0]['broadcaster'])
                self.assertEqual(2, details[0]['evidenceCount'])

    def test_known_argentina_club_aliases_do_not_generalize_to_other_contexts(self):
        for alias, canonical in [('CA Atlanta', 'Atlanta'), ('Estudiantes BA', 'Estudiantes Caseros'),
                                 ('Colón Santa Fe', 'Colón'), ('Ferrocarril Midland', 'Midland')]:
            with self.subTest(alias=alias):
                self.assertEqual(guides.team_key(alias, 'Primera Nacional', self.config),
                                 guides.team_key(canonical, 'Primera Nacional', self.config))
                self.assertNotEqual(guides.team_key(alias, 'CONMEBOL Libertadores', self.config),
                                    guides.team_key(canonical, 'CONMEBOL Libertadores', self.config))
        self.assertNotEqual(guides.team_key('Estudiantes', 'Primera Nacional', self.config),
                            guides.team_key('Estudiantes Caseros', 'Primera Nacional', self.config))

    def test_existing_four_european_agendas_remain_supported_without_sports_api(self):
        for source_name, canonical, section in [('LALIGA EA SPORTS', 'LaLiga', 'spain'),
                                               ('Premier League', 'Premier League', 'england'),
                                               ('Serie A Italia', 'Serie A', 'italy'),
                                               ('Ligue 1', 'Ligue 1', 'france')]:
            with self.subTest(competition=source_name):
                records = self.records(item=listing(competition=source_name, signal=''))
                events, _, details, _ = self.feed(records)
                self.assertEqual(canonical, events[0]['competition'])
                self.assertEqual('EDITORIAL_AGENDA', details[0]['sourceConfidence'])
                self.assertNotIn('channels', events[0])

    def test_unqualified_serie_a_cannot_cross_brazil_and_italy(self):
        self.assertEqual([], self.records(item=listing(competition='Série A')))
        self.assertEqual([], self.records(item=listing(competition='Serie A')))
        self.assertEqual('Serie A', guides.canonical_competition('Serie A Italia', self.config))

    def test_reserve_suffix_alias_stays_within_reserve_competition(self):
        self.assertEqual(guides.team_key('Boca Juniors Reserva', 'Torneo Proyección', self.config),
                         guides.team_key('Boca', 'Torneo Proyección', self.config))
        self.assertNotEqual(guides.team_key('Boca Juniors Reserva', 'Liga Profesional Argentina', self.config),
                            guides.team_key('Boca', 'Liga Profesional Argentina', self.config))
        self.assertNotEqual(guides.team_key('Gimnasia', 'Torneo Proyección', self.config),
                            guides.team_key('Gimnasia (Mza.)', 'Torneo Proyección', self.config))

    def test_no_partial_names_or_opponent_orientation_matching(self):
        self.assertNotEqual(guides.team_key('River', 'CONMEBOL Libertadores', self.config),
                            guides.team_key('River Plate', 'CONMEBOL Libertadores', self.config))
        self.assertNotEqual(guides.team_key('Estudiantes', 'Torneo Proyección', self.config),
                            guides.team_key('Estudiantes RC', 'Torneo Proyección', self.config))

    def test_external_platform_never_becomes_mbox_channel(self):
        records = self.records(0, listing(signal='LPF Play (plan plus)')) + self.records(1, listing(signal='LPF PLAY'))
        events, daily, details, _ = self.feed(records)
        self.assertEqual('CONFIRMED_EXTERNAL', details[0]['state'])
        self.assertNotIn('channels', events[0])
        self.assertEqual([], daily['broadcasts'])

    def test_new_day_does_not_reuse_guide_cache(self):
        _, cache, _, _ = self.acquire({'argentina_tv': [listing()], 'ole': [listing()]})
        records, _, report, network = self.acquire({}, cache=cache, now=NOW, offline=True)
        self.assertTrue(records)
        network.fetch.assert_not_called()
        cache['date'] = '2026-11-11'
        records, _, report, network = self.acquire({}, cache=cache, offline=True)
        self.assertEqual([], records)
        network.fetch.assert_not_called()

    def test_cache_prevents_duplicate_guide_requests(self):
        _, cache, _, _ = self.acquire({'argentina_tv': [listing()], 'ole': [listing()]})
        records, _, report, network = self.acquire({}, cache=cache)
        self.assertEqual(2, len(records))
        self.assertEqual(['CACHE_HIT', 'CACHE_HIT', 'SKIP_RESOLVED'], [r['state'] for r in report])
        network.fetch.assert_not_called()

    def test_shared_budget_six_for_guides_and_all_official_sources(self):
        network = Mock()
        network.fetch.return_value = 'page'
        def official_source(source, day, pages):
            pages.fetch(source['url'])
            for index in range(source['articles']):
                pages.fetch(source['url'] + 'article-' + str(index))
            return []
        with patch.object(guides, 'parse_page', return_value=[]), patch.object(bot, 'source_rows', side_effect=official_source):
            _, _, _, _, report = guides.acquire_all(self.official_config, self.config, DAY, NOW,
                                                    self.groups, self.mapping, transport=network)
        self.assertEqual(6, network.fetch.call_count)
        self.assertEqual([p['url'] for p in guides.PROVIDERS], [call.args[0] for call in network.fetch.call_args_list[:3]])
        self.assertEqual(6, report['publicRequests'])
        self.assertEqual(0, report['sportsApiRequests'])
        self.assertTrue(any(source['state'] == 'DEFERRED_BUDGET' for source in report['sources']))

    def test_all_guides_fail_official_agenda_is_still_used(self):
        network = Mock()
        network.fetch.side_effect = TimeoutError()
        with patch.object(bot, 'source_rows', side_effect=lambda source, day, pages: [self.official()] if source['id'] == 'lpf-proyeccion' else []):
            rows, _, _, _, report = guides.acquire_all(self.official_config, self.config, DAY, NOW,
                                                     self.groups, self.mapping, transport=network)
        self.assertEqual('lpf_official', rows[0]['source'])
        self.assertEqual(3, report['publicRequests'])
        self.assertTrue(all(source['state'] == 'ERROR' for source in report['guides']))


if __name__ == '__main__':
    unittest.main()
