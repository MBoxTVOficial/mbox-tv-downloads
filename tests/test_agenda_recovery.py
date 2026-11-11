"""Offline regressions for independent agenda sources and stale-feed failures."""
import contextlib
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import generate_agenda_today as agenda
from scripts import generate_sports_today as generator
from scripts.football_data_provider import ProviderError


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 10)


def row(name='Liga Profesional Argentina', identifier='lg_22EEYKR'):
    return {'competition': name, 'competitionId': identifier,
            'providerFixtureId': 'offline:' + identifier,
            'homeTeam': 'Club Azul', 'awayTeam': 'Club Verde',
            'startTime': DAY.isoformat() + 'T19:00:00-03:00'}


class AgendaRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'tests/data').mkdir(parents=True)
        for name in ('sports_agenda_providers.json', 'tests/data/football_data_validation.json',
                     'tests/data/lpf_agenda_validation.json'):
            shutil.copyfile(ROOT / name, self.root / name)
        self.config = agenda.validated_config(self.root)
        self.output = self.root / 'sports_today.json'
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_argentina_failure_does_not_block_four_european_routes(self):
        argentina = Mock()
        argentina.matches.side_effect = ProviderError('ARGENTINA_REQUEST_FAILED')
        europe = Mock()
        names = {'PD': 'LaLiga', 'PL': 'Premier League', 'SA': 'Serie A', 'FL1': 'Ligue 1'}
        europe.matches.side_effect = lambda code, day: [row(names[code], code)]
        diagnostics = {}
        rows = agenda.acquire(self.root, DAY, self.config,
                              {'football_soccer_argentina': argentina, 'football_data': europe},
                              diagnostics=diagnostics)
        self.assertEqual(set(names.values()), {item['competition'] for item in rows})
        self.assertEqual(4, europe.matches.call_count)
        failure = next(item for item in diagnostics['sources']
                       if item['provider'] == 'football_soccer_argentina')
        self.assertEqual('ERROR', failure['state'])
        self.assertEqual('ARGENTINA_REQUEST_FAILED', failure['reason'])

    def test_missing_football_data_token_does_not_block_argentina(self):
        argentina = Mock()
        argentina.matches.return_value = [row()]
        diagnostics = {}
        rows = agenda.acquire(self.root, DAY, self.config,
                              {'football_soccer_argentina': argentina}, diagnostics=diagnostics)
        self.assertEqual(['Liga Profesional Argentina'], [item['competition'] for item in rows])
        errors = [item for item in diagnostics['sources'] if item['provider'] == 'football_data']
        self.assertTrue(errors)
        self.assertTrue(all(item['state'] == 'ERROR' for item in errors))
        self.assertTrue(all(item['reason'] == 'TOKEN_NOT_AVAILABLE' for item in errors))

    def test_http_403_remains_a_safe_diagnostic_code(self):
        argentina = Mock()
        argentina.matches.return_value = [row()]
        europe = Mock()
        europe.matches.side_effect = ProviderError('HTTP_403')
        diagnostics = {}
        rows = agenda.acquire(self.root, DAY, self.config,
                              {'football_soccer_argentina': argentina, 'football_data': europe},
                              diagnostics=diagnostics)
        self.assertEqual(1, len(rows))
        errors = [item for item in diagnostics['sources'] if item['state'] == 'ERROR']
        self.assertEqual(4, len(errors))
        self.assertTrue(all(item['reason'] == 'HTTP_403' for item in errors))

    def test_unknown_provider_error_never_exposes_its_text(self):
        secret = 'synthetic-secret-not-for-diagnostics'
        argentina = Mock()
        argentina.matches.side_effect = ProviderError(secret)
        europe = Mock()
        europe.matches.return_value = []
        diagnostics = {}
        agenda.acquire(self.root, DAY, self.config,
                       {'football_soccer_argentina': argentina, 'football_data': europe},
                       diagnostics=diagnostics)
        self.assertNotIn(secret, json.dumps(diagnostics))
        today = datetime.now(generator.argentina_timezone()).date().isoformat()
        report = {'state': 'KEEP_LAST_GOOD', 'reason': 'ProviderError', **diagnostics}
        captured = io.StringIO()
        with patch.object(agenda, 'generate_agenda', return_value=({'date': today}, report, False)), \
                contextlib.redirect_stdout(captured):
            self.assertEqual(0, agenda.main([]))
        self.assertNotIn(secret, captured.getvalue())

    def test_stale_last_good_causes_failure_and_preserves_previous_bytes(self):
        old_day = date(2026, 10, 10)
        old_feed, _ = generator.build_feed({'errors': [], 'response': [], 'results': 0}, old_day,
                                          datetime(2026, 10, 10, 12, tzinfo=generator.argentina_timezone()))
        self.output.write_text(json.dumps(old_feed), encoding='utf8')
        before = self.output.read_bytes()
        today = datetime.now(generator.argentina_timezone()).date().isoformat()
        captured = io.StringIO()
        with patch.object(generator, 'ROOT', self.root), \
                patch.object(agenda, 'acquire', side_effect=ProviderError('HTTP_403')), \
                contextlib.redirect_stdout(captured):
            result = agenda.main(['--output', str(self.output)])
        self.assertEqual(1, result)
        self.assertEqual(before, self.output.read_bytes())
        self.assertIn('requestedDate=' + today, captured.getvalue())
        self.assertIn('availableDate=2026-10-10', captured.getvalue())
        self.assertIn('::error ', captured.getvalue())

    def test_current_last_good_can_succeed(self):
        today = datetime.now(generator.argentina_timezone()).date().isoformat()
        report = {'state': 'KEEP_LAST_GOOD', 'reason': 'HTTP_403', 'sources': [], 'requests': {}}
        with patch.object(agenda, 'generate_agenda', return_value=({'date': today}, report, False)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, agenda.main([]))


if __name__ == '__main__':
    unittest.main()
