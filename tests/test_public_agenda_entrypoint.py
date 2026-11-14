"""The production CLI stays public-only while retaining existing publication diagnostics."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import io
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from scripts import generate_public_agenda as public
from scripts import generate_agenda_today as legacy
from scripts import generate_official_agenda as official
from scripts.broadcaster_sources import ZONE


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 14, 12, tzinfo=ZONE)


def feed(day=NOW.date()):
    return {'date': day.isoformat(), 'sections': [{'events': [{'id': 'fixture-7001'}]}]}


def report(**changes):
    value = {'state': 'DEGRADED', 'publicRequests': 2, 'sportsApiRequests': 0,
             'sources': [{'source': 'lpf-primera', 'state': 'ERROR', 'reason': 'DiscoveryError'}],
             'guides': [{'source': 'futbol-en-vivo-ar', 'state': 'OK', 'fixtures': 1}]}
    value.update(changes)
    return value


class PublicAgendaEntrypointTests(unittest.TestCase):
    def invoke(self, result, args=()):
        output = io.StringIO()
        with patch.object(public, 'datetime') as clock, \
                patch.object(public, 'generate_official_agenda', return_value=result) as engine, \
                redirect_stdout(output):
            clock.now.return_value = NOW
            code = public.main(list(args))
        return code, output.getvalue(), engine

    def test_fresh_package_import_does_not_load_api_adapters_or_router(self):
        original = {name: module for name, module in sys.modules.items()
                    if name == 'scripts' or name.startswith('scripts.')}
        # Remove the package as well: cached package attributes must not hide imports.
        # Restore every original module object so other tests retain class identity.
        with patch.dict(sys.modules):
            for name in original:
                del sys.modules[name]
            fresh = importlib.import_module('scripts.generate_public_agenda')
            self.assertIsNot(fresh, public)
            loaded_legacy = [name for name in (
                'scripts.football_data_provider', 'scripts.competition_source_router',
                'scripts.generate_agenda_today', 'scripts.lpf_official_provider') if name in sys.modules]
        self.assertEqual([], loaded_legacy)
        for name, module in original.items():
            self.assertIs(module, sys.modules[name])

    def test_no_flags_always_selects_official_engine_with_current_art_date(self):
        code, output, engine = self.invoke((feed(), report(), True))
        self.assertEqual(0, code)
        engine.assert_called_once_with(public.generator.ROOT, public.generator.REAL_OUTPUT,
                                       NOW.date(), NOW)
        self.assertIn('SPORTS_AGENDA PUBLIC requests=2 sportsApiRequests=0', output)
        self.assertIn('requestedDate=2026-11-14 availableDate=2026-11-14', output)

    def test_output_argument_uses_same_official_publication_engine(self):
        target = Path('isolated-output.json')
        code, _, engine = self.invoke((feed(), report(), False), ['--output', str(target)])
        self.assertEqual(0, code)
        engine.assert_called_once_with(public.generator.ROOT, target, NOW.date(), NOW)

    def test_current_last_good_feed_preserves_existing_success_contract(self):
        value = feed()
        code, output, _ = self.invoke((value, report(state='KEEP_LAST_GOOD', reason='PUBLIC_SOURCES_UNAVAILABLE'), False))
        self.assertEqual(0, code)
        self.assertIn('state=KEEP_LAST_GOOD changed=false', output)
        self.assertNotIn('::error', output)
        self.assertEqual('2026-11-14', value['date'])

    def test_stale_last_good_feed_remains_failure_and_reports_both_dates(self):
        code, output, _ = self.invoke((feed(NOW.date() - timedelta(days=1)),
                                      report(state='KEEP_LAST_GOOD', reason='NO_VALID_SOURCE'), False))
        self.assertEqual(1, code)
        self.assertIn('::error title=Sports agenda unavailable::requestedDate=2026-11-14 '
                      'availableDate=2026-11-13 reason=NO_VALID_SOURCE', output)

    def test_missing_feed_remains_failure(self):
        code, output, _ = self.invoke((None, report(state='KEEP_LAST_GOOD', reason='NO_CURRENT_FEED'), False))
        self.assertEqual(1, code)
        self.assertIn('availableDate=none', output)

    def test_public_and_compatibility_clis_keep_identical_safe_annotations(self):
        result = (feed(), report(), True)
        new_code, new_output, _ = self.invoke(result)
        old_output = io.StringIO()
        with patch.object(legacy, 'datetime') as clock, \
                patch.object(official, 'generate_official_agenda', return_value=result), \
                patch.object(legacy, 'generate_agenda', side_effect=AssertionError('Legacy API must not run')), \
                redirect_stdout(old_output):
            clock.now.return_value = NOW
            old_code = legacy.main(['--official-sites'])
        self.assertEqual(old_code, new_code)
        self.assertEqual(old_output.getvalue(), new_output)
        self.assertIn('::warning title=Sports agenda source::provider=lpf-primera reason=DiscoveryError', new_output)
        self.assertNotIn('http://', new_output)
        self.assertNotIn('https://', new_output)

    def test_public_cli_rejects_provider_switch_before_any_generation(self):
        with patch.object(public, 'generate_official_agenda') as engine, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            public.main(['--provider', 'football-data'])
        self.assertEqual(2, error.exception.code)
        engine.assert_not_called()

    def test_general_workflow_restores_only_current_public_caches(self):
        workflow = (ROOT / '.github/workflows/update-sports-today.yml').read_text(encoding='utf8')
        self.assertIn('run: python -m scripts.generate_public_agenda', workflow)
        self.assertNotIn('sports_broadcast_discovery_cache.json', workflow)
        self.assertNotIn('sports_agenda_request_budget.json', workflow)
        self.assertIn('official_source_cache.json', workflow)
        self.assertIn('argentina_guide_cache.json', workflow)
        self.assertIn('run: python -m scripts.generate_sports_match_updates', workflow)


if __name__ == '__main__':
    unittest.main()
