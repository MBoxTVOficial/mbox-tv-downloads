"""Exact, context-scoped aliases from the three captured 19/20 November guides."""
import copy
from datetime import date, datetime
import json
from pathlib import Path
import unittest

from scripts import official_football_bot as bot, prioritized_agendas as guides
from scripts.broadcaster_sources import Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups
from scripts.sports_team_logos import fixture_logos


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 20)


class AgendaAliasesNovember20Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((ROOT / 'tests/fixtures/agenda_guides_2026_11_20.json').read_text(encoding='utf8'))
        cls.config = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.official = json.loads((ROOT / 'sports_official_sources.json').read_text(encoding='utf8'))
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)

    def records(self, competition=None):
        result = []
        for source in self.snapshot['sources']:
            provider = next(item for item in guides.PROVIDERS if item['id'] == source['guideId'])
            listings = [Listing(row['homeTeam'], row['awayTeam'], datetime.fromisoformat(row['startTime']),
                                row['competition'], tuple(row['broadcasters']), row['homeLogo'], row['awayLogo'])
                        for row in source['records']]
            result.extend(guides.checked_records(listings, provider, DAY, self.config))
        return [row for row in result if competition is None or row['competition'] == competition]

    def official_row(self, home, away, competition, start):
        return {'source': 'lpf_official', 'providerFixtureId': 'offline-regression-'+start[:10],
                'competitionId': 'lpf-primera' if competition == 'Liga Profesional Argentina' else 'lpf-proyeccion',
                'competition': competition, 'homeTeam': home, 'awayTeam': away, 'startTime': start,
                'sourceUrl': 'https://www.ligaprofesional.ar/categoria/primera/', 'broadcasters': []}

    def test_observed_aliases_enable_only_already_supported_competitions(self):
        expected = {'Torneo Betano': 'Liga Profesional Argentina',
                    'Liga Profesional de Fútbol': 'Liga Profesional Argentina',
                    'La Liga EA Sports': 'LaLiga', 'España - Primera División': 'LaLiga',
                    'Francia - Primera División': 'Ligue 1'}
        for label, canonical in expected.items():
            with self.subTest(label=label):
                self.assertEqual(canonical, guides.canonical_competition(label, self.config))
        self.assertIs(self.config, guides.validate_guides(self.config))
        self.assertEqual('/competicion/liga-argentina', self.snapshot['competitionContext']['Torneo Betano'])

    def test_other_leagues_and_youth_are_not_enabled_by_aliases(self):
        for label in ('Mundial Sub 17', 'FIFA Mundial Sub-17', 'Alemania - Bundesliga',
                      'Chile - Primera División', 'MLS', 'Premier League Ucrania', 'Serie B Italiana'):
            with self.subTest(label=label):
                self.assertIsNone(guides.canonical_competition(label, self.config))
        row = self.snapshot['november19']['oleArgentinaU17']
        listing = Listing(row['home'], row['away'], datetime.fromisoformat(row['kickoff']),
                          row['competition'], tuple(row['broadcasters']))
        self.assertEqual([], guides.checked_records([listing], guides.PROVIDERS[1], date(2026, 11, 19), self.config))

    def test_athletic_alias_is_exact_and_only_laliga(self):
        self.assertEqual(guides.team_key('Athletic Club', 'LaLiga', self.config),
                         guides.team_key('Athletic Bilbao', 'LaLiga', self.config))
        for competition in ('Liga Profesional Argentina', 'CONMEBOL Libertadores', 'Copa Argentina'):
            with self.subTest(competition=competition):
                self.assertNotEqual(guides.team_key('Athletic Club', competition, self.config),
                                    guides.team_key('Athletic Bilbao', competition, self.config))
        for other in ('Athletic', 'Bilbao', 'Athletic Club B', 'Athletic Bilbao Femenino'):
            self.assertNotEqual(guides.team_key('Athletic Club', 'LaLiga', self.config),
                                guides.team_key(other, 'LaLiga', self.config))

    def test_observed_future_laliga_names_keep_old_canonical_pairs_with_strict_scope(self):
        observed = [('Deportivo', 'Deportivo de A Coruña'), ('Valencia CF', 'Valencia'),
                    ('Sevilla FC', 'Sevilla'), ('Betis', 'Real Betis'), ('Celta', 'Celta de Vigo'),
                    ('FC Barcelona', 'Barcelona'), ('Racing Santander', 'Racing de Santander'),
                    ('At. Madrid', 'Atlético de Madrid')]
        for alias, previous in observed:
            with self.subTest(alias=alias):
                self.assertEqual(guides.team_key(alias, 'LaLiga', self.config),
                                 guides.team_key(previous, 'LaLiga', self.config))
                for competition in ('Liga Profesional Argentina', 'CONMEBOL Libertadores', 'Ligue 1'):
                    self.assertNotEqual(guides.team_key(alias, competition, self.config),
                                        guides.team_key(previous, competition, self.config))
                self.assertNotEqual(guides.team_key(alias + ' B', 'LaLiga', self.config),
                                    guides.team_key(previous, 'LaLiga', self.config))

    def test_newly_recognized_guides_preserve_all_eight_published_future_laliga_ids(self):
        for snapshot in self.snapshot['futureLaLiga']:
            day = date.fromisoformat(snapshot['date'])
            records = []
            for source in snapshot['sources']:
                provider = next(item for item in guides.PROVIDERS if item['id'] == source['guideId'])
                listings = [Listing(row['homeTeam'], row['awayTeam'], datetime.fromisoformat(row['startTime']),
                                    row['competition'], tuple(row['broadcasters']), row['homeLogo'], row['awayLogo'])
                            for row in source['records']]
                records.extend(guides.checked_records(listings, provider, day, self.config))
            rows, conflicts = guides.merge_rows([], records, day, self.official, self.config)
            self.assertEqual([], conflicts)
            self.assertEqual(4, len(rows))
            self.assertEqual(4, len({guides.exact_key(row, self.config) for row in rows}))
            self.assertTrue(all(not guides.guide_conflict(row, records, self.config) for row in rows))
            identity = copy.deepcopy(self.snapshot['priorFutureIdentity'])
            feed, _, _, updated = bot.preview(rows, day, datetime(2026, 11, 20, 23, tzinfo=ZONE),
                self.official, self.groups, self.mapping, guide_records=records, guide_config=self.config,
                identity=identity)
            events = [event for section in feed['sections'] for event in section['events']]
            self.assertEqual({event['id'] for event in snapshot['previousFeedRows']},
                             {event['id'] for event in events})
            self.assertEqual(self.snapshot['priorFutureIdentity'], updated)
            for previous in snapshot['previousFeedRows']:
                matches = [event for event in events if guides.pair_key(event, self.config)
                           == guides.pair_key(previous, self.config) and event['startTime'] == previous['startTime']]
                self.assertEqual(1, len(matches), previous)
                self.assertEqual(previous['id'], matches[0]['id'])
            sevilla = [event for event in events if guides.team_key(event['homeTeam'], 'LaLiga', self.config) == 'sevilla']
            if day == date(2026, 11, 21):
                self.assertEqual(1, len(sevilla))

    def test_velez_alias_reconciles_the_official_seed_only_in_argentina_context(self):
        for competition in ('Liga Profesional Argentina', 'Copa Argentina', 'Torneo Proyección'):
            with self.subTest(competition=competition):
                self.assertEqual(guides.team_key('Vélez (3A)', competition, self.config),
                                 guides.team_key('Vélez Sarsfield', competition, self.config))
        self.assertNotEqual(guides.team_key('Vélez', 'CONMEBOL Libertadores', self.config),
                            guides.team_key('Vélez Sarsfield', 'CONMEBOL Libertadores', self.config))
        self.assertNotEqual(guides.team_key('Vélez', 'Liga Profesional Argentina', self.config),
                            guides.team_key('Vélez Sarsfield Reserva', 'Liga Profesional Argentina', self.config))

    def test_actual_guides_merge_one_fixture_per_match_without_losing_official_identity(self):
        official = self.official_row('Vélez (3A)', 'Belgrano (6B)', 'Liga Profesional Argentina',
                                     '2026-11-20T20:00:00-03:00')
        rows, conflicts = guides.merge_rows([official], self.records(), DAY, self.official, self.config)
        self.assertEqual([], conflicts)
        self.assertEqual(3, len(rows))
        self.assertEqual({'Liga Profesional Argentina', 'LaLiga', 'Ligue 1'}, {row['competition'] for row in rows})
        self.assertEqual(official, next(row for row in rows if row['competition'] == official['competition']))

    def test_source_badges_resolve_velez_and_belgrano_at_the_exact_official_fixture(self):
        official = self.official_row('Vélez (3A)', 'Belgrano (6B)', 'Liga Profesional Argentina',
                                     '2026-11-20T20:00:00-03:00')
        badges = fixture_logos(official, self.records(), self.config)
        source = self.snapshot['sources'][0]['records']
        captured = next(row for row in source if row['competition'] == 'Torneo Betano')
        self.assertEqual({'homeLogo': captured['homeLogo'], 'awayLogo': captured['awayLogo']}, badges)
        self.assertTrue(all(badges.values()))
        wrong_time = {**official, 'startTime': '2026-11-20T19:00:00-03:00'}
        self.assertEqual({'homeLogo': '', 'awayLogo': ''}, fixture_logos(wrong_time, self.records(), self.config))

    def test_laliga_card_has_source_badges_but_never_forces_unverified_dsports2(self):
        records = self.records('LaLiga')
        rows, conflicts = guides.merge_rows([], records, DAY, self.official, self.config)
        self.assertEqual([], conflicts)
        self.assertEqual(1, len(rows))
        self.assertEqual('17:00', datetime.fromisoformat(rows[0]['startTime']).strftime('%H:%M'))
        self.assertFalse(guides.guide_conflict(rows[0], records, self.config))
        feed, daily, details, _ = bot.preview(rows, DAY, datetime(2026, 11, 20, 18, tzinfo=ZONE),
            self.official, self.groups, self.mapping, guide_records=records, guide_config=self.config)
        events = [event for section in feed['sections'] for event in section['events']]
        self.assertEqual(1, len(events))
        self.assertTrue(events[0]['homeLogo'] and events[0]['awayLogo'])
        self.assertEqual([], events[0].get('channels', []))
        self.assertEqual([], daily['broadcasts'])
        self.assertEqual('REVIEW', details[0]['state'])
        self.assertFalse(self.groups['dsports_2'].get('signalRegion'))

    def test_reserve_racing_and_talleres_aliases_remove_the_november19_duplicate(self):
        day = date(2026, 11, 19)
        rows = self.snapshot['november19']['racingRows']
        self.assertEqual(2, len(rows))
        first, second = rows
        official = self.official_row(first['homeTeam'], first['awayTeam'], first['competition'],
                                    '2026-11-19T15:00:00-03:00')
        listing = Listing(second['homeTeam'], second['awayTeam'], datetime(2026, 11, 19, 15, tzinfo=ZONE),
                          second['competition'], ('LPF Play',))
        records = guides.checked_records([listing], guides.PROVIDERS[0], day, self.config)
        merged, conflicts = guides.merge_rows([official], records, day, self.official, self.config)
        self.assertEqual([], conflicts)
        self.assertEqual([official], merged)
        self.assertFalse(guides.guide_conflict(official, records, self.config))
        for alias, canonical in (('Racing Avellaneda Reserva', 'Racing'), ('Talleres Córdoba Reserva', 'Talleres')):
            self.assertEqual(guides.team_key(alias, 'Torneo Proyección', self.config),
                             guides.team_key(canonical, 'Torneo Proyección', self.config))
            self.assertNotEqual(guides.team_key(alias, 'Liga Profesional Argentina', self.config),
                                guides.team_key(canonical, 'Liga Profesional Argentina', self.config))


if __name__ == '__main__':
    unittest.main()
