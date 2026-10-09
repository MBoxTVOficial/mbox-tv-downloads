import contextlib
import copy
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_sports_today as generator
from scripts.sports_playback_policy import apply_playback_policy
from test_generate_sports_today import DAY, NOW, fixture, response, section_events


def channel(sid=101):
    return {'name': 'Test signal', 'streamId': sid, 'priority': 1, 'enabled': True, 'aliases': []}


class PlaybackPolicyTest(unittest.TestCase):
    def make_feed(self, status='LIVE', start='19:30', sid=101):
        return {'date': str(DAY), 'timezone': generator.TIMEZONE, 'sections': [{'events': [
            {'id': 'fixture-100', 'sport': 'football', 'status': status, 'startTime': start,
             'homeScore': 2, 'awayScore': 1, 'channels': [channel(sid)]}]}]}

    def apply(self, feed, hour=21, minute=40, groups=None):
        return apply_playback_policy(feed, NOW.replace(hour=hour, minute=minute),
                                     generator.argentina_timezone(), groups, log=lambda _: None)

    def event(self, feed):
        return feed['sections'][0]['events'][0]

    def test_finished_removes_channels_preserving_every_other_field(self):
        feed = self.make_feed('FINISHED')
        original = copy.deepcopy(self.event(feed)); original.pop('channels')
        self.assertEqual(original, self.event(self.apply(feed)))

    def test_explicit_special_states_are_not_playable(self):
        for status in ('CANCELLED', 'POSTPONED', 'ABANDONED', 'SUSPENDED', 'UNKNOWN'):
            with self.subTest(status=status):
                self.assertNotIn('channels', self.event(self.apply(self.make_feed(status))))

    def test_live_within_three_hours_is_playable(self):
        self.assertEqual([channel()], self.event(self.apply(self.make_feed(), 22, 29))['channels'])

    def test_live_exact_three_hours_is_playable(self):
        self.assertIn('channels', self.event(self.apply(self.make_feed(), 22, 30)))

    def test_live_strictly_after_three_hours_is_not_playable_or_finished(self):
        event = self.event(self.apply(self.make_feed(), 22, 31))
        self.assertNotIn('channels', event); self.assertEqual('LIVE', event['status'])
        self.assertEqual((2, 1), (event['homeScore'], event['awayScore']))

    def test_non_football_does_not_use_three_hour_limit(self):
        feed = self.make_feed(); self.event(feed)['sport'] = 'tennis'
        self.assertIn('channels', self.event(self.apply(feed, 23, 0)))

    def test_scheduled_future_remains_playable(self):
        self.assertIn('channels', self.event(self.apply(self.make_feed('SCHEDULED', '23:00'))))

    def pair(self, first_id=101, second_id=101, second_status='LIVE'):
        feed = self.make_feed(sid=first_id)
        later = self.make_feed(second_status, '21:30', second_id)['sections'][0]['events'][0]
        later['id'] = 'fixture-200'; feed['sections'][0]['events'].append(later)
        return feed

    def test_santos_palmeiras_same_signal_superseded(self):
        events = self.apply(self.pair())['sections'][0]['events']
        self.assertNotIn('channels', events[0]); self.assertIn('channels', events[1])

    def test_same_group_different_backup_ids_also_superseded(self):
        groups = {'win': {'streams': [{'streamId': sid, 'enabled': True} for sid in (101, 102)]}}
        events = self.apply(self.pair(101, 102), groups=groups)['sections'][0]['events']
        self.assertNotIn('channels', events[0]); self.assertIn('channels', events[1])

    def test_different_broadcasters_do_not_affect_each_other(self):
        self.assertTrue(all('channels' in e for e in self.apply(self.pair(101, 201))['sections'][0]['events']))

    def test_before_later_kickoff_does_not_supersede(self):
        self.assertTrue(all('channels' in e for e in self.apply(self.pair(), 21, 29)['sections'][0]['events']))

    def test_exact_later_kickoff_supersedes(self):
        self.assertNotIn('channels', self.event(self.apply(self.pair(), 21, 30)))

    def test_later_postponed_or_cancelled_does_not_supersede(self):
        for status in ('POSTPONED', 'CANCELLED'):
            self.assertIn('channels', self.event(self.apply(self.pair(second_status=status))))

    def test_only_shared_broadcaster_removed(self):
        feed = self.pair(); self.event(feed)['channels'].append(channel(201))
        self.assertEqual([201], [c['streamId'] for c in self.event(self.apply(feed))['channels']])

    def test_invalid_time_and_old_day_fail_closed(self):
        for start in ('TBD', '', '25:00'):
            self.assertNotIn('channels', self.event(self.apply(self.make_feed(start=start))))
        feed = self.make_feed(); feed['date'] = '2026-10-05'
        self.assertNotIn('channels', self.event(self.apply(feed)))


class GeneratorPlaybackRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.output = self.root / 'sports_today.json'
        self.write('sports_channels.json', {'schemaVersion': 1, 'rules': []})
        self.write('mbox_sports_channels_clean.json', {'schemaVersion': 1, 'groups': [
            {'id': 'signal', 'canonicalName': 'Signal', 'aliases': [], 'streams': [
                {'name': 'Signal', 'streamId': 101, 'priority': 1, 'enabled': True}]}]})
        self.write('sports_broadcasts_today.json', {'schemaVersion': 1, 'date': str(DAY),
            'timezone': generator.TIMEZONE, 'broadcasts': [
                {'fixtureId': sid, 'channelGroups': ['signal'], 'confidence': 'confirmed'} for sid in (100, 200)]})

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding='utf-8')

    def run_generation(self, status, now, live=False):
        fixtures = [fixture(100, status=status, time=f'{DAY}T19:30:00-03:00'),
                    fixture(200, status='NS', time=f'{DAY}T21:30:00-03:00')]
        fixtures[0]['goals'] = {'home': 2, 'away': 1}
        with patch.object(generator, 'ROOT', self.root), patch.object(generator, 'fetch_fixtures', return_value=response(*fixtures)), \
                patch.object(socket, 'create_connection', side_effect=AssertionError('No network')), \
                patch.dict('os.environ', {'GITHUB_EVENT_NAME': 'schedule'}), contextlib.redirect_stdout(io.StringIO()):
            return generator.generate(DAY, self.output, now=now, live_only=live)[0]

    def test_general_live_then_second_live_remove_finished_repeatedly(self):
        first = self.run_generation('1H', NOW.replace(hour=19, minute=40))
        self.assertIn('channels', section_events(first)[0])
        for minute in (20, 30):
            feed = self.run_generation('FT', NOW.replace(hour=21, minute=minute), live=True)
            finished = next(e for e in section_events(feed) if e['id'] == 'fixture-100')
            self.assertNotIn('channels', finished)
            self.assertEqual((2, 1), (finished['homeScore'], finished['awayScore']))
            self.assertEqual(1, feed['schemaVersion'])

    def test_live_refresh_removes_superseded_signal_without_using_old_channels(self):
        self.run_generation('1H', NOW.replace(hour=19, minute=40))
        for minute in (40, 50):
            feed = self.run_generation('1H', NOW.replace(hour=21, minute=minute), live=True)
            events = {e['id']: e for e in section_events(feed)}
            self.assertNotIn('channels', events['fixture-100'])
            self.assertEqual([101], [c['streamId'] for c in events['fixture-200']['channels']])

    def test_workflows_share_policy_path(self):
        root = Path(generator.__file__).resolve().parents[1]
        for name in ('today', 'live'):
            text = (root / f'.github/workflows/update-sports-{name}.yml').read_text()
            self.assertIn('scripts/generate_sports_today.py', text)

    def test_santos_palmeiras_2120_then_2140_using_real_fixture_groups(self):
        identifiers = (1492398, 1492396)
        self.write('mbox_sports_channels_clean.json', {'schemaVersion': 1, 'groups': [
            {'id': 'win_sports', 'canonicalName': 'Win Sports', 'aliases': [], 'streams': [
                {'name': 'Test WIN', 'streamId': 101, 'priority': 1, 'enabled': True}]}]})
        self.write('sports_broadcasts_today.json', {'schemaVersion': 1, 'date': str(DAY),
            'timezone': generator.TIMEZONE, 'broadcasts': [
                {'fixtureId': sid, 'channelGroups': ['win_sports'], 'confidence': 'confirmed', 'source': 'manual'}
                for sid in identifiers]})
        evidence = (self.root / 'sports_broadcasts_today.json').read_bytes()
        santos = fixture(identifiers[0], status='1H', time=f'{DAY}T19:30:00-03:00')
        palmeiras = fixture(identifiers[1], status='NS', time=f'{DAY}T21:30:00-03:00')
        santos['teams']['home']['name'], santos['teams']['away']['name'] = 'Santos', 'Flamengo'
        palmeiras['teams']['home']['name'], palmeiras['teams']['away']['name'] = 'Palmeiras', 'Bahia'
        santos['goals'] = {'home': 0, 'away': 2}
        for minute, live in ((20, False), (40, True), (50, True)):
            if minute >= 40:
                palmeiras['fixture']['status']['short'] = '1H'
                palmeiras['goals'] = {'home': 1, 'away': 0}
            with patch.object(generator, 'ROOT', self.root), \
                    patch.object(generator, 'fetch_fixtures', return_value=response(santos, palmeiras)) as fetch, \
                    patch.dict('os.environ', {'GITHUB_EVENT_NAME': 'schedule'}), contextlib.redirect_stdout(io.StringIO()):
                result = generator.generate(DAY, self.output, now=NOW.replace(hour=21, minute=minute), live_only=live)[0]
            fetch.assert_called_once()
            events = {e['id']: e for e in section_events(result)}
            self.assertEqual(minute == 20, bool(events['fixture-1492398'].get('channels')))
            self.assertEqual([101], [c['streamId'] for c in events['fixture-1492396']['channels']])
            self.assertEqual((0, 2), (events['fixture-1492398']['homeScore'], events['fixture-1492398']['awayScore']))
            self.assertEqual('LIVE', events['fixture-1492398']['status'])
            self.assertEqual(evidence, (self.root / 'sports_broadcasts_today.json').read_bytes())

    def test_offline_channel_regeneration_cannot_restore_finished_playback(self):
        self.run_generation('FT', NOW.replace(hour=21, minute=0))
        with patch.object(generator, 'ROOT', self.root), contextlib.redirect_stdout(io.StringIO()):
            result, _ = generator.refresh_channels(DAY, self.output, now=NOW.replace(hour=21, minute=10))
        self.assertNotIn('channels', next(e for e in section_events(result) if e['id'] == 'fixture-100'))

    def test_legacy_finished_cache_is_sanitized_without_extra_api_call(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cached = generator.build_feed(response(fixture(100, status='FT')), DAY, NOW)[0]
        section_events(cached)[0]['channels'] = [channel()]
        self.write('sports_today.json', cached)
        with patch.object(generator, 'ROOT', self.root), patch.dict('os.environ', {'GITHUB_EVENT_NAME': 'schedule'}), \
                patch.object(generator, 'fetch_fixtures', side_effect=AssertionError('No API needed')) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(DAY, self.output, now=NOW, live_only=True)[0]
            self.assertNotIn('channels', section_events(result)[0])
            saved = self.output.read_bytes()
            with self.assertRaises(generator.RefreshSkipped):
                generator.generate(DAY, self.output, now=NOW, live_only=True)
            self.assertEqual(saved, self.output.read_bytes())
        fetch.assert_not_called()
