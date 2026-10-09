"""No internet: explicit schedule evidence, production pipeline and date isolation."""
import ast
import contextlib
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scripts import broadcaster_discovery as discovery
from scripts import broadcaster_sources as sources
from scripts import generate_sports_today as generator
from scripts import sports_daily_broadcasts as daily
from scripts.sports_channel_rules import validate_clean_groups
from test_generate_sports_today import fixture, response

ROOT = generator.ROOT
DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 8, 15, tzinfo=sources.ZONE)
CASES = [
    (1492398, 'Santos', 'Flamengo', '19:30', 'Win Sports', 'Colombia', 'win_sports', 2),
    (1520912, 'Ceará', 'Criciúma', '19:30', 'ESPN Brasil', 'Brasil', 'espn_brazil', 1),
    (1549764, 'Atlético Nacional', 'Tolima', '20:05', 'Win+ Fútbol', 'Colombia', 'win_sports_plus', 3),
    (1492396, 'Palmeiras', 'Bahia', '21:30', 'Win Sports', 'Colombia', 'win_sports', 2),
    (1581444, 'Alebrijes', 'Correcaminos', '22:00', 'ESPN 2 México', 'México', 'espn_2_mexico', 2),
    (1549817, 'Fortaleza', 'Millonarios', '22:10', 'Win+ Fútbol', 'Colombia', 'win_sports_plus', 3),
]


def settings():
    return json.loads((ROOT / 'sports_broadcaster_sources.json').read_text(encoding='utf-8'))


def mapping():
    return json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf-8'))


def clean():
    # Synthetic streams only; real fixture IDs are regression identifiers, not IPTV IDs.
    real = json.loads((ROOT / 'mbox_sports_channels_clean.json').read_text(encoding='utf-8'))
    counts = {case[6]: case[7] for case in CASES}
    return {'schemaVersion': 1, 'groups': [
        {'id': group['id'], 'canonicalName': group['canonicalName'], 'aliases': ['Test alias'],
         'streams': [{'streamId': 10000 + index * 10 + number,
                      'name': f'Test signal {index} option {number}', 'priority': number, 'enabled': True}
                     for number in range(1, counts.get(group['id'], 1) + 1)]}
        for index, group in enumerate(real['groups'])]}


def payload(day=DAY, status='NS', score=0):
    items = []
    for identifier, home, away, clock, *_ in CASES:
        item = fixture(identifier, league='Serie A' if identifier in (1492398, 1492396) else 'Fixture competition',
                       country='Brazil' if identifier in (1492398, 1492396, 1520912) else 'Colombia',
                       time=f'{day}T{clock}:00-03:00', status=status)
        item['teams']['home']['name'], item['teams']['away']['name'] = home, away
        item['goals'] = {'home': score if status != 'NS' else None, 'away': 0 if status != 'NS' else None}
        items.append(item)
    return response(*items)


def agenda(day=DAY):
    with contextlib.redirect_stdout(io.StringIO()):
        return generator.build_feed(payload(day), day, NOW.replace(day=day.day))[0]


def entries(*items, day=DAY):
    return {'schemaVersion': 1, 'date': str(day), 'timezone': daily.TIMEZONE, 'broadcasts': list(items)}


def evidence(item, broadcaster='Win Sports', owner='one', official=True, territory='Colombia'):
    return sources.Evidence(item.fixture_id, item.kickoff.isoformat(), broadcaster, territory,
                            owner, owner, official, True, f'https://example.com/{owner}')


class FakeProvider:
    def __init__(self, owner='one', official=True, signal=None, fail=False):
        self.sourceName, self.official, self.signal, self.fail = owner, official, signal, fail
        self.prepared, self.lookups = 0, 0

    def supports(self, fixture):
        return True

    def prepare(self, day, transport):
        self.prepared += 1
        if self.fail:
            raise TimeoutError('simulated')

    def lookup(self, item):
        self.lookups += 1
        case = next(case for case in CASES if case[0] == item.fixture_id)
        return [evidence(item, self.signal or case[4], self.sourceName, self.official, case[5])]


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.feed, self.sources, self.mapping = agenda(), settings(), mapping()
        self.groups = validate_clean_groups(clean(), sources.norm)
        self.fixtures = discovery.fixtures_from_feed(self.feed)
        self.item = next(item for item in self.fixtures if item.fixture_id == CASES[0][0])
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('No internet'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def classify(self, *items):
        return discovery.confidence(self.item, items, self.mapping, self.groups)

    def run_discovery(self, **kwargs):
        return discovery.discover(self.feed, self.groups, self.sources, self.mapping, now=NOW,
                                  providers=kwargs.pop('providers', [FakeProvider()]), **kwargs)

    def test_one_official_source_confirms_mbox(self):
        result = self.classify(evidence(self.item))
        self.assertEqual('CONFIRMED_MBOX', result['state'])
        self.assertEqual(['win_sports'], result['channelGroups'])

    def test_two_independent_trusted_sources_confirm(self):
        result = self.classify(evidence(self.item, official=False), evidence(self.item, owner='two', official=False))
        self.assertEqual('CONFIRMED_MBOX', result['state'])
        self.assertEqual(2, result['evidenceCount'])

    def test_same_editorial_owner_is_not_two_votes(self):
        result = self.classify(evidence(self.item, official=False), evidence(self.item, official=False))
        self.assertEqual('REVIEW', result['state'])

    def test_one_nonofficial_source_needs_review(self):
        self.assertEqual('REVIEW', self.classify(evidence(self.item, official=False))['state'])

    def test_two_agreeing_tv_sources_do_not_confirm_extra_weak_platform(self):
        result = self.classify(evidence(self.item, 'Win+', official=False),
            evidence(self.item, 'Win Fútbol +', owner='two', official=False),
            evidence(self.item, 'Win Play', owner='two', official=False))
        self.assertEqual('CONFIRMED_MBOX', result['state'])
        self.assertEqual(['win_sports_plus'], result['channelGroups'])
        self.assertEqual(['Win+ Fútbol'], result['broadcaster'])

    def test_conflicting_sources_review_without_assignment(self):
        result = self.classify(evidence(self.item), evidence(self.item, 'Win+', owner='two'))
        self.assertEqual('REVIEW', result['state'])
        self.assertEqual([], result['channelGroups'])

    def test_official_source_can_explicitly_confirm_simulcast(self):
        result = self.classify(evidence(self.item), evidence(self.item, 'Win+'))
        self.assertEqual('CONFIRMED_MBOX', result['state'])
        self.assertEqual(['win_sports', 'win_sports_plus'], result['channelGroups'])

    def test_external_platform_does_not_create_channels(self):
        for name in ('Fanatiz', 'LPF Play', 'OneFootball', 'Disney+', 'YouTube', 'Entel Gol'):
            with self.subTest(name=name):
                result = self.classify(evidence(self.item, name))
                self.assertEqual('CONFIRMED_EXTERNAL', result['state'])
                self.assertEqual([], result['channelGroups'])

    def test_espn_regional_variants_are_distinct(self):
        for name, region, group in [('ESPN', 'Argentina', 'espn'), ('ESPN', 'Brasil', 'espn_brazil'),
              ('ESPN Brazil', 'Argentina', 'espn_brazil'), ('ESPN2 México', 'México', 'espn_2_mexico'),
              ('ESPN Premium', 'Argentina', 'espn_premium'), ('ESPN 2', 'Argentina', 'espn_2')]:
            with self.subTest(name=name, region=region):
                self.assertEqual(group, discovery.map_signal(name, region, self.mapping)[2])

    def test_espn_without_region_does_not_guess(self):
        self.assertTrue(discovery.map_signal('ESPN', '', self.mapping)[3])

    def test_hd_accent_plus_normalization(self):
        for name, group in [('WIN SPORTS HD', 'win_sports'), ('Win+', 'win_sports_plus'),
                            ('Win Sports+', 'win_sports_plus'), ('Win+ Fútbol', 'win_sports_plus')]:
            self.assertEqual(group, discovery.map_signal(name, 'Colombia', self.mapping)[2])

    def test_manual_wins_over_different_auto_signal(self):
        item = {'fixtureId': self.item.fixture_id, 'channelGroups': ['espn_brazil'], 'confidence': 'confirmed', 'source': 'manual'}
        config, _, report = self.run_discovery(previous=entries(item))
        self.assertEqual(item, next(value for value in config['broadcasts'] if value['fixtureId'] == self.item.fixture_id))
        self.assertEqual(1, report['manualPreserved'])

    def test_legacy_source_absent_is_manual(self):
        item = {'fixtureId': self.item.fixture_id, 'channelGroups': ['espn_brazil'], 'confidence': 'confirmed'}
        config, _, _ = self.run_discovery(previous=entries(item))
        self.assertIn(item, config['broadcasts'])

    def test_cache_prevents_all_duplicate_requests(self):
        config, cache, _ = self.run_discovery()
        second = FakeProvider()
        after, _, report = self.run_discovery(previous=config, cache=cache, providers=[second])
        self.assertEqual(config, after)
        self.assertEqual(6, report['cacheHits'])
        self.assertEqual(0, report['requests'])
        self.assertEqual((0, 0), (second.prepared, second.lookups))

    def test_cache_expires_on_argentina_date(self):
        _, cache, _ = self.run_discovery()
        cache['date'] = str(DAY - timedelta(days=1))
        provider = FakeProvider()
        _, _, report = self.run_discovery(cache=cache, providers=[provider])
        self.assertEqual(0, report['cacheHits'])
        self.assertEqual(6, provider.lookups)

    def test_cache_expired_unresolved_retries_later(self):
        config, cache, _ = self.run_discovery(providers=[])
        cache['fixtures'][str(self.item.fixture_id)]['timestamp'] = (NOW - timedelta(hours=3)).isoformat()
        provider = FakeProvider()
        _, _, report = self.run_discovery(previous=config, cache=cache, providers=[provider])
        self.assertEqual(1, report['investigated'])

    def test_score_change_does_not_invalidate_discovery_cache(self):
        config, cache, _ = self.run_discovery()
        for section in self.feed['sections']:
            for event in section['events']:
                event.update(status='LIVE', homeScore=2, awayScore=1)
        _, _, report = self.run_discovery(previous=config, cache=cache)
        self.assertEqual(0, report['requests'])

    def test_kickoff_change_invalidates_cached_evidence(self):
        _, cache, _ = self.run_discovery()
        next(event for section in self.feed['sections'] for event in section['events']
             if event['id'] == f'fixture-{self.item.fixture_id}')['startTime'] = '20:00'
        _, _, report = self.run_discovery(cache=cache)
        self.assertEqual(1, report['investigated'])

    def test_provider_timeout_isolated(self):
        config, _, report = self.run_discovery(providers=[FakeProvider('down', fail=True), FakeProvider('good')])
        self.assertEqual(6, len(config['broadcasts']))
        self.assertEqual([{'provider': 'down', 'error': 'TimeoutError'}], report['providerWarnings'])

    def test_all_sources_fail_preserving_same_day_auto(self):
        config, _, _ = self.run_discovery()
        after, _, report = self.run_discovery(previous=config, providers=[FakeProvider(fail=True)])
        self.assertEqual(config, after)
        self.assertEqual(6, report['counts']['CONFIRMED_MBOX'])

    def test_outage_backoff_keeps_confirmed_without_repeating_requests(self):
        config, _, _ = self.run_discovery()
        preserved, cache, _ = self.run_discovery(previous=config, providers=[FakeProvider(fail=True)])
        again, _, report = self.run_discovery(previous=preserved, cache=cache)
        self.assertEqual(config, again)
        self.assertEqual(0, report['requests'])
        self.assertEqual(6, report['cacheHits'])

    def test_new_day_does_not_copy_old_assignments(self):
        config, _, _ = self.run_discovery()
        config['date'] = str(DAY - timedelta(days=1))
        after, _, _ = self.run_discovery(previous=config, providers=[])
        self.assertEqual([], after['broadcasts'])

    def test_unknown_group_configuration_rejected(self):
        self.mapping['signals'][0]['channelGroup'] = 'invented'
        with self.assertRaisesRegex(discovery.DiscoveryError, 'Unknown channelGroup'):
            self.run_discovery()

    def test_zero_confirmed_produces_valid_empty_config(self):
        config, _, report = self.run_discovery(providers=[])
        daily.validate_broadcasts(config)
        self.assertEqual([], config['broadcasts'])
        self.assertEqual(6, report['counts']['UNRESOLVED'])

    def test_wrong_fixture_or_kickoff_evidence_rejected(self):
        for invalid in (replace(evidence(self.item), fixture_id=999),
                        replace(evidence(self.item), kickoff=(NOW + timedelta(days=1)).isoformat())):
            self.assertEqual('UNRESOLVED', self.classify(invalid)['state'])

    def test_untrusted_evidence_rejected(self):
        self.assertEqual('UNRESOLVED', self.classify(replace(evidence(self.item), trusted=False))['state'])

    def test_duplicate_editorial_domain_configuration_rejected(self):
        duplicate = copy.deepcopy(self.sources['providers'][0])
        duplicate.update(id='another', independenceKey='fake-independent')
        self.sources['providers'].append(duplicate)
        with self.assertRaises(discovery.DiscoveryError):
            self.run_discovery()

    def test_request_and_concurrency_caps_validated(self):
        for key, value in [('maxRequests', 7), ('concurrency', 3), ('timeoutSeconds', True), ('timeoutSeconds', 9), ('maxFixtures', 0)]:
            original = self.sources['limits'][key]
            self.sources['limits'][key] = value
            with self.subTest(key=key), self.assertRaises(discovery.DiscoveryError):
                self.run_discovery()
            self.sources['limits'][key] = original

    def test_fixture_cap_does_not_cache_uninvestigated_fixtures(self):
        self.sources['limits']['maxFixtures'] = 2
        _, cache, report = self.run_discovery()
        self.assertEqual(2, report['investigated'])
        self.assertEqual(2, len(cache['fixtures']))
        self.assertEqual(6, len(report['fixtures']))

    def test_tbd_report_is_unresolved_without_auto_assignment(self):
        event = next(event for section in self.feed['sections'] for event in section['events'])
        event['startTime'] = ''
        config, _, report = self.run_discovery()
        row = next(row for row in report['fixtures'] if f"fixture-{row['fixtureId']}" == event['id'])
        self.assertEqual('UNRESOLVED', row['state'])
        self.assertEqual(6, sum(report['counts'].values()))
        self.assertNotIn(int(event['id'].removeprefix('fixture-')), [item['fixtureId'] for item in config['broadcasts']])

    def test_disabled_stream_group_requires_review(self):
        for stream in self.groups['win_sports']['streams']:
            stream['enabled'] = False
        self.assertEqual('REVIEW', self.classify(evidence(self.item))['state'])

    def test_unresolved_scope_skips_past_kickoffs(self):
        for section in self.feed['sections']:
            for event in section['events']:
                event['startTime'] = '12:00'
        _, _, report = self.run_discovery(scope='unresolved')
        self.assertEqual(0, report['requests'])

    def test_unresolved_scope_keeps_confirmed_without_cache(self):
        config, _, _ = self.run_discovery()
        after, _, report = self.run_discovery(previous=config, scope='unresolved')
        self.assertEqual(config, after)
        self.assertEqual(0, report['requests'])

    def test_lookup_failure_does_not_abort_other_sources(self):
        broken = FakeProvider('broken')
        broken.lookup = Mock(side_effect=ValueError('changed data'))
        config, _, report = self.run_discovery(providers=[broken, FakeProvider('good')])
        self.assertEqual(6, len(config['broadcasts']))
        self.assertEqual(6, len(report['providerWarnings']))

    def test_provider_concurrency_never_exceeds_two(self):
        lock = threading.Lock()
        counts = {'active': 0, 'peak': 0, 'requests': 0}
        class Counted(FakeProvider):
            def prepare(self, day, transport):
                with lock:
                    counts['active'] += 1
                    counts['requests'] += 1
                    counts['peak'] = max(counts['peak'], counts['active'])
                time.sleep(0.03)
                with lock:
                    counts['active'] -= 1
        _, _, report = self.run_discovery(providers=[Counted(str(i)) for i in range(6)])
        self.assertEqual(6, counts['requests'])
        self.assertEqual(6, report['requests'])
        self.assertLessEqual(counts['peak'], 2)

    def test_future_cache_timestamp_does_not_reuse_evidence(self):
        _, cache, _ = self.run_discovery()
        for entry in cache['fixtures'].values():
            entry['timestamp'] = (NOW + timedelta(hours=1)).isoformat()
        _, _, report = self.run_discovery(cache=cache)
        self.assertEqual(6, report['investigated'])

    def test_source_metadata_validated_without_android_leak(self):
        for invalid in ({'source': 'guess'}, {'evidenceCount': True}, {'broadcaster': ['https://private.example']}):
            value = {'fixtureId': self.item.fixture_id, 'channelGroups': ['win_sports'], 'confidence': 'confirmed', **invalid}
            with self.assertRaises(daily.BroadcastError):
                daily.validate_broadcasts(entries(value))


class ProviderTest(unittest.TestCase):
    def test_guide_explicit_date_and_colombian_time(self):
        html = '''<table><tr class="cabeceraTabla"><th>Jueves, 8/10/2026</th></tr>
          <tr class="cabeceraCompericion"><th>Liga</th></tr><tr>
          <td class="hora">18:05</td><td class="local">Atlético Nacional</td><td class="visitante">Deportes Tolima</td>
          <td class="canales"><meta itemprop="startDate" content="2026-10-08T16:05:00"><ul class="listaCanales">
          <li>Win Fútbol +</li><li>Win Play</li></ul></td></tr></table>'''
        listing = sources.football_tv_listings(html, DAY)[0]
        self.assertEqual('20:05', listing.kickoff.strftime('%H:%M'))
        self.assertEqual(('Win Fútbol +', 'Win Play'), listing.broadcasters)
        self.assertEqual([], sources.football_tv_listings(html, DAY + timedelta(days=1)))

    def test_tyс_exact_date_pair_and_explicit_tv(self):
        html = '''<div class="agenda_results"><h2>8 de octubre del 2026</h2>
          <div class="agenda_comp_results" data-selectDeporte="futbol"><h3 class="header_comp_agenda">Cup</h3>
          <div class="item_agenda"><div class="text-teams-agenda"><img alt="Santos"><img alt="Flamengo"></div>
          <span class="hs-item-agenda">19:30</span><div class="text-channels-agenda"><span>Win Sports</span></div></div></div></div>'''
        result = sources.tyc_listings(html, DAY)
        self.assertEqual(('Win Sports',), result[0].broadcasters)
        self.assertEqual([], sources.tyc_listings(html, DAY + timedelta(days=1)))
        self.assertEqual([], sources.tyc_listings(html.replace('<span>Win Sports</span>', ''), DAY))

    def test_changed_html_fails_provider_without_inference(self):
        for parser in (sources.tyc_listings, sources.espn_listings):
            with self.assertRaises(discovery.DiscoveryError):
                parser('<html>New layout</html>', DAY)

    def test_espn_only_explicit_broadcasts(self):
        event = {'date': '2026-10-08T22:30:00Z', 'competitors': [
            {'isHome': True, 'displayName': 'Santos'}, {'isHome': False, 'displayName': 'Flamengo'}],
            'broadcasts': ['ESPN']}
        data = {'page': {'content': {'events': [[event]]}}}
        html = "window['__espnfitt__']=" + json.dumps({'data': data}) + ';'
        self.assertEqual(('ESPN',), sources.espn_listings(html, DAY)[0].broadcasters)
        event['broadcasts'] = []
        html = "window['__espnfitt__']=" + json.dumps({'data': data}) + ';'
        self.assertEqual([], sources.espn_listings(html, DAY))

    def test_win_clock_converted_and_winplay_not_inferred(self):
        html = '<h2>Jueves 8 de octubre</h2><article><h3>Liga</h3>Santos vs Flamengo 5:30 PM Win Sports</article>'
        clock = Mock(wraps=datetime)
        clock.now.return_value = NOW
        with patch.object(sources, 'datetime', clock):
            result = sources.win_listings(html, DAY)
            self.assertEqual('19:30', result[0].kickoff.strftime('%H:%M'))
            self.assertEqual([], sources.win_listings(html.replace('Win Sports', 'Ver Online Win Play'), DAY))
            self.assertEqual([], sources.win_listings(html, DAY + timedelta(days=1)))

    def test_jsonld_official_event_requires_full_timestamp(self):
        data = {'@type': 'BroadcastEvent', 'publishedOn': {'name': 'ESPN'},
                'broadcastOfEvent': {'@type': 'SportsEvent', 'name': 'Cup', 'startDate': '2026-10-08T19:30:00-03:00',
                   'homeTeam': {'name': 'Santos'}, 'awayTeam': {'name': 'Flamengo'}}}
        html = '<script type="application/ld+json">'+json.dumps(data)+'</script>'
        self.assertEqual(1, len(sources.jsonld_listings(html, DAY)))
        self.assertEqual([], sources.jsonld_listings(html, DAY + timedelta(days=1)))

    def test_match_needs_both_exact_teams_and_kickoff(self):
        provider = sources.BroadcasterProvider(next(p for p in settings()['providers'] if p['id'] == 'win-official'), {})
        item = sources.Fixture(1, 'Santos', 'Flamengo', 'Cup', NOW)
        provider.listings = [sources.Listing('Santos', 'Flamengo', NOW, 'Cup', ('Win Sports',))]
        self.assertEqual(1, len(provider.lookup(item)))
        for other in (replace(item, home='Santos U20'), replace(item, away='Flamengo Women'),
                      replace(item, kickoff=NOW + timedelta(minutes=1))):
            self.assertEqual([], provider.lookup(other))

    def test_official_broadcaster_is_official_only_for_own_signal(self):
        provider = sources.BroadcasterProvider(next(p for p in settings()['providers'] if p['id'] == 'tyc-agenda'), {})
        item = sources.Fixture(1, 'Santos', 'Flamengo', 'Cup', NOW)
        provider.listings = [sources.Listing('Santos', 'Flamengo', NOW, 'Cup', ('TyC Sports', 'ESPN'))]
        self.assertEqual([True, False], [item.official for item in provider.lookup(item)])

    def test_private_credentials_urls_rejected_before_network(self):
        for url in ('http://example.com', 'https://u:p@example.com', 'https://127.0.0.1',
                    'https://localhost/a', 'https://example.com/player_api.php', 'https://example.com?token=x'):
            with self.subTest(url=url), self.assertRaises(discovery.DiscoveryError):
                sources.public_url(url)

    def test_public_request_hard_timeout_has_no_retry(self):
        with patch.object(sources.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 16)) as worker:
            with self.assertRaises(TimeoutError):
                sources.PublicTransport(8).fetch('https://example.com/programme')
        worker.assert_called_once()
        self.assertEqual(16, worker.call_args.kwargs['timeout'])

    def test_timeout_cannot_exceed_eight_seconds(self):
        for timeout in (True, 0, 9, 16, '8'):
            with self.subTest(timeout=timeout), self.assertRaises(discovery.DiscoveryError):
                sources.PublicTransport(timeout)

    def test_redirects_disabled(self):
        self.assertIsNone(sources.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://example.org'))

    def test_parser_depth_bounded(self):
        with self.assertRaises(discovery.DiscoveryError):
            sources.Tree('<div>' * 85)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, value in [('sports_broadcaster_sources.json', settings()), ('sports_broadcaster_mapping.json', mapping()),
                            ('mbox_sports_channels_clean.json', clean()), ('sports_channels.json', {'schemaVersion': 1, 'rules': []})]:
            self.write(name, value)
        self.output = self.root / 'output.json'
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('No internet'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def generate(self, data=None, discover=True, day=DAY, now=NOW, **kwargs):
        data = data or payload(day)
        provider_factory = kwargs.pop('provider_factory', lambda config, aliases: FakeProvider(config['id']))
        with patch.object(generator, 'ROOT', self.root), patch.object(generator, 'fetch_fixtures', return_value=data) as fetch, \
                patch.object(discovery, 'BroadcasterProvider', side_effect=provider_factory), \
                contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(day, self.output, now=now, discover_broadcasts=discover, **kwargs)[0]
        fetch.assert_called_once_with(day)
        generator.validate_feed(result)
        return result

    def channels(self, feed):
        return {event['id']: event['channels'] for section in feed['sections'] for event in section['events'] if event.get('channels')}

    def test_general_then_two_live_runs_and_new_day_complete_simulation(self):
        initial = self.generate()
        channels = self.channels(initial)
        self.assertEqual(6, len(channels))
        self.assertEqual(13, sum(map(len, channels.values())))
        for identifier, _, _, _, _, _, _, count in CASES:
            self.assertEqual(count, len(channels[f'fixture-{identifier}']))
        for score in (1, 2):
            with patch.object(generator, 'discover_from_root', side_effect=AssertionError('LIVE must not discover')):
                live = self.generate(payload(status='1H', score=score), discover=False, live_only=True,
                                     now=NOW.replace(hour=19, minute=25))
            self.assertEqual(channels, self.channels(live))
            for section in live['sections']:
                for event in section['events']:
                    self.assertEqual(score, event['homeScore'])
                    self.assertNotIn('channelGroups', event)
                    self.assertNotIn('source', event)
            self.assertEqual(1, live['schemaVersion'])
        # Delete old channels entirely: regeneration still comes from daily assignments.
        stripped = copy.deepcopy(live)
        for section in stripped['sections']:
            for event in section['events']:
                event.pop('channels', None)
        self.write('output.json', stripped)
        again = self.generate(payload(status='FT', score=2), discover=False)
        self.assertEqual({}, self.channels(again))
        for section in again['sections']:
            for event in section['events']:
                self.assertEqual('FINISHED', event['status'])
                self.assertEqual(2, event['homeScore'])
        tomorrow = DAY + timedelta(days=1)
        with patch.object(generator, 'discover_from_root', return_value=(entries(day=tomorrow), {},
              {'investigated': 6, 'requests': 0, 'counts': {'CONFIRMED_MBOX': 0}}, {})):
            new = self.generate(payload(tomorrow), day=tomorrow, now=NOW.replace(day=9))
        self.assertEqual({}, self.channels(new))
        self.assertEqual(str(tomorrow), json.loads((self.root / 'sports_broadcasts_today.json').read_text())['date'])

    def test_missing_old_feed_does_not_block_fresh_assignment(self):
        self.write('sports_broadcasts_today.json', entries({'fixtureId': CASES[0][0], 'channelGroups': ['win_sports'], 'confidence': 'confirmed'}))
        self.assertEqual(2, len(self.channels(self.generate(discover=False))[f'fixture-{CASES[0][0]}']))

    def test_invalid_discovery_config_refreshes_scores_and_preserves_assignments(self):
        self.generate()
        before = (self.root / 'sports_broadcasts_today.json').read_bytes()
        self.write('sports_broadcaster_mapping.json', {'invalid': True})
        updated = self.generate(payload(status='1H', score=2))
        self.assertEqual(13, sum(map(len, self.channels(updated).values())))
        self.assertEqual(json.loads(before), json.loads((self.root / 'sports_broadcasts_today.json').read_text(encoding='utf-8')))
        self.assertEqual('DISCOVERY_FAILED', json.loads((self.root / 'sports_broadcast_discovery_report.json').read_text())['state'])

    def test_zero_confirmed_generator_still_produces_agenda(self):
        with patch.object(discovery, 'BroadcasterProvider', side_effect=lambda config, aliases: FakeProvider(config['id'], fail=True)), \
                patch.object(generator, 'ROOT', self.root), patch.object(generator, 'fetch_fixtures', return_value=payload()), \
                contextlib.redirect_stdout(io.StringIO()):
            feed = generator.generate(DAY, self.output, now=NOW, discover_broadcasts=True)[0]
        generator.validate_feed(feed)
        self.assertEqual(6, sum(len(section['events']) for section in feed['sections']))
        self.assertEqual({}, self.channels(feed))
        self.assertEqual([], json.loads((self.root / 'sports_broadcasts_today.json').read_text())['broadcasts'])

    def test_corrupt_cache_is_discarded_and_reported(self):
        (self.root / 'sports_broadcast_discovery_cache.json').write_bytes(b'{broken')
        feed = self.generate()
        self.assertEqual(13, sum(map(len, self.channels(feed).values())))
        report = json.loads((self.root / 'sports_broadcast_discovery_report.json').read_text(encoding='utf-8'))
        self.assertIn({'provider': 'cache', 'error': 'InvalidCache'}, report['providerWarnings'])

    def test_invalid_daily_metadata_preserves_feed_without_api_call(self):
        self.write('sports_broadcasts_today.json', entries({'fixtureId': CASES[0][0], 'channelGroups': ['win_sports'],
                    'confidence': 'confirmed', 'source': 'guess'}))
        self.output.write_bytes(b'keep')
        with patch.object(generator, 'ROOT', self.root), patch.object(generator, 'fetch_fixtures') as fetch:
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output, now=NOW, discover_broadcasts=True)
        fetch.assert_not_called()
        self.assertEqual(b'keep', self.output.read_bytes())

    def test_empty_discovery_new_day_does_not_inherit_old_config(self):
        self.generate()
        tomorrow = DAY + timedelta(days=1)
        with patch.object(generator, 'ROOT', self.root), patch.object(generator, 'fetch_fixtures', return_value=payload(tomorrow)), \
                patch.object(discovery, 'BroadcasterProvider', side_effect=lambda config, aliases: FakeProvider(config['id'], fail=True)), \
                contextlib.redirect_stdout(io.StringIO()):
            feed = generator.generate(tomorrow, self.output, now=NOW.replace(day=9), discover_broadcasts=True)[0]
        self.assertEqual({}, self.channels(feed))
        stored = json.loads((self.root / 'sports_broadcasts_today.json').read_text())
        self.assertEqual(str(tomorrow), stored['date'])
        self.assertEqual([], stored['broadcasts'])

    def test_unknown_fresh_fixture_fails_without_overwriting_feed(self):
        self.write('sports_broadcasts_today.json', entries({'fixtureId': 999, 'channelGroups': ['win_sports'], 'confidence': 'confirmed'}))
        self.output.write_bytes(b'keep')
        with self.assertRaisesRegex(generator.GenerationError, 'Unknown daily fixtureId'):
            self.generate(discover=False)
        self.assertEqual(b'keep', self.output.read_bytes())

    def test_unknown_fixture_cannot_be_silently_removed_by_discovery(self):
        self.write('sports_broadcasts_today.json', entries({'fixtureId': 999, 'channelGroups': ['win_sports'], 'confidence': 'confirmed'}))
        self.output.write_bytes(b'keep')
        with patch.object(generator, 'discover_from_root', side_effect=AssertionError('Invalid IDs must stop before discovery')):
            with self.assertRaisesRegex(generator.GenerationError, 'Unknown daily fixtureId'):
                self.generate()
        self.assertEqual(b'keep', self.output.read_bytes())



    def test_auto_audit_fields_never_reach_android(self):
        feed = self.generate()
        self.assertEqual(1, feed['schemaVersion'])
        self.assertNotIn('channelGroups', json.dumps(feed))
        config = json.loads((self.root / 'sports_broadcasts_today.json').read_text())
        self.assertTrue(all(item['source'] == 'auto' and item['evidenceCount'] >= 1 for item in config['broadcasts']))
        self.assertNotIn('streamId', json.dumps(config))

    def test_cache_key_changes_with_day_or_mapping(self):
        original = discovery.cache_key(self.root, DAY)
        self.assertNotEqual(original, discovery.cache_key(self.root, DAY + timedelta(days=1)))
        value = mapping()
        value['signals'][0]['aliases'].append('Verified new spelling')
        self.write('sports_broadcaster_mapping.json', value)
        self.assertNotEqual(original, discovery.cache_key(self.root, DAY))

    def test_no_new_production_stream_ids_hardcoded(self):
        for name in ('broadcaster_discovery.py', 'broadcaster_sources.py'):
            tree = ast.parse((ROOT / 'scripts' / name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        self.assertFalse(isinstance(key, ast.Constant) and key.value == 'streamId' and isinstance(value, ast.Constant))
        self.assertNotIn('streamId', json.dumps(mapping()))

    def test_workflow_discovery_only_in_general(self):
        general = (ROOT / '.github/workflows/update-sports-today.yml').read_text(encoding='utf-8')
        live = (ROOT / '.github/workflows/update-sports-live.yml').read_text(encoding='utf-8')
        self.assertIn('generate_sports_today.py --discover-broadcasts', general)
        self.assertNotIn('--discover-broadcasts', live)
        self.assertNotIn('broadcaster_discovery.py', live)
        self.assertIn('run: python scripts/generate_sports_today.py --live', live)
        self.assertIn('git add -- sports_today.json sports_broadcasts_today.json', general)
        self.assertIn('--only -- sports_today.json sports_broadcasts_today.json', general)
        self.assertEqual(90, generator.MAX_DAILY_API_CALLS)
        self.assertEqual(5, general.count('- cron:'))
        self.assertEqual(2, live.count('- cron:'))

    def test_http403_preserves_auto_and_refreshes_scores(self):
        initial = self.generate()
        (self.root / 'sports_broadcast_discovery_cache.json').unlink()
        def blocked(config, aliases):
            provider = FakeProvider(config['id'])
            provider.prepare = Mock(side_effect=HTTPError('https://example.com/programme', 403, 'blocked', {}, None))
            return provider
        refreshed = self.generate(payload(status='1H', score=2), provider_factory=blocked)
        self.assertEqual(self.channels(initial), self.channels(refreshed))
        report = json.loads((self.root / 'sports_broadcast_discovery_report.json').read_text(encoding='utf-8'))
        self.assertTrue(all(item['error'] == 'HTTPError' for item in report['providerWarnings']))
        self.assertEqual(6, report['counts']['CONFIRMED_MBOX'])

    def test_empty_provider_results_do_not_erase_confirmed(self):
        initial = self.generate()
        (self.root / 'sports_broadcast_discovery_cache.json').unlink()
        def empty(config, aliases):
            provider = FakeProvider(config['id'])
            provider.lookup = Mock(return_value=[])
            return provider
        refreshed = self.generate(payload(status='1H', score=1), provider_factory=empty)
        self.assertEqual(self.channels(initial), self.channels(refreshed))
        self.assertEqual(13, sum(map(len, self.channels(refreshed).values())))

    def test_six_manual_assignments_survive_failed_general(self):
        manual = [{'fixtureId': case[0], 'channelGroups': [case[6]], 'confidence': 'confirmed', 'source': 'manual'} for case in CASES]
        self.write('sports_broadcasts_today.json', entries(*manual))
        refreshed = self.generate(provider_factory=lambda config, aliases: FakeProvider(config['id'], fail=True))
        self.assertEqual(13, sum(map(len, self.channels(refreshed).values())))
        saved = json.loads((self.root / 'sports_broadcasts_today.json').read_text(encoding='utf-8'))
        self.assertEqual({item['fixtureId']:item for item in manual}, {item['fixtureId']:item for item in saved['broadcasts']})

    def test_live_rejects_discovery_before_any_api_request(self):
        with patch.object(generator, 'fetch_fixtures') as fetch:
            with self.assertRaisesRegex(generator.GenerationError, 'LIVE reutiliza'):
                generator.generate(DAY, self.output, now=NOW, live_only=True, discover_broadcasts=True)
        fetch.assert_not_called()

    def test_invalid_cached_timestamp_is_rejected_safely(self):
        config, cache, _ = discovery.discover(agenda(), validate_clean_groups(clean(), sources.norm), settings(), mapping(),
            now=NOW, providers=[FakeProvider()])
        first = next(iter(cache['fixtures'].values()))
        first['result']['evidence'][0]['kickoff'] = None
        _, _, report = discovery.discover(agenda(), validate_clean_groups(clean(), sources.norm), settings(), mapping(),
            previous=config, cache=cache, now=NOW, providers=[FakeProvider()])
        self.assertEqual(1, report['investigated'])


if __name__ == '__main__':
    unittest.main()
