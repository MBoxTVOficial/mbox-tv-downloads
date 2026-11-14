"""Offline official-site snapshots: competition isolation, bounded reads, exact TV evidence."""
import copy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import official_football_bot as bot
from scripts.broadcaster_sources import DiscoveryError, ZONE
from scripts.lpf_expanded_source import article_links, parse_lpf_fixtures
from scripts.lpf_official_provider import LpfOfficialProvider
from scripts.sports_channel_rules import load_clean_groups

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 12)
NOW = datetime(2026, 11, 12, 14, tzinfo=ZONE)
FIRST = 'https://www.ligaprofesional.ar/notas/primera/2026/11/09/agenda-de-la-fecha/'
RESERVE = 'https://www.ligaprofesional.ar/notas/proyeccion/2026/11/09/agenda-de-la-fecha-17-2/'


def config():
    return json.loads((ROOT / bot.REGISTRY).read_text(encoding='utf8'))


def html(body, reserve=False):
    title = 'Torneo Clausura de Proyección 2026' if reserve else 'Torneo Clausura 2026'
    return '<div class="entry-content"><p>' + title + '</p>' + body + '</div>'


def row(home='Club Azul', away='Club Verde', broadcasters=None, start='2026-11-12T19:00:00-03:00'):
    return {'providerFixtureId': 'official-row', 'competition': 'Torneo Proyección',
        'competitionId': 'lpf-proyeccion', 'homeTeam': home, 'awayTeam': away,
        'startTime': start, 'source': 'lpf_official', 'sourceUrl': RESERVE,
        'broadcasters': broadcasters or []}


class ExpandedLpfTests(unittest.TestCase):
    def test_reserve_explicit_schedule_removes_zone_and_stadium_not_team_qualifier(self):
        page = html('<p>Jueves 12 de noviembre<br>15.00 Huracán – Estudiantes (Río Cuarto) (Zona B) – La Quemita<br>19.00 Boca – Newell’s (Zona B) – Boca Predio</p>', True)
        rows = parse_lpf_fixtures(page, 'Torneo Proyección', RESERVE, DAY)
        self.assertEqual(2, len(rows))
        self.assertEqual('Estudiantes (Río Cuarto)', rows[0]['awayTeam'])
        self.assertEqual('Newell’s', rows[1]['awayTeam'])
        self.assertEqual([], rows[0]['broadcasters'])
        self.assertEqual('lpf-proyeccion', rows[0]['competitionId'])

    def test_navigation_lpfplay_is_not_fixture_evidence(self):
        page = '<nav>LPF Play</nav>' + html('<p>Jueves 12 de noviembre<br>15.00 Azul – Verde (Zona A) – Predio</p>', True)
        self.assertEqual([], parse_lpf_fixtures(page, 'Torneo Proyección', RESERVE, DAY)[0]['broadcasters'])

    def test_explicit_tv_only_in_fixture_row(self):
        page = html('<p>Jueves 12 de noviembre<br>15.00 Azul – Verde (Zona A) (LPF Play)<br>19.00 Negro – Blanco (Zona B) – Predio</p>', True)
        rows = parse_lpf_fixtures(page, 'Torneo Proyección', RESERVE, DAY)
        self.assertEqual(['LPF Play'], rows[0]['broadcasters'])
        self.assertEqual([], rows[1]['broadcasters'])

    def test_result_and_arbitrator_not_agenda(self):
        page = html('<p>Jueves 12 de noviembre<br>Azul 1 (Perez) – Verde 0 (Zona A) – Predio<br>15.00 Negro 2 – Blanco 1 (Zona A)<br>Árbitro: persona</p>', True)
        self.assertEqual([], parse_lpf_fixtures(page, 'Torneo Proyección', RESERVE, DAY))

    def test_missing_year_and_foreign_context_fail_closed(self):
        page = html('<p>Jueves 12 de noviembre<br>15.00 Azul – Verde (Zona A)</p>', True)
        self.assertEqual(1, len(parse_lpf_fixtures(page.replace(' 2026', ''), 'Torneo Proyección', RESERVE, DAY)))
        for url in (RESERVE.replace('/2026/', '/2025/'), RESERVE.replace('/proyeccion/', '/primera/'), RESERVE.replace('www.ligaprofesional.ar', 'other.example')):
            with self.subTest(url=url), self.assertRaises(DiscoveryError):
                parse_lpf_fixtures(page.replace(' 2026', ''), 'Torneo Proyección', url, DAY)

    def test_reserve_does_not_enter_primera(self):
        page = html('<p>Jueves 12 de noviembre<br>15.00 Azul – Verde (Zona A)</p>', True)
        with self.assertRaises(DiscoveryError):
            parse_lpf_fixtures(page, 'Liga Profesional Argentina', FIRST, DAY)

    def test_index_cannot_switch_category(self):
        page = '<a href="' + FIRST + '">Agenda</a><a href="' + RESERVE + '">Resultados</a>'
        links = article_links(page, 'Torneo Proyección', 'https://www.ligaprofesional.ar/categoria/proyeccion/', DAY)
        self.assertEqual([RESERVE], links)

    def test_primera_identity_remains_compatible_with_published_provider(self):
        page = html('<p>Jueves 12 de noviembre<br>19.00 Azul – Verde (Zona A) (TNT Sports)</p>')
        network = Mock()
        network.fetch.side_effect = ['<a href="' + FIRST + '">Agenda</a>', page]
        old = LpfOfficialProvider(network).matches('lpf-primera', DAY)[0]
        new = parse_lpf_fixtures(page, 'Liga Profesional Argentina', FIRST, DAY)[0]
        self.assertEqual(old['providerFixtureId'], new['providerFixtureId'])

    def test_reprogramming_keeps_source_identity_but_updates_time(self):
        before = html('<p>Jueves 12 de noviembre<br>15.00 Azul – Verde (Zona A)</p>', True)
        a = parse_lpf_fixtures(before, 'Torneo Proyección', RESERVE, DAY)[0]
        b = parse_lpf_fixtures(before.replace('15.00', '16.00'), 'Torneo Proyección', RESERVE, DAY)[0]
        self.assertEqual(a['providerFixtureId'], b['providerFixtureId'])
        self.assertNotEqual(a['startTime'], b['startTime'])

    def test_wrong_or_missing_kickoff_does_not_invent_clock(self):
        page = html('<p>Jueves 12 de noviembre<br>Azul – Verde (Zona A)<br>25.00 Negro – Blanco (Zona B)</p>', True)
        self.assertEqual([], parse_lpf_fixtures(page, 'Torneo Proyección', RESERVE, DAY))


class OfficialBotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))

    def make_preview(self, rows, day=DAY, daily=None, now=NOW, identity=None):
        return bot.preview(rows, day, now, config(), self.groups, self.mapping, identity, daily)

    def events(self, feed):
        return [event for section in feed['sections'] for event in section['events']]

    def test_without_fixture_tv_no_channels_even_when_group_has_rights(self):
        feed, assignments, details, _ = self.make_preview([row()])
        self.assertEqual('UNRESOLVED', details[0]['state'])
        self.assertFalse(self.events(feed)[0].get('channels'))
        self.assertEqual([], assignments['broadcasts'])

    def test_official_tv_resolves_only_actual_group_primary_and_backups(self):
        feed, daily, details, _ = self.make_preview([row(broadcasters=['TNT Sports'])])
        channels = self.events(feed)[0]['channels']
        self.assertEqual('CONFIRMED_MBOX', details[0]['state'])
        self.assertEqual(['tnt_sports_premium'], daily['broadcasts'][0]['channelGroups'])
        self.assertEqual([165664, 165666, 165665], [c['streamId'] for c in channels])
        self.assertTrue(all(c['enabled'] for c in channels))
        self.assertEqual([1, 2, 3], [c['priority'] for c in channels])

    def test_external_platform_is_known_without_channel(self):
        feed, daily, details, _ = self.make_preview([row(broadcasters=['LPF Play'])])
        self.assertEqual('CONFIRMED_EXTERNAL', details[0]['state'])
        self.assertFalse(self.events(feed)[0].get('channels'))
        self.assertEqual([], daily['broadcasts'])

    def test_tnt_chile_cannot_become_tnt_argentina(self):
        feed, daily, details, _ = self.make_preview([row(broadcasters=['TNT Sports Chile'])])
        self.assertNotEqual('CONFIRMED_MBOX', details[0]['state'])
        self.assertFalse(self.events(feed)[0].get('channels'))

    def test_future_and_past_days_not_in_today_feed(self):
        rows = [row(start='2026-11-13T00:30:00Z'),
                row('Other', 'Teams', start='2026-11-13T13:00:00Z'),
                row('Past', 'Day', start='2026-11-12T00:30:00Z')]
        feed, _, _, _ = self.make_preview(rows)
        self.assertEqual(1, len(self.events(feed)))
        self.assertEqual('21:30', self.events(feed)[0]['startTime'])
        self.assertEqual(DAY.isoformat(), feed['date'])

    def test_auxiliary_fields_ignored_and_no_scores_states_created(self):
        raw = {**row(), 'status': 'FINISHED', 'score': '3-1', 'channels': [{'streamId': 1}], 'venue': 'X'}
        feed, daily, _, _ = self.make_preview([raw])
        event = self.events(feed)[0]
        self.assertEqual('SCHEDULED', event['status'])
        self.assertEqual('DEGRADED', feed['sportsDataMode'])
        self.assertNotIn('homeScore', event)
        self.assertNotIn('awayScore', event)
        self.assertNotIn('channels', event)
        self.assertEqual(1, feed['schemaVersion'])
        bot.generator.validate_feed(feed)

    def test_manual_confirmed_has_priority_and_wrong_day_does_not_apply(self):
        _, auto, _, _ = self.make_preview([row(broadcasters=['TNT Sports'])])
        manual = copy.deepcopy(auto)
        manual['broadcasts'][0].update(source='manual', channelGroups=['espn_premium'])
        feed, daily, _, _ = self.make_preview([row(broadcasters=['TNT Sports'])], daily=manual)
        self.assertEqual([183616, 183615, 183614], [c['streamId'] for c in self.events(feed)[0]['channels']])
        self.assertEqual('manual', daily['broadcasts'][0]['source'])
        manual['date'] = '2026-11-11'
        feed, _, _, _ = self.make_preview([row()], daily=manual)
        self.assertNotIn('channels', self.events(feed)[0])

    def test_source_namespace_prevents_cross_competition_id_transfer(self):
        a = row()
        b = {**row(), 'source': 'conmebol_official', 'sourceUrl': 'https://gol.conmebol.com/libertadores/es/fixture/view/1683',
             'competition': 'CONMEBOL Libertadores', 'competitionId': 'libertadores'}
        feed, _, _, identity = self.make_preview([a, b])
        self.assertEqual(2, len({e['id'] for e in self.events(feed)}))
        repeat, _, _, _ = self.make_preview([a, b], identity=identity)
        self.assertEqual([e['id'] for e in self.events(feed)], [e['id'] for e in self.events(repeat)])

    def test_unknown_competition_rejected(self):
        with self.assertRaises(DiscoveryError):
            self.make_preview([{**row(), 'competitionId': 'CL'}])

    def test_conmebol_generic_tv_is_not_argentina_fixture_confirmation(self):
        raw = {**row(broadcasters=['ESPN']), 'source': 'conmebol_official',
            'sourceUrl': 'https://gol.conmebol.com/libertadores/es/fixture/view/1683',
            'competition': 'CONMEBOL Libertadores', 'competitionId': 'libertadores'}
        feed, _, details, _ = self.make_preview([raw])
        self.assertEqual('UNRESOLVED', details[0]['state'])
        self.assertNotIn('channels', self.events(feed)[0])

    def test_duplicate_streams_deduped_and_maximum_preserved(self):
        feed, _, _, _ = self.make_preview([row(broadcasters=['TNT Sports', 'TNT Sports Premium', 'ESPN Premium'])])
        channels = self.events(feed)[0]['channels']
        self.assertLessEqual(len(channels), 5)
        self.assertEqual(len(channels), len({c['streamId'] for c in channels}))
        self.assertEqual([165664, 183616], [c['streamId'] for c in channels[:2]])

    def test_confirmed_metadata_survives_elapsed_window_without_inventing_final_state(self):
        feed, _, _, _ = self.make_preview([row(broadcasters=['TNT Sports'])], now=NOW.replace(hour=23))
        self.assertEqual([165664, 165666, 165665],
                         [channel['streamId'] for channel in self.events(feed)[0]['channels']])
        self.assertEqual('SCHEDULED', self.events(feed)[0]['status'])

    def test_confirmed_metadata_is_independent_of_refresh_hour(self):
        rows = [row(broadcasters=['TyC Sports'])]
        expected = [165660, 165659]
        for hour in (0, 8, 12, 17, 20, 23):
            with self.subTest(hour=hour):
                feed, assignments, details, _ = self.make_preview(rows, now=NOW.replace(hour=hour))
                self.assertEqual(expected, [channel['streamId'] for channel in self.events(feed)[0]['channels']])
                self.assertEqual(['tyc_sports'], assignments['broadcasts'][0]['channelGroups'])
                self.assertEqual('CONFIRMED_MBOX', details[0]['state'])

    def test_later_fixture_does_not_destroy_earlier_confirmed_metadata(self):
        rows = [row(broadcasters=['TyC Sports']),
                {**row('Other Home', 'Other Away', ['TyC Sports'], '2026-11-12T21:00:00-03:00'),
                 'providerFixtureId': 'later-official-row'}]
        feed, _, _, _ = self.make_preview(rows, now=NOW.replace(hour=22))
        self.assertEqual(2, len(self.events(feed)))
        self.assertTrue(all([channel['streamId'] for channel in event['channels']] == [165660, 165659]
                            for event in self.events(feed)))


class OfficialBotBudgetTests(unittest.TestCase):
    def simulate(self, source, day, pages):
        pages.fetch(source['url'])
        for index in range(source['articles']):
            pages.fetch(source['url'] + 'article-' + str(index))
        return []

    def test_six_read_ceiling_rotation_then_cache_no_requests(self):
        network = Mock()
        network.fetch.return_value = '<html>offline snapshot</html>'
        with patch.object(bot, 'source_rows', side_effect=self.simulate):
            _, cache, report = bot.acquire(config(), DAY, NOW, transport=network)
            self.assertEqual(6, report['publicRequests'])
            self.assertTrue(any(s['state'] == 'DEFERRED_BUDGET' for s in report['sources']))
            _, cache, second = bot.acquire(config(), DAY, NOW + timedelta(minutes=1), cache, network)
            self.assertLessEqual(second['publicRequests'], 6)
            self.assertEqual(5, len(cache['sources']))
            self.assertTrue(all(s['state'] in ('OK', 'CACHE_HIT') for s in second['sources']))
            _, _, third = bot.acquire(config(), DAY, NOW + timedelta(minutes=2), cache, network)
            self.assertEqual(0, third['publicRequests'])
            self.assertEqual(0, third['sportsApiRequests'])

    def test_next_day_cannot_reuse_yesterdays_cache(self):
        with patch.object(bot, 'source_rows', return_value=[]):
            _, cache, _ = bot.acquire(config(), DAY, NOW, transport=Mock())
        network = Mock()
        _, _, report = bot.acquire(config(), DAY + timedelta(days=1), NOW + timedelta(days=1), cache, network, offline=True)
        self.assertTrue(all(source['state'] == 'OFFLINE_MISS' for source in report['sources']))
        network.fetch.assert_not_called()

    def test_new_day_keeps_only_rotation_metadata_not_yesterday_rows(self):
        with patch.object(bot, 'source_rows', return_value=[]):
            _, cache, _ = bot.acquire(config(), DAY, NOW, transport=Mock())
        cache['sources']['lpf-primera']['rows'] = [row()]
        _, updated, _ = bot.acquire(config(), DAY + timedelta(days=1), NOW + timedelta(days=1), cache, offline=True)
        self.assertTrue(updated['sources'])
        self.assertTrue(all(set(entry) == {'attemptedAt'} for entry in updated['sources'].values()))

    def test_timeout_does_not_abort_others_or_dump_exception(self):
        network = Mock()
        network.fetch.side_effect = TimeoutError('secret-sensitive-response')
        _, _, report = bot.acquire(config(), DAY, NOW, transport=network)
        self.assertTrue(all(source['state'] == 'ERROR' for source in report['sources']))
        self.assertEqual(5, network.fetch.call_count)
        self.assertNotIn('secret-sensitive-response', json.dumps(report))

    def test_same_day_error_uses_cached_rows(self):
        def fetch_rows(source, day, pages):
            return [row()] if source['id'] == 'lpf-proyeccion' else []
        with patch.object(bot, 'source_rows', side_effect=fetch_rows):
            _, cache, _ = bot.acquire(config(), DAY, NOW, transport=Mock())
        with patch.object(bot, 'source_rows', side_effect=TimeoutError('private')):
            rows, _, report = bot.acquire(config(), DAY, NOW + timedelta(hours=7), cache, Mock())
        self.assertEqual(1, len(rows))
        self.assertTrue(any(source['state'] == 'ERROR_USING_CACHE' for source in report['sources']))

    def test_url_dedupes_success_and_failure(self):
        network = Mock()
        budget = bot.RequestBudget(network, 6)
        pages = bot.PublicPages(budget)
        pages.fetch('https://www.afa.com.ar/es/')
        pages.fetch('https://www.afa.com.ar/es/')
        self.assertEqual(1, network.fetch.call_count)
        network.fetch.side_effect = TimeoutError()
        for _ in range(2):
            with self.assertRaises((TimeoutError, DiscoveryError)):
                pages.fetch('https://www.afa.com.ar/es/posts/programacion/')
        self.assertEqual(2, network.fetch.call_count)

    def test_malformed_cache_entry_fails_closed_without_network_offline(self):
        _, cache, _ = bot.acquire(config(), DAY, NOW, offline=True)
        cache['sources'] = {'lpf-primera': [], 'lpf-proyeccion': {'attemptedAt': 42}}
        rows, _, report = bot.acquire(config(), DAY, NOW, cache, offline=True)
        self.assertEqual([], rows)
        self.assertTrue(all(source['state'] == 'OFFLINE_MISS' for source in report['sources']))

    def test_conflicting_documents_require_review_and_remove_cached_tv(self):
        def initial(source, day, pages):
            return [row(broadcasters=['TNT Sports'])] if source['id'] == 'lpf-proyeccion' else []
        with patch.object(bot, 'source_rows', side_effect=initial):
            _, cache, _ = bot.acquire(config(), DAY, NOW, transport=Mock())
        def conflict(source, day, pages):
            return [row(broadcasters=['TNT Sports']), row(broadcasters=['ESPN Premium'])] if source['id'] == 'lpf-proyeccion' else []
        with patch.object(bot, 'source_rows', side_effect=conflict):
            rows, updated, report = bot.acquire(config(), DAY, NOW + timedelta(hours=7), cache, Mock())
        self.assertEqual([], rows[0]['broadcasters'])
        self.assertTrue(any(source['state'] == 'REVIEW' for source in report['sources']))
        self.assertEqual([], updated['sources']['lpf-proyeccion']['rows'][0]['broadcasters'])

    def test_conflicting_kickoff_never_merges_silently(self):
        source = next(s for s in config()['sources'] if s['id'] == 'lpf-proyeccion')
        with self.assertRaisesRegex(DiscoveryError, 'OFFICIAL_FIXTURE_CONFLICT'):
            bot.checked_rows([row(), row(start='2026-11-12T20:00:00-03:00')], source, DAY)

    def test_limits_or_api_url_cannot_be_silently_enabled(self):
        for mutation in ('limit', 'api'):
            value = config()
            if mutation == 'limit':
                value['limits']['maxRequests'] = 7
            else:
                value['sources'][0]['url'] = 'https://api.football-data.org/v4/matches'
            with self.assertRaises(ValueError):
                bot.validate_registry(value)

    def test_command_refuses_output_inside_repository_before_requests(self):
        with patch.object(bot, 'acquire') as acquire, self.assertRaises(SystemExit):
            bot.main(['--root', str(ROOT), '--output-dir', str(ROOT / 'preview')])
        acquire.assert_not_called()

    def test_production_entrypoints_do_not_import_prototype(self):
        for relative in ('scripts/generate_agenda_today.py', 'scripts/generate_sports_today.py',
                         '.github/workflows/update-sports-today.yml', '.github/workflows/update-sports-live.yml'):
            self.assertNotIn('official_football_bot', (ROOT / relative).read_text(encoding='utf8'))


if __name__ == '__main__':
    unittest.main()
