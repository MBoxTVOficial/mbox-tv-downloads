import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import generate_sports_today as generator


class ApiErrorDiagnosticsTest(unittest.TestCase):
    def capture(self, errors):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(generator.GenerationError):
            generator.validate_api_response({'errors': errors})
        return out.getvalue()

    def test_annotation_uses_only_sanitized_text(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'API_FOOTBALL_KEY': 'test-secret'}):
            log = self.capture({'requests': 'test-secret limit reached'})
        self.assertIn('::error title=API-Football diagnostics::', log)
        self.assertNotIn('test-secret', log)

    def test_dict(self):
        log = self.capture({'requests': 'Daily request limit reached.'})
        self.assertIn('type=requests', log)
        self.assertIn('Daily request limit reached.', log)

    def test_list(self):
        self.assertIn('message="Limit reached"', self.capture(['Limit reached']))

    def test_string(self):
        self.assertIn('message="Subscription expired"', self.capture('Subscription expired'))

    def test_unexpected(self):
        for value in [None, 123, True, {'requests': {'private': 'do not print'}}, ['text', 1]]:
            with self.subTest(value=value):
                self.assertEqual('SPORTS_REALTIME API_ERROR type=unknown\n', self.capture(value))

    def test_secrets_urls_headers_and_unknown_keys_never_print(self):
        key = 'sensitive-test-api-key'
        token = 'another-secret-token'
        with patch.dict(os.environ, {'API_FOOTBALL_KEY': key, 'GITHUB_TOKEN': token}):
            log = self.capture({key: f'{key} {token} Authorization: Bearer abcdefghijklmnopqrstuvwxyz https://private.example/path?password=hidden'})
        for secret in [key, token, 'abcdefghijklmnopqrstuvwxyz', 'private.example', 'password=hidden']:
            self.assertNotIn(secret, log)
        self.assertIn('type=unknown', log)

    def test_control_characters_and_oversize_are_unknown(self):
        for value in ['oops\nsecret', 'x' * 4097]:
            self.assertEqual('SPORTS_REALTIME API_ERROR type=unknown\n', self.capture(value))

    def test_empty_errors_remain_success(self):
        for value in [{}, []]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual([], generator.validate_api_response({'errors': value, 'results': 0, 'response': []}))
            self.assertEqual('', out.getvalue())

    def test_previous_feed_preserved_and_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'sports_today.json'
            original = b'previous feed bytes'
            output.write_bytes(original)
            with patch.object(generator, 'daily_context', return_value=(None, {})), patch.object(generator, 'fetch_fixtures', return_value={'errors': {'subscription': 'Subscription expired'}}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, generator.main(['--output', str(output), '--date', '2026-10-10']))
            self.assertEqual(original, output.read_bytes())


if __name__ == '__main__':
    unittest.main()
