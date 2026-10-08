"""Daily assignments survive new general/LIVE data; no real network or cached channels."""
import ast
import contextlib
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_sports_today as generator
from scripts import sports_daily_broadcasts as daily
from scripts.sports_channel_rules import load_clean_groups
from test_generate_sports_today import DAY, NOW, fixture, response


class DailyRegenerationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / 'sports_today.json'
        self.config_path = self.root / 'sports_broadcasts_today.json'
        self.clean_path = self.root / 'mbox_sports_channels_clean.json'
        self.fixture_groups = ['a', 'b', 'c', 'a', 'd', 'c']
        self.counts = [2, 1, 3, 2, 2, 3]
        self.clean = {'schemaVersion': 1, 'groups': [
            {'id': name, 'canonicalName': name.upper(), 'aliases': [name.upper()+' alias'], 'streams': [
                {'streamId': 10 * index + number, 'name': name+' stream '+str(number),
                 'priority': number, 'enabled': True, 'aliases': []} for number in range(1, count+1)]}
            for index, (name, count) in enumerate(zip('abcd', [2, 1, 3, 2]), 1)]}
        self.config = {'schemaVersion': 1, 'date': DAY.isoformat(), 'timezone': generator.TIMEZONE,
            'broadcasts': [{'fixtureId': 100+index, 'channelGroups': [group], 'confidence': 'confirmed'}
                          for index, group in enumerate(self.fixture_groups)]}
        self.write(self.clean_path, self.clean)
        self.write(self.config_path, self.config)
        self.payload = response(*[fixture(100+index, status='1H') for index in range(6)], fixture(200, status='1H'))
        self.patcher = patch.object(generator, 'ROOT', self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.no_network = patch.object(socket, 'create_connection', side_effect=AssertionError('No network'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def events(self, feed):
        return {e['id']: e for s in feed['sections'] for e in s['events']}

    def assert_channels(self, feed):
        events = self.events(feed)
        self.assertEqual(6, sum(bool(e.get('channels')) for e in events.values()))
        self.assertEqual(13, sum(len(e.get('channels', [])) for e in events.values()))
        for index, count in enumerate(self.counts):
            channels = events['fixture-'+str(100+index)]['channels']
            self.assertEqual(count, len(channels))
            self.assertEqual(list(range(1, count+1)), [c['priority'] for c in channels])
            self.assertEqual(count, len({c['streamId'] for c in channels}))
            self.assertTrue(all(c['enabled'] is True for c in channels))
        self.assertNotIn('channels', events['fixture-200'])
        self.assertEqual(1, feed['schemaVersion'])

    def run_generator(self, payload=None, live=False, manual=False, now=NOW):
        with patch.object(generator, 'fetch_fixtures', return_value=payload or self.payload) as fetch, \
                patch.dict(generator.os.environ, {'GITHUB_ACTIONS': 'true' if manual else 'false',
                                                  'GITHUB_EVENT_NAME': 'workflow_dispatch' if manual else 'schedule'}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(DAY, self.output, now=now, live_only=live)
        fetch.assert_called_once_with(DAY)
        return result

    def test_two_generations_general_then_live_restore_all_six_without_cached_channels(self):
        first = self.run_generator()[0]
        self.assert_channels(first)
        # Fresh data and the cache contain NO channels; config alone restores them.
        cached = copy.deepcopy(first)
        for event in self.events(cached).values():
            event.pop('channels', None)
        self.write(self.output, cached)
        payload = copy.deepcopy(self.payload)
        payload['response'][0]['goals'] = {'home': 2, 'away': 1}
        second = self.run_generator(payload, live=True, now=NOW+timedelta(minutes=10))[0]
        self.assert_channels(second)
        updated = self.events(second)['fixture-100']
        self.assertEqual((2, 1), (updated['homeScore'], updated['awayScore']))
        self.assertEqual('LIVE', updated['status'])
        self.assertEqual(self.events(first)['fixture-100']['channels'], updated['channels'])

    def test_no_previous_feed_required(self):
        self.assertFalse(self.output.exists())
        self.assert_channels(self.run_generator(live=True)[0])

    def test_manual_live_refresh_restores_assignments(self):
        self.assert_channels(self.run_generator(live=True, manual=True)[0])

    def test_identical_second_generation_preserves_bytes_and_updated_at(self):
        first = self.run_generator()[0]
        original = self.output.read_bytes()
        second = self.run_generator(live=True, now=NOW+timedelta(minutes=10))
        self.assert_channels(second[0])
        self.assertFalse(second[-1])
        self.assertEqual(first['updatedAt'], second[0]['updatedAt'])
        self.assertEqual(original, self.output.read_bytes())

    def test_general_runs_do_not_increase_one_request_limit(self):
        self.assert_channels(self.run_generator()[0])
        self.assert_channels(self.run_generator(now=NOW+timedelta(minutes=10))[0])
        self.assertEqual(90, generator.MAX_DAILY_API_CALLS)

    def test_offline_api_payload_same_path(self):
        source = self.root / 'input.json'
        self.write(source, self.payload)
        with patch.object(generator, 'fetch_fixtures', side_effect=AssertionError('No API')), contextlib.redirect_stdout(io.StringIO()):
            self.assert_channels(generator.generate(DAY, self.output, source, NOW)[0])

    def test_next_argentine_day_does_not_inherit_assignments(self):
        tomorrow = DAY+timedelta(days=1)
        payload = copy.deepcopy(self.payload)
        for item in payload['response']:
            item['fixture']['date'] = (tomorrow.isoformat()+'T20:00:00-03:00')
        with patch.object(generator, 'fetch_fixtures', return_value=payload), contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(tomorrow, self.output, now=NOW+timedelta(days=1))[0]
        self.assertFalse(any('channels' in e for e in self.events(result).values()))

    def test_only_confirmed_adds_channels(self):
        for confidence in ('probable', 'unknown'):
            with self.subTest(confidence=confidence):
                self.config['broadcasts'][0]['confidence'] = confidence
                self.write(self.config_path, self.config)
                result = self.run_generator()[0]
                self.assertNotIn('channels', self.events(result)['fixture-100'])

    def test_missing_daily_file_normal_generation(self):
        self.config_path.unlink()
        self.assertFalse(any('channels' in e for e in self.events(self.run_generator()[0]).values()))

    def test_unknown_group_before_api_keeps_output(self):
        self.run_generator()
        previous = self.output.read_bytes()
        self.config['broadcasts'][0]['channelGroups'] = ['missing']
        self.write(self.config_path, self.config)
        with patch.object(generator, 'fetch_fixtures', side_effect=AssertionError('No API')) as fetch:
            with self.assertRaisesRegex(generator.GenerationError, 'Unknown channelGroup'):
                generator.generate(DAY, self.output, now=NOW)
        fetch.assert_not_called()
        self.assertEqual(previous, self.output.read_bytes())

    def test_missing_base_fails_before_api(self):
        self.clean_path.unlink()
        with patch.object(generator, 'fetch_fixtures', side_effect=AssertionError('No API')):
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output, now=NOW)

    def test_invalid_daily_json_does_not_overwrite_feed(self):
        self.run_generator()
        previous = self.output.read_bytes()
        self.config_path.write_text('{', encoding='utf-8')
        with patch.object(generator, 'fetch_fixtures', side_effect=AssertionError('No API')):
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output, now=NOW)
        self.assertEqual(previous, self.output.read_bytes())

    def test_missing_fixture_in_fresh_data_preserves_output(self):
        self.run_generator()
        previous = self.output.read_bytes()
        payload = response(*self.payload['response'][1:])
        with self.assertRaisesRegex(generator.GenerationError, 'Unknown daily fixtureId'):
            self.run_generator(payload)
        self.assertEqual(previous, self.output.read_bytes())

    def test_disabled_stream_skipped_and_aliases_preserved(self):
        self.clean['groups'][0]['streams'][1]['enabled'] = False
        self.write(self.clean_path, self.clean)
        result = self.run_generator()[0]
        channels = self.events(result)['fixture-100']['channels']
        self.assertEqual(1, len(channels))
        self.assertEqual(['A alias'], channels[0]['aliases'])

    def test_multiple_groups_order_and_real_id_deduplication(self):
        self.clean['groups'][1]['streams'].append(copy.deepcopy(self.clean['groups'][0]['streams'][0]))
        self.config['broadcasts'][0]['channelGroups'] = ['a', 'b']
        self.write(self.clean_path, self.clean)
        self.write(self.config_path, self.config)
        channels = self.events(self.run_generator()[0])['fixture-100']['channels']
        self.assertEqual([11, 12, 21], [c['streamId'] for c in channels])
        self.assertEqual([1, 2, 3], [c['priority'] for c in channels])

    def test_bool_stream_id_rejected(self):
        self.clean['groups'][0]['streams'][0]['streamId'] = True
        self.write(self.clean_path, self.clean)
        with self.assertRaises(generator.GenerationError):
            self.run_generator()

    def test_duplicate_fixture_assignment_rejected(self):
        self.config['broadcasts'].append(copy.deepcopy(self.config['broadcasts'][0]))
        self.write(self.config_path, self.config)
        with self.assertRaisesRegex(generator.GenerationError, 'Duplicate daily fixtureId'):
            self.run_generator()

    def test_refresh_channels_preserves_latest_sports_metadata(self):
        before = self.run_generator()[0]
        for event in self.events(before).values():
            event.pop('channels', None)
        self.write(self.output, before)
        with patch.object(generator, 'fetch_fixtures', side_effect=AssertionError('No API')):
            result, changed = generator.refresh_channels(DAY, self.output)
        self.assertTrue(changed)
        self.assert_channels(result)
        restored = copy.deepcopy(result)
        for event in self.events(restored).values():
            event.pop('channels', None)
        self.assertEqual(before, restored)

    def test_refresh_unconfirmed_does_not_retain_old_channels(self):
        self.run_generator()
        self.config['broadcasts'][0]['confidence'] = 'unknown'
        self.write(self.config_path, self.config)
        result, _ = generator.refresh_channels(DAY, self.output)
        self.assertNotIn('channels', self.events(result)['fixture-100'])

    def test_published_daily_config_and_groups_are_valid(self):
        actual_root = Path(generator.__file__).resolve().parents[1]
        config = daily.read_broadcasts(actual_root/'sports_broadcasts_today.json')
        actual_groups = load_clean_groups(actual_root/'mbox_sports_channels_clean.json', generator.normalize)
        daily.validate_broadcasts(config)
        for item in config['broadcasts']:
            for group in item['channelGroups']:
                self.assertIn(group, actual_groups)
        self.assertNotIn('streamId', json.dumps(config))

    def test_new_production_helpers_have_no_stream_id_literals(self):
        root = Path(generator.__file__).resolve().parents[1]
        for name in ('sports_channel_rules.py', 'sports_daily_broadcasts.py'):
            tree = ast.parse((root/'scripts'/name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        self.assertFalse(isinstance(key, ast.Constant) and key.value == 'streamId' and isinstance(value, ast.Constant))


if __name__ == '__main__':
    unittest.main()
