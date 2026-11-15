"""Offline integration checks for scope isolation, budgets and source conflicts."""
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.broadcaster_sources import instant, norm
from scripts import sports_results_coordinator as coordinator
from scripts.sports_channel_rules import load_clean_groups
from scripts.sports_match_updates import (
    MatchUpdatesError, SourceMatch, TIMEZONE, fixture_key, merge_updates, reserve_request,
)
from scripts.sports_results_sources import compare_results, source_urls


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 13)
NOW = instant('2026-11-13T17:00:00-03:00')
COMPETITIONS = {'UEFA Nations League', 'Amistosos de selecciones', 'Primera Nacional', 'Copa Argentina'}


def event(identifier='fixture-1', home='Moldavia', away='Kazajiztan', start='14:00',
          competition='UEFA Nations League', channels=True):
    value = {'id': identifier, 'sport': 'football', 'competition': competition,
             'homeTeam': home, 'awayTeam': away, 'startTime': start, 'status': 'SCHEDULED'}
    if channels:
        value['channels'] = [{'streamId': 167697, 'name': 'Existing catalog signal', 'enabled': True}]
    return value


def source(event, provider='ole-opta', status='LIVE', home_score=1, away_score=0,
           identifier=None, now=NOW):
    identifier = identifier or 'raw' + event['id'].split('-')[-1]
    return SourceMatch(identifier, event['competition'], event['homeTeam'], event['awayTeam'],
        instant(f"{DAY.isoformat()}T{event['startTime']}:00-03:00"), status, now,
        home_score, away_score, provider=provider, source_url=source_urls()[provider])


class FakeTransport:
    def __init__(self, failures=()):
        self.failures, self.calls = set(failures), []

    def fetch(self, provider, day):
        self.calls.append((provider, day))
        if provider in self.failures:
            raise MatchUpdatesError('source_unavailable')
        return provider.encode('ascii')


class SplitResultsCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for filename in ('sports_agenda_guides.json', 'mbox_sports_channels_clean.json'):
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.guides = json.loads((self.root / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        self.groups = load_clean_groups(self.root / 'mbox_sports_channels_clean.json', norm)
        self.output = self.root / 'sports_match_updates.json'
        self.state = self.root / 'sports_match_updates_state.json'
        self.confirmed = event()
        self.informative = event('fixture-2', 'Singapur', 'Paraguay', '09:00',
                                 'Amistosos de selecciones', False)
        self.events = [self.confirmed, self.informative]
        self.write_feed()
        self.rows = {}
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(coordinator, 'source_competitions',
            return_value={provider: set(COMPETITIONS) for provider in source_urls()}))
        stack.enter_context(patch.object(coordinator, 'parse_provider',
            side_effect=lambda provider, raw, day, now: self.rows.get(provider, [])))

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=3) + '\n', encoding='utf8')

    def write_feed(self, events=None, day=DAY):
        self.agenda = {'date': day.isoformat(), 'timezone': TIMEZONE,
                       'sections': [{'events': self.events if events is None else events}]}
        self.write(self.root / 'sports_today.json', self.agenda)

    def seed(self, rows):
        value, _ = merge_updates(self.agenda, DAY, NOW, rows, self.guides, self.groups)
        self.write(self.output, value)
        return value

    def refresh(self, scope, rows=None, now=NOW, failures=()):
        self.rows = rows or {}
        transport = FakeTransport(failures)
        value, report = coordinator.refresh_scope(self.root, self.output, DAY, now, scope,
                                                   transport=transport)
        return value, report, transport

    def ledger(self):
        return json.loads(self.state.read_text(encoding='utf8'))

    def test_confirmed_scope_updates_live_but_preserves_informative_final(self):
        old = self.seed([source(self.confirmed), source(self.informative, status='FINISHED')])
        before_feed = (self.root / 'sports_today.json').read_bytes()
        later = NOW + timedelta(minutes=5)
        rows = [source(self.confirmed, home_score=2, now=later),
                source(self.informative, status='FINISHED', home_score=9, now=later)]
        value, report, _ = self.refresh('confirmed', {'ole-opta': rows}, now=later)
        self.assertEqual(1, report['eligible'])
        self.assertEqual(2, value['matches']['fixture-1']['homeScore'])
        self.assertEqual(old['matches']['fixture-2'], value['matches']['fixture-2'])
        self.assertEqual(before_feed, (self.root / 'sports_today.json').read_bytes())

    def test_informative_scope_updates_final_but_preserves_confirmed_live(self):
        old = self.seed([source(self.confirmed)])
        later = NOW + timedelta(minutes=5)
        rows = [source(self.confirmed, home_score=9, now=later),
                source(self.informative, status='FINISHED', home_score=2, away_score=3, now=later)]
        value, report, _ = self.refresh('informative', {'ole-opta': rows}, now=later)
        self.assertEqual(1, report['eligible'])
        self.assertEqual(old['matches']['fixture-1'], value['matches']['fixture-1'])
        self.assertEqual('FINISHED', value['matches']['fixture-2']['status'])
        self.assertEqual((2, 3), (value['matches']['fixture-2']['homeScore'],
                                  value['matches']['fixture-2']['awayScore']))

    def test_informative_ongoing_and_incomplete_final_stay_informative(self):
        for status, scores in [('LIVE', (1, 0)), ('FINISHED', (None, None))]:
            with self.subTest(status=status):
                if self.state.exists():
                    self.state.unlink()
                value, _, _ = self.refresh('informative', {'ole-opta': [
                    source(self.informative, status=status, home_score=scores[0], away_score=scores[1])]})
                self.assertNotIn('fixture-2', value['matches'])

    def test_confirmed_final_without_score_is_not_zero_zero(self):
        value, _, _ = self.refresh('confirmed', {'ole-opta': [source(self.confirmed,
            status='FINISHED', home_score=None, away_score=None)]})
        self.assertEqual('FINISHED', value['matches']['fixture-1']['status'])
        self.assertNotIn('homeScore', value['matches']['fixture-1'])
        self.assertNotIn('awayScore', value['matches']['fixture-1'])

    def test_confirmed_repeated_slot_skips_network_and_next_five_minute_slot_runs(self):
        self.refresh('confirmed', {'ole-opta': [source(self.confirmed)]})
        _, report, transport = self.refresh('confirmed', now=NOW + timedelta(minutes=4))
        self.assertTrue(report['budgetSkipped'])
        self.assertEqual([], transport.calls)
        self.assertEqual(1, self.ledger()['scopes']['confirmed']['cycles'])
        _, report, transport = self.refresh('confirmed', now=NOW + timedelta(minutes=5))
        self.assertEqual(3, report['requests'])
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(2, self.ledger()['scopes']['confirmed']['cycles'])

    def test_informative_gate_persists_until_next_two_hour_slot(self):
        start = NOW.replace(hour=16)
        self.refresh('informative', now=start)
        before_state = self.state.read_bytes()
        _, report, transport = self.refresh('informative', now=start + timedelta(hours=1, minutes=59))
        self.assertTrue(report['budgetSkipped'])
        self.assertEqual([], transport.calls)
        self.assertEqual(before_state, self.state.read_bytes())
        _, report, transport = self.refresh('informative', now=start + timedelta(hours=2))
        self.assertEqual(3, report['requests'])
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(2, self.ledger()['scopes']['informative']['cycles'])

    def test_scopes_have_independent_slots_and_failed_attempt_counters(self):
        providers = tuple(source_urls())
        _, report, _ = self.refresh('confirmed', failures=providers)
        self.assertEqual(3, report['requests'])
        self.assertTrue(report['sourceFailed'])
        _, report, _ = self.refresh('informative', failures=providers)
        self.assertEqual(3, report['requests'])
        value = self.ledger()
        for scope in ('confirmed', 'informative'):
            self.assertEqual(1, value['scopes'][scope]['cycles'])
            self.assertEqual(dict.fromkeys(providers, 1), value['scopes'][scope]['requests'])
        self.assertIs(coordinator.validate_ledger(value), value)

    def test_provider_selection_skips_competitions_a_source_does_not_cover(self):
        registry = {provider: set(COMPETITIONS) for provider in source_urls()}
        registry['tyc-results'] = {'Primera Nacional'}
        with patch.object(coordinator, 'source_competitions', return_value=registry):
            _, report, transport = self.refresh('confirmed')
        self.assertEqual({'ole-opta', 'canchallena'}, {provider for provider, _ in transport.calls})
        self.assertEqual(2, report['requests'])
        self.assertEqual(0, self.ledger()['scopes']['confirmed']['requests']['tyc-results'])

    def test_invalid_persisted_budget_fails_before_network_or_rewriting(self):
        self.seed([source(self.confirmed)])
        before = self.output.read_bytes()
        ledger = coordinator.reserve_cycle(None, DAY, NOW, 'confirmed')
        ledger['scopes']['confirmed']['requests']['ole-opta'] = 2
        self.write(self.state, ledger)
        transport = FakeTransport()
        with self.assertRaises(MatchUpdatesError):
            coordinator.refresh_scope(self.root, self.output, DAY, NOW + timedelta(minutes=5),
                                      'confirmed', transport=transport)
        self.assertEqual([], transport.calls)
        self.assertEqual(before, self.output.read_bytes())

    def test_independent_daily_cycle_caps_are_enforced_before_network(self):
        for scope, limit in [('confirmed', 288), ('informative', 12)]:
            with self.subTest(scope=scope):
                ledger = coordinator.reserve_cycle(None, DAY, NOW, scope)
                ledger['scopes'][scope]['cycles'] = limit
                self.assertIsNone(coordinator.reserve_cycle(ledger, DAY, NOW + timedelta(hours=2), scope))
                other = 'informative' if scope == 'confirmed' else 'confirmed'
                self.assertIsNotNone(coordinator.reserve_cycle(ledger, DAY, NOW, other))

    def test_legacy_budget_and_settled_identities_survive_scope_migration(self):
        legacy = reserve_request(None, DAY, NOW)
        legacy['requests'] = 7
        legacy['settled']['fixture-2'] = ['raw2', *fixture_key(self.informative, DAY, self.guides)]
        original = json.loads(json.dumps(legacy))
        migrated = coordinator.ledger_for_day(legacy, DAY)
        self.assertEqual(original, migrated['legacy'])
        self.assertEqual(original, legacy)
        for scope in ('confirmed', 'informative'):
            self.assertEqual(['ole-opta', *legacy['settled']['fixture-2']],
                migrated['scopes'][scope]['settled']['fixture-2'])
        self.write(self.state, legacy)
        _, report, transport = self.refresh('confirmed', now=NOW + timedelta(minutes=5))
        self.assertEqual(3, report['requests'])
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(original, self.ledger()['legacy'])
        self.assertEqual(1, self.ledger()['scopes']['confirmed']['requests']['ole-opta'])
        _, report, transport = self.refresh('informative', now=NOW + timedelta(minutes=5))
        self.assertTrue(report['budgetSkipped'])
        self.assertEqual([], transport.calls)

    def test_legacy_last_attempt_keeps_four_minute_guard_across_new_slot(self):
        legacy = reserve_request(None, DAY, NOW + timedelta(minutes=4))
        self.write(self.state, legacy)
        _, report, transport = self.refresh('confirmed', now=NOW + timedelta(minutes=5))
        self.assertTrue(report['budgetSkipped'])
        self.assertEqual([], transport.calls)
        self.assertEqual(legacy, self.ledger())
        _, report, transport = self.refresh('confirmed', now=NOW + timedelta(minutes=8))
        self.assertEqual(3, report['requests'])
        self.assertEqual(3, len(transport.calls))
        self.assertEqual(legacy, self.ledger()['legacy'])

    def test_legacy_requests_count_against_same_provider_daily_ceiling(self):
        legacy = reserve_request(None, DAY, NOW.replace(hour=8))
        legacy['requests'] = 120
        ledger = coordinator.ledger_for_day(legacy, DAY)
        scope = ledger['scopes']['confirmed']
        scope.update(cycles=180, lastAttemptAt='2026-11-13T16:55:00-03:00', slot=203)
        scope['requests']['ole-opta'] = 180
        self.write(self.state, ledger)
        _, report, transport = self.refresh('confirmed')
        self.assertTrue(report['budgetSkipped'])
        self.assertEqual({'canchallena', 'tyc-results'}, {provider for provider, _ in transport.calls})
        self.assertEqual(2, report['requests'])
        self.assertEqual(180, self.ledger()['scopes']['confirmed']['requests']['ole-opta'])
        self.assertEqual(120, self.ledger()['legacy']['requests'])

    def test_previous_day_legacy_ledger_starts_a_clean_new_day_only(self):
        legacy = reserve_request(None, DAY, NOW)
        next_day = coordinator.ledger_for_day(legacy, DAY + timedelta(days=1))
        self.assertNotIn('legacy', next_day)
        self.assertEqual((DAY + timedelta(days=1)).isoformat(), next_day['date'])
        for scope in next_day['scopes'].values():
            self.assertEqual(0, scope['cycles'])
            self.assertTrue(all(count == 0 for count in scope['requests'].values()))

    def test_no_pending_future_or_unsupported_fixture_makes_no_request(self):
        cases = [[], [event(start='20:00')], [event(competition='LaLiga')]]
        for events in cases:
            with self.subTest(events=events):
                self.write_feed(events)
                _, report, transport = self.refresh('confirmed')
                self.assertEqual(0, report['pending'])
                self.assertEqual(0, report['requests'])
                self.assertEqual([], transport.calls)
                self.assertFalse(self.state.exists())

    def test_old_agenda_does_not_query_or_rewrite_last_good_files(self):
        self.seed([source(self.confirmed)])
        before = self.output.read_bytes()
        self.write_feed(day=DAY - timedelta(days=1))
        value, report, transport = self.refresh('confirmed')
        self.assertTrue(report['agendaUnavailable'])
        self.assertEqual([], transport.calls)
        self.assertEqual(before, self.output.read_bytes())
        self.assertEqual(DAY.isoformat(), value['date'])

    def test_all_sources_fail_preserves_exact_last_good_bytes_and_freshness(self):
        old = self.seed([source(self.confirmed)])
        before = self.output.read_bytes()
        value, report, _ = self.refresh('confirmed', now=NOW + timedelta(minutes=5),
                                        failures=tuple(source_urls()))
        self.assertEqual(old, value)
        self.assertEqual(before, self.output.read_bytes())
        self.assertFalse(report['changed'])
        self.assertTrue(report['sourceFailed'])

    def test_one_failed_source_does_not_block_exact_fallback_from_another(self):
        value, report, _ = self.refresh('confirmed', {'canchallena': [
            source(self.confirmed, provider='canchallena', identifier='differentrawid')]},
            failures=('ole-opta',))
        self.assertEqual('canchallena', value['matches']['fixture-1']['source']['provider'])
        self.assertEqual('differentrawid', value['matches']['fixture-1']['source']['matchId'])
        self.assertEqual(1, report['matched'])
        self.assertIn('ole-opta', report['sourcesFailed'])

    def test_cross_source_conflict_preserves_exact_last_good_bytes(self):
        old = self.seed([source(self.confirmed)])
        before = self.output.read_bytes()
        later = NOW + timedelta(minutes=5)
        value, report, _ = self.refresh('confirmed', {
            'ole-opta': [source(self.confirmed, home_score=2, now=later)],
            'canchallena': [source(self.confirmed, provider='canchallena', home_score=3, now=later)]}, now=later)
        self.assertEqual(1, report['conflicts'])
        self.assertEqual(old, value)
        self.assertEqual(before, self.output.read_bytes())
        self.assertFalse(report['changed'])

    def test_cross_source_live_final_disagreement_is_not_a_final(self):
        value, report, _ = self.refresh('confirmed', {
            'ole-opta': [source(self.confirmed)],
            'canchallena': [source(self.confirmed, provider='canchallena', status='FINISHED')]})
        self.assertEqual(1, report['conflicts'])
        self.assertNotIn('fixture-1', value['matches'])

    def test_independent_source_ids_can_corroborate_same_exact_fixture(self):
        value, report, _ = self.refresh('confirmed', {
            'ole-opta': [source(self.confirmed, identifier='ole123')],
            'canchallena': [source(self.confirmed, provider='canchallena', identifier='cancha789')]})
        self.assertEqual(1, report['corroborated'])
        self.assertEqual(1, report['matched'])
        self.assertIn('fixture-1', value['matches'])

    def test_same_provider_different_raw_ids_for_one_fixture_are_quarantined(self):
        value, report, _ = self.refresh('confirmed', {'ole-opta': [
            source(self.confirmed, identifier='111'), source(self.confirmed, identifier='222')]})
        self.assertEqual(1, report['conflicts'])
        self.assertNotIn('fixture-1', value['matches'])

    def test_same_provider_raw_id_cannot_identify_two_different_fixtures(self):
        rows = [source(self.confirmed, identifier='123'),
                source(self.informative, identifier='123', status='FINISHED')]
        comparison = compare_results(rows, self.guides, NOW)
        self.assertEqual((), comparison.rows)
        self.assertGreater(comparison.conflicts, 0)

    def test_same_raw_id_at_different_providers_is_not_shared_identity(self):
        old = self.seed([source(self.confirmed, identifier='123')])
        later = NOW + timedelta(minutes=5)
        value, _, _ = self.refresh('confirmed', {'canchallena': [
            source(self.confirmed, provider='canchallena', identifier='123', home_score=2, now=later)]}, now=later)
        self.assertEqual(old['matches']['fixture-1']['fixtureKey'], value['matches']['fixture-1']['fixtureKey'])
        self.assertEqual('canchallena', value['matches']['fixture-1']['source']['provider'])
        self.assertEqual(2, value['matches']['fixture-1']['homeScore'])

    def test_existing_raw_identity_cannot_silently_change_within_provider(self):
        old = self.seed([source(self.confirmed, identifier='123')])
        later = NOW + timedelta(minutes=5)
        value, _, _ = self.refresh('confirmed', {'ole-opta': [
            source(self.confirmed, identifier='999', home_score=2, now=later)]}, now=later)
        self.assertEqual(old, value)

    def test_final_card_never_reopens_after_later_live_observation(self):
        old = self.seed([source(self.confirmed, status='FINISHED')])
        before = self.output.read_bytes()
        later = NOW + timedelta(minutes=5)
        value, _, _ = self.refresh('confirmed', {'ole-opta': [source(self.confirmed, now=later)]}, now=later)
        self.assertEqual(old, value)
        self.assertEqual(before, self.output.read_bytes())

    def test_cross_provider_same_live_facts_without_timestamp_cannot_extend_expiry(self):
        old = self.seed([source(self.confirmed)])
        before = self.output.read_bytes()
        later = NOW + timedelta(minutes=5)
        unknown_age = replace(source(self.confirmed, provider='canchallena', now=later),
                              source_updated=None)
        value, _, _ = self.refresh('confirmed', {'canchallena': [unknown_age]}, now=later)
        self.assertEqual(old, value)
        self.assertEqual(before, self.output.read_bytes())
        self.assertEqual(old['matches']['fixture-1']['expiresAt'], value['matches']['fixture-1']['expiresAt'])

    def test_cross_provider_explicit_final_can_close_known_live_without_source_timestamp(self):
        self.seed([source(self.confirmed)])
        later = NOW + timedelta(minutes=5)
        final = replace(source(self.confirmed, provider='canchallena', status='FINISHED', now=later),
                        source_updated=None)
        value, _, _ = self.refresh('confirmed', {'canchallena': [final]}, now=later)
        self.assertEqual('FINISHED', value['matches']['fixture-1']['status'])
        self.assertEqual('canchallena', value['matches']['fixture-1']['source']['provider'])

    def test_verified_informative_final_stops_later_result_queries(self):
        self.seed([source(self.informative, status='FINISHED')])
        value, report, transport = self.refresh('informative', now=NOW + timedelta(hours=2))
        self.assertEqual('FINISHED', value['matches']['fixture-2']['status'])
        self.assertEqual(0, report['pending'])
        self.assertEqual([], transport.calls)

    def test_confirmed_final_stops_query_after_a_later_matching_observation(self):
        self.seed([source(self.confirmed, status='FINISHED')])
        later = NOW + timedelta(minutes=5)
        self.refresh('confirmed', {'ole-opta': [source(self.confirmed, status='FINISHED')]}, now=later)
        _, report, transport = self.refresh('confirmed', now=later + timedelta(minutes=5))
        self.assertEqual(0, report['pending'])
        self.assertEqual([], transport.calls)

    def test_wrong_team_kickoff_day_or_home_away_order_cannot_attach_result(self):
        good = source(self.confirmed)
        wrong = [replace(good, home='Moldavia U20'), replace(good, home=good.away, away=good.home),
                 replace(good, kickoff=good.kickoff + timedelta(minutes=1)),
                 replace(good, kickoff=good.kickoff - timedelta(days=1))]
        for row in wrong:
            with self.subTest(row=row):
                if self.state.exists():
                    self.state.unlink()
                value, report, _ = self.refresh('confirmed', {'ole-opta': [row]})
                self.assertNotIn('fixture-1', value['matches'])
                self.assertEqual(0, report['matched'])

    def test_future_or_stale_live_observation_cannot_renew_label(self):
        good = source(self.confirmed)
        rows = [replace(good, source_updated=NOW + timedelta(seconds=1)),
                replace(good, source_updated=NOW - timedelta(minutes=16))]
        for row in rows:
            comparison = compare_results([row], self.guides, NOW)
            self.assertEqual((), comparison.rows)

    def test_partial_invalid_score_and_unapproved_source_cannot_enter_comparison(self):
        good = source(self.confirmed)
        invalid = [replace(good, home_score=None), replace(good, home_score=True),
                   replace(good, away_score=-1), replace(good, provider='search-snippet'),
                   replace(good, source_url='https://example.invalid/')]
        for row in invalid:
            with self.subTest(row=row):
                self.assertEqual((), compare_results([row], self.guides, NOW).rows)


if __name__ == '__main__':
    unittest.main()
