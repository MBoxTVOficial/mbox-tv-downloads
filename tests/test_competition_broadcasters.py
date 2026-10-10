"""Offline fixture confirmation: historical cases never become production rules."""
import ast
import contextlib
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import broadcaster_discovery as discovery
from scripts import broadcaster_sources as sources
from scripts import sports_competition_broadcasters as registry_module
from scripts.sports_channel_rules import load_clean_groups
from scripts.sports_daily_broadcasts import resolve_broadcasts, TIMEZONE
from scripts import generate_sports_today as generator
from test_generate_sports_today import fixture as api_fixture, response

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 15, tzinfo=sources.ZONE)
INDEX = 'https://www.ligaprofesional.ar/categoria/primera/'
ARTICLE = 'https://www.ligaprofesional.ar/notas/primera/2026/10/07/agenda-de-las-proximas-fechas/'


def article(body, year=2026):
    return f'<div class="elementor-widget-theme-post-content"><p>Clausura {year}</p>{body}</div>'


def agenda(home='Club Azul', away='Club Verde', identifier=7001, day=DAY, status='SCHEDULED'):
    return {'schemaVersion': 1, 'date': str(day), 'timezone': TIMEZONE, 'sections': [
        {'id': 'argentina', 'events': [{'id': f'fixture-{identifier}', 'homeTeam': home,
         'awayTeam': away, 'competition': 'Liga Profesional Argentina', 'startTime': '19:30', 'status': status}]}]}


class CompetitionBroadcastersTest(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads((ROOT / 'sports_competition_broadcasters.json').read_text(encoding='utf8'))
        self.settings = json.loads((ROOT / 'sports_broadcaster_sources.json').read_text(encoding='utf8'))
        self.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        self.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', sources.norm)
        self.feed = agenda()
        self.fixture = discovery.fixtures_from_feed(self.feed)[0]
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('Offline tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def provider(self):
        config = next(p for p in self.settings['providers'] if p['id'] == 'lpf-official')
        provider = sources.BroadcasterProvider(config, {})
        provider.bind_fixtures([self.fixture])
        return provider

    def evidence(self, signal='ESPN Premium', owner='organisation', official=True, kind='official_league'):
        return sources.Evidence(self.fixture.fixture_id, self.fixture.kickoff.isoformat(), signal,
            'Argentina', owner, owner, official, True, f'https://example.com/{owner}', kind)

    def discover(self, html=None, **kwargs):
        index = f'<a href="{ARTICLE}">Agenda de las próximas fechas</a>'
        html = html or article('<p>Sábado 10 de octubre<br>19.30 Club Azul – Club Verde (Zona A) (ESPN Premium)</p>')
        transport = Mock()
        transport.fetch.side_effect = lambda url: {INDEX: index, ARTICLE: html}[url]
        result = discovery.discover(self.feed, self.groups, self.settings, self.mapping, now=NOW,
            registry=self.registry, providers=[self.provider()], transport=transport, **kwargs)
        return result, transport

    def test_registry_is_valid(self):
        registry_module.validate_registry(self.registry)

    def test_rights_only_never_auto_assign(self):
        config, _, report = discovery.discover(self.feed, self.groups, self.settings, self.mapping,
            now=NOW, registry=self.registry, providers=[])
        self.assertEqual([], config['broadcasts'])
        self.assertEqual('B', report['fixtures'][0]['rightsLevel'])

    def test_fixture_overrides_competition_priority(self):
        (config, _, report), _ = self.discover()
        self.assertEqual(['espn_premium'], config['broadcasts'][0]['channelGroups'])
        self.assertEqual('A', report['fixtures'][0]['evidenceLevel'])

    def test_organisation_overrides_conflicting_guide(self):
        result = discovery.confidence(self.fixture, [self.evidence(), self.evidence('TNT Sports', 'guide', False, 'trusted_guide')], self.mapping, self.groups)
        self.assertEqual(['espn_premium'], result['channelGroups'])

    def test_incompatible_official_sources_require_review(self):
        result = discovery.confidence(self.fixture, [self.evidence(),
            self.evidence('TNT Sports', 'holder', True, 'official_broadcaster')], self.mapping, self.groups)
        self.assertEqual('REVIEW', result['state'])
        self.assertEqual([], result['channelGroups'])

    def test_unexpected_official_signal_is_allowed(self):
        result = discovery.confidence(self.fixture, [self.evidence('TyC Sports')], self.mapping, self.groups)
        self.assertEqual(['tyc_sports'], result['channelGroups'])

    def test_multiple_official_signals_ordered(self):
        result = discovery.confidence(self.fixture, [self.evidence('TNT Sports'), self.evidence('ESPN Premium')], self.mapping, self.groups)
        self.assertEqual(['tnt_sports_premium', 'espn_premium'], result['channelGroups'])

    def test_two_independent_guides_are_level_c(self):
        result = discovery.confidence(self.fixture, [self.evidence(owner='one', official=False, kind='trusted_guide'),
            self.evidence(owner='two', official=False, kind='trusted_guide')], self.mapping, self.groups)
        self.assertEqual('C', result['evidenceLevel'])

    def test_one_guide_is_not_confirmed(self):
        result = discovery.confidence(self.fixture, [self.evidence(official=False, kind='trusted_guide')], self.mapping, self.groups)
        self.assertEqual('REVIEW', result['state'])

    def test_external_platform_is_not_linear_espn(self):
        result = discovery.confidence(self.fixture, [self.evidence('Disney+')], self.mapping, self.groups)
        self.assertEqual('CONFIRMED_EXTERNAL', result['state'])
        self.assertEqual([], result['channelGroups'])

    def test_tnt_argentina_never_resolves_chile(self):
        self.assertEqual('tnt_sports_premium', discovery.map_signal('TNT Sports', 'Argentina', self.mapping)[2])
        self.assertIsNone(discovery.map_signal('TNT Sports', 'Chile', self.mapping)[2])

    def test_fox_territory_isolated(self):
        self.assertEqual('fox_sports', discovery.map_signal('FOX Sports Argentina', 'Argentina', self.mapping)[2])
        self.assertIsNone(discovery.map_signal('FOX Sports Argentina', 'Mexico', self.mapping)[2])

    def test_wrong_group_region_rejected(self):
        wrong = copy.deepcopy(self.mapping)
        next(s for s in wrong['signals'] if s['name'] == 'TNT Sports Premium')['channelGroup'] = 'tnt_sports'
        with self.assertRaisesRegex(sources.DiscoveryError, 'Territorio'):
            discovery.validate_settings(self.settings, wrong, self.groups)

    def test_unknown_group_region_requires_review(self):
        self.groups['tnt_sports_premium']['signalRegion'] = ''
        result = discovery.confidence(self.fixture, [self.evidence('TNT Sports')], self.mapping, self.groups)
        self.assertEqual('REVIEW', result['state'])
        self.assertEqual([], result['channelGroups'])

    def test_shared_budget_across_real_provider_preparations(self):
        configs = [p for p in self.settings['providers'] if p['enabled']]
        transport = Mock()
        transport.fetch.return_value = article('<p>Sábado 10 de octubre 19.30 Club Azul – Club Verde (TNT Sports)</p>')
        _, _, report = discovery.discover(self.feed, self.groups, self.settings, self.mapping,
            now=NOW, registry=self.registry, transport=transport)
        self.assertLessEqual(transport.fetch.call_count, 6)
        self.assertLessEqual(report['requests'], 6)

    def test_lpf_index_rejects_arbitration_articles(self):
        provider = self.provider()
        transport = Mock()
        transport.fetch.side_effect = lambda url: (
            '<a href="/notas/primera/2026/10/09/autoridades-de-la-fecha/">Autoridades de la fecha</a>'
            f'<a href="{ARTICLE}">Agenda</a>' if url == INDEX else
            article('<p>Sábado 10 de octubre 19.30 Club Azul – Club Verde (TNT Sports)</p>'))
        provider.prepare(DAY, transport)
        self.assertEqual(2, transport.fetch.call_count)
        self.assertEqual(1, len(provider.lookup(self.fixture)))

    def test_expired_rights_have_no_candidates(self):
        self.registry['competitions'][0]['validUntil'] = '2026-10-09'
        self.assertEqual([], registry_module.search_hints(self.registry, self.fixture.competition, DAY))
        self.assertEqual([], registry_module.candidates(self.registry, self.fixture.competition, DAY, season='2026'))

    def test_unknown_expiry_not_perpetual(self):
        self.assertEqual([], registry_module.search_hints(self.registry, self.fixture.competition, date(2027, 1, 1)))

    def test_unknown_season_not_trusted(self):
        self.assertEqual([], registry_module.candidates(self.registry, self.fixture.competition, DAY))

    def test_serie_a_brazil_does_not_use_italy_registry(self):
        self.assertEqual([], registry_module.search_hints(self.registry, 'Serie A', DAY, 'Brazil'))
        self.assertEqual(['ESPN'], registry_module.search_hints(self.registry, 'Serie A', DAY, 'Italy'))

    def test_known_season_candidates_only(self):
        result = registry_module.candidates(self.registry, self.fixture.competition, DAY, season='2026')
        self.assertEqual(['TNT Sports Premium', 'ESPN Premium'], [s['name'] for s in result])

    def test_manual_assignment_wins(self):
        manual = {'fixtureId': self.fixture.fixture_id, 'channelGroups': ['tyc_sports'], 'confidence': 'confirmed', 'source': 'manual'}
        previous = {'schemaVersion': 1, 'date': str(DAY), 'timezone': TIMEZONE, 'broadcasts': [manual]}
        (config, _, _), transport = self.discover(previous=previous)
        self.assertEqual([manual], config['broadcasts'])
        transport.fetch.assert_not_called()

    def test_daily_cache_avoids_lookup(self):
        (config, cache, _), _ = self.discover()
        (second, _, report), transport = self.discover(previous=config, cache=cache)
        self.assertEqual(config, second)
        self.assertEqual(0, report['requests'])
        transport.fetch.assert_not_called()

    def test_registry_changes_invalidate_cache(self):
        (config, cache, _), _ = self.discover()
        self.registry['competitions'][0]['officialBroadcasters'][0]['notes'] += ' Review.'
        (_, _, report), transport = self.discover(previous=config, cache=cache)
        self.assertEqual(2, report['requests'])
        self.assertEqual(2, transport.fetch.call_count)

    def test_article_date_not_publication_date(self):
        self.assertEqual([], sources.lpf_listings(article('<p>Viernes 9 de octubre<br>19.30 Club Azul – Club Verde (TNT Sports)</p>'), DAY))

    def test_missing_year_fails_closed(self):
        with self.assertRaises(sources.DiscoveryError):
            sources.lpf_listings('<div class="entry-content">Sábado 10 de octubre 19.30 A – B (TNT Sports)</div>', DAY)

    def test_multiple_years_fail_closed(self):
        with self.assertRaises(sources.DiscoveryError):
            sources.lpf_listings(article('<p>2027 Sábado 10 de octubre 19.30 A – B (TNT Sports)</p>'), DAY)

    def test_ambiguous_short_names_not_assigned(self):
        provider = self.provider()
        provider.listings = sources.lpf_listings(article('<p>Sábado 10 de octubre 19.30 Club – Club (TNT Sports)</p>'), DAY)
        provider.bind_fixtures([self.fixture, replace(self.fixture, fixture_id=7002, home='Club Negro', away='Club Blanco')])
        self.assertEqual([], provider.lookup(self.fixture))

    def test_youth_identity_never_matches_senior(self):
        provider = self.provider()
        provider.listings = sources.lpf_listings(article('<p>Sábado 10 de octubre 19.30 Club Azul – Club Verde (TNT Sports)</p>'), DAY)
        young = replace(self.fixture, home='Club Azul U20')
        provider.bind_fixtures([young])
        self.assertEqual([], provider.lookup(young))

    def test_generic_club_label_never_confirms_even_single_fixture(self):
        provider = self.provider()
        provider.listings = sources.lpf_listings(article('<p>Sábado 10 de octubre 19.30 Club – Club (TNT Sports)</p>'), DAY)
        self.assertEqual([], provider.lookup(self.fixture))

    def test_other_country_primera_division_not_lpf(self):
        provider = self.provider()
        self.assertFalse(provider.supports(replace(self.fixture, competition='Primera Division', country='Chile')))

    def test_unknown_fixture_is_not_injected(self):
        (config, _, _), _ = self.discover(article('<p>Sábado 10 de octubre 19.30 Otro Club – Otro Equipo (TNT Sports)</p>'))
        self.assertEqual([], config['broadcasts'])

    def test_finished_fixture_not_discovered(self):
        self.feed['sections'][0]['events'][0]['status'] = 'FINISHED'
        (config, _, _), transport = self.discover()
        self.assertEqual([], config['broadcasts'])
        transport.fetch.assert_not_called()

    def test_request_budget_counts_articles(self):
        (_, _, report), transport = self.discover()
        self.assertEqual(2, report['requests'])
        self.assertEqual(2, transport.fetch.call_count)

    def test_request_budget_rejects_seventh_fetch(self):
        transport = Mock()
        budget = sources.RequestBudget(transport, 6)
        for _ in range(6): budget.fetch(ARTICLE)
        with self.assertRaises(sources.DiscoveryError): budget.fetch(ARTICLE)
        self.assertEqual(6, transport.fetch.call_count)

    def test_catalog_primaries_then_backups_deduplicated(self):
        config = {'schemaVersion': 1, 'date': str(DAY), 'timezone': TIMEZONE, 'broadcasts': [
            {'fixtureId': self.fixture.fixture_id, 'channelGroups': ['tnt_sports_premium', 'espn_premium', 'tnt_sports_premium'],
             'source': 'auto', 'confidence': 'confirmed'}]}
        channels = resolve_broadcasts(config, self.feed, self.groups, sources.norm)[f'fixture-{self.fixture.fixture_id}']
        expected = [self.groups[g]['streams'][0]['streamId'] for g in ('tnt_sports_premium', 'espn_premium')]
        self.assertEqual(expected, [c['streamId'] for c in channels[:2]])
        self.assertEqual(5, len(channels))
        self.assertEqual(5, len({c['streamId'] for c in channels}))
        self.assertEqual([1,2,3,4,5], [c['priority'] for c in channels])

    def test_instituto_boca_historical_regression_only(self):
        historical_day = date(2026, 10, 9)
        historical = agenda('Instituto Cordoba', 'Boca Juniors', 1493172, historical_day)
        fixture = discovery.fixtures_from_feed(historical)[0]
        provider = self.provider()
        provider.bind_fixtures([fixture])
        provider.listings = sources.lpf_listings(article('<p>Viernes 9 de octubre<br>19.30 Instituto – Boca (Zona A) (TNT Sports)</p>'), historical_day)
        result = discovery.confidence(fixture, provider.lookup(fixture), self.mapping, self.groups)
        self.assertEqual(['tnt_sports_premium'], result['channelGroups'])
        self.assertEqual('A', result['evidenceLevel'])
        config = {'schemaVersion':1,'date':str(historical_day),'timezone':TIMEZONE,'broadcasts':[
            {'fixtureId':1493172,'channelGroups':result['channelGroups'],'confidence':'confirmed','source':'auto'}]}
        channels = resolve_broadcasts(config, historical, self.groups, sources.norm)['fixture-1493172']
        self.assertEqual([165664,165666,165665], [c['streamId'] for c in channels])
        # The same historical article cannot produce assignments on a future day.
        self.assertEqual([], sources.lpf_listings(article('<p>Viernes 9 de octubre 19.30 Instituto – Boca (TNT Sports)</p>'), DAY))

    def test_no_fixture_or_stream_constants_in_new_production_code(self):
        for name in ('sports_competition_broadcasters.py', 'broadcaster_sources.py', 'broadcaster_discovery.py'):
            text = (ROOT / 'scripts' / name).read_text(encoding='utf8')
            for token in ('1493172','165664','165666','165665','Boca','Instituto'):
                self.assertNotIn(token, text)
        self.assertNotIn('streamId', json.dumps(self.registry))

    def test_invalid_registry_fails_before_network(self):
        self.registry['schemaVersion'] = True
        with self.assertRaises(sources.DiscoveryError): self.discover()

    def test_external_links_not_fetched(self):
        provider = self.provider()
        transport = Mock()
        transport.fetch.return_value = '<a href="https://example.com/notas/primera/2026/10/07/x/">Agenda</a>'
        with self.assertRaises(sources.DiscoveryError):
            provider.prepare(DAY, transport)
        self.assertEqual(1, transport.fetch.call_count)
        self.assertEqual([], provider.listings)

    def test_lpf_unexpected_known_signal_not_filtered_by_rights(self):
        (config, _, _), _ = self.discover(article('<p>Sábado 10 de octubre 19.30 Club Azul – Club Verde (TyC Sports)</p>'))
        self.assertEqual(['tyc_sports'], config['broadcasts'][0]['channelGroups'])

    def test_registry_duplicates_rejected(self):
        self.registry['competitions'].append(copy.deepcopy(self.registry['competitions'][0]))
        with self.assertRaises(sources.DiscoveryError): registry_module.validate_registry(self.registry)

    def test_lpf_general_two_live_terminal_and_new_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = copy.deepcopy(self.settings)
            for provider in config['providers']:
                provider['enabled'] = provider['id'] == 'lpf-official'
            for name, value in [('sports_broadcaster_sources.json', config),
                    ('sports_broadcaster_mapping.json', self.mapping), ('sports_competition_broadcasters.json', self.registry),
                    ('sports_channels.json', {'schemaVersion': 1, 'rules': []}),
                    ('mbox_sports_channels_clean.json', json.loads((ROOT / 'mbox_sports_channels_clean.json').read_text(encoding='utf8')))]:
                (root / name).write_text(json.dumps(value), encoding='utf8')
            output = root / 'sports_today.json'
            item = api_fixture(7001, league='Liga Profesional Argentina', country='Argentina',
                               time=f'{DAY}T19:30:00-03:00', status='NS')
            item['teams']['home']['name'], item['teams']['away']['name'] = 'Club Azul', 'Club Verde'
            # Another live fixture keeps the existing LIVE eligibility guard active
            # after the assigned match finishes; otherwise the correct action is skip.
            auxiliary = api_fixture(7009, league='Ligue 1', country='France',
                                    time=f'{DAY}T19:30:00-03:00', status='NS')
            transport = Mock()
            transport.fetch.side_effect = lambda url: (f'<a href="{ARTICLE}">Agenda</a>' if url == INDEX else
                article('<p>Sábado 10 de octubre 19.30 Club Azul – Club Verde (TNT Sports)</p>'))
            with patch.object(generator, 'ROOT', root), patch.object(generator, 'fetch_fixtures', return_value=response(item, auxiliary)), \
                    patch.object(discovery, 'PublicTransport', return_value=transport), contextlib.redirect_stdout(io.StringIO()):
                initial = generator.generate(DAY, output, now=NOW, discover_broadcasts=True)[0]
                event = next(e for s in initial['sections'] for e in s['events'])
                channels = event['channels']
                self.assertEqual(3, len(channels))
                self.assertEqual(2, transport.fetch.call_count)
                auxiliary['fixture']['status']['short'] = '1H'
                for minute, score in [(35, 1), (45, 2)]:
                    item['fixture']['status']['short'] = '1H'
                    item['goals'] = {'home': score, 'away': 0}
                    with patch.object(generator, 'discover_from_root', side_effect=AssertionError('No discovery in LIVE')):
                        live = generator.generate(DAY, output, now=NOW.replace(hour=19, minute=minute), live_only=True)[0]
                    current = next(e for s in live['sections'] for e in s['events'])
                    self.assertEqual(channels, current['channels'])
                    self.assertEqual(score, current['homeScore'])
                    self.assertEqual(1, live['schemaVersion'])
                item['fixture']['status']['short'] = 'FT'
                item['goals'] = {'home': 3, 'away': 0}
                for minute in (0, 10):
                    with patch.object(generator, 'discover_from_root', side_effect=AssertionError('No discovery in LIVE')):
                        final = generator.generate(DAY, output, now=NOW.replace(hour=20, minute=minute), live_only=True)[0]
                    current = next(e for s in final['sections'] for e in s['events'])
                    self.assertNotIn('channels', current)
                    self.assertEqual(3, current['homeScore'])
                self.assertEqual(2, transport.fetch.call_count)
                tomorrow = DAY + timedelta(days=1)
                item['fixture']['id'] = 7002
                item['fixture']['date'] = f'{tomorrow}T19:30:00-03:00'
                auxiliary['fixture']['date'] = f'{tomorrow}T19:30:00-03:00'
                auxiliary['fixture']['status']['short'] = 'NS'
                item['fixture']['status']['short'] = 'NS'
                item['goals'] = {'home': None, 'away': None}
                transport.fetch.side_effect = TimeoutError('Mock public outage')
                fresh = generator.generate(tomorrow, output, now=NOW + timedelta(days=1), discover_broadcasts=True)[0]
                self.assertTrue(all(not e.get('channels') for s in fresh['sections'] for e in s['events']))
                assignments = json.loads((root / 'sports_broadcasts_today.json').read_text(encoding='utf8'))
                self.assertEqual(str(tomorrow), assignments['date'])
                self.assertEqual([], assignments['broadcasts'])


if __name__ == '__main__':
    unittest.main()
