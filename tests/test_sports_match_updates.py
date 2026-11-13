"""Hermetic source/identity/retention checks for the independent match overlay."""
import copy
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from scripts.broadcaster_sources import instant
from scripts.generate_sports_match_updates import refresh
from scripts.sports_match_updates import (
    LIVE_TTL, MAX_DAILY_REQUESTS, SOURCE_URL, TIMEZONE, MatchUpdatesError, OleBulkTransport,
    confirm_finished, decode_json, eligible_events, merge_updates, parse_ole_bulk, pending_events,
    request_body, reserve_request, validate_updates,
)


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 13)
NOW = instant('2026-11-13T17:00:00-03:00')
MOLDOVA = '1jpjop6resgb4rdrg9s6rgs9g'
SINGAPORE = '5mz4ghtu4ewty2n7g60sxyj2s'
GROUPS = {'confirmed': {'streams': [{'streamId': 1, 'enabled': True}]},
          'disabled': {'streams': [{'streamId': 2, 'enabled': False}]}}


def payload():
    return json.loads((ROOT / 'tests/fixtures/ole_match_updates_bulk_2026_11_13.json').read_text(encoding='utf8'))


def source_record(value=None, identifier=MOLDOVA):
    value = payload() if value is None else value
    return next(row for group in value['data']['fixture'] for row in group['match']
                if row['matchInfo']['id'] == identifier)


def event(identifier='fixture-1', home='Moldavia', away='Kazajiztan',
          competition='UEFA Nations League', start='14:00', channels=True):
    row = {'id': identifier, 'sport': 'football', 'competition': competition,
           'homeTeam': home, 'awayTeam': away, 'startTime': start, 'status': 'SCHEDULED'}
    if channels:
        row['channels'] = [{'streamId': 1, 'name': 'Existing configured signal', 'enabled': True}]
    return row


def feed(*events, day=DAY):
    return {'date': day.isoformat(), 'timezone': TIMEZONE,
            'sections': [{'events': list(events or (event(),))}]}


class MatchUpdatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guides = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))

    def rows(self, value=None):
        return parse_ole_bulk(payload() if value is None else value, DAY)

    def merge(self, rows=None, agenda=None, previous=None, now=NOW, **kwargs):
        return merge_updates(feed() if agenda is None else agenda, DAY, now,
                             self.rows() if rows is None else rows,
                             self.guides, GROUPS, previous, **kwargs)

    def live_row(self, **changes):
        row = next(row for row in self.rows() if row.match_id == MOLDOVA)
        return replace(row, **{'status': 'LIVE', 'source_updated': NOW, **changes})

    def test_real_minimal_fixture_reads_two_played_and_ignores_scheduled(self):
        rows = self.rows()
        self.assertEqual({MOLDOVA, SINGAPORE}, {row.match_id for row in rows})
        self.assertEqual({'FINISHED'}, {row.status for row in rows})
        by_id = {row.match_id: row for row in rows}
        self.assertEqual((2, 1), (by_id[MOLDOVA].home_score, by_id[MOLDOVA].away_score))
        self.assertEqual((1, 4), (by_id[SINGAPORE].home_score, by_id[SINGAPORE].away_score))
        self.assertEqual('2026-11-13T14:00:00-03:00', by_id[MOLDOVA].kickoff.isoformat())

    def test_existing_national_aliases_join_to_exact_published_event_id(self):
        value, report = self.merge()
        self.assertEqual(['fixture-1'], list(value['matches']))
        entry = value['matches']['fixture-1']
        self.assertEqual('FINISHED', entry['status'])
        self.assertEqual((2, 1), (entry['homeScore'], entry['awayScore']))
        self.assertEqual({'provider': 'ole-opta', 'matchId': MOLDOVA, 'url': SOURCE_URL}, entry['source'])
        self.assertEqual({'eligible': 1, 'matched': 1, 'sourceFailed': False}, report)
        self.assertIs(validate_updates(value), value)

    def test_overlay_never_mutates_agenda_or_assigns_channels(self):
        agenda = feed()
        before = copy.deepcopy(agenda)
        value, _ = self.merge(agenda=agenda)
        self.assertEqual(before, agenda)
        self.assertNotIn('channels', value['matches']['fixture-1'])
        self.assertEqual('SCHEDULED', agenda['sections'][0]['events'][0]['status'])

    def test_reversed_teams_kickoff_and_competition_mismatch_do_not_join(self):
        mismatches = [event(home='Kazajiztan', away='Moldavia'), event(start='14:01'),
                      event(competition='Amistosos de selecciones'), event(home='Moldavia Sub 20')]
        for row in mismatches:
            with self.subTest(row=row):
                value, report = self.merge(agenda=feed(row))
                self.assertEqual({}, value['matches'])
                self.assertEqual(0, report['matched'])

    def test_source_competition_id_and_name_both_require_verification(self):
        for field, wrong in [('id', 'unverified-id'), ('name', 'Futbol')]:
            value = payload()
            source_record(value)['matchInfo']['competition'][field] = wrong
            self.assertNotIn(MOLDOVA, {row.match_id for row in self.rows(value)})

    def test_unconfirmed_disabled_and_unknown_channels_are_ignored(self):
        options = [None, [], [{'streamId': 999, 'enabled': True}],
                   [{'streamId': 1, 'enabled': False}], [{'streamId': 2, 'enabled': True}],
                   [{'streamId': '1', 'enabled': True}]]
        for channels in options:
            row = event(channels=False)
            if channels is not None:
                row['channels'] = channels
            with self.subTest(channels=channels):
                value, report = self.merge(agenda=feed(row))
                self.assertEqual({}, value['matches'])
                self.assertEqual(0, report['eligible'])

    def test_exact_retained_window_can_prove_existing_channel_without_adding_one(self):
        agenda = feed(event(channels=False))
        window = {'timezone': TIMEZONE, 'days': {DAY.isoformat(): feed()}}
        value, _ = self.merge(agenda=agenda, window=window)
        self.assertIn('fixture-1', value['matches'])
        self.assertNotIn('channels', agenda['sections'][0]['events'][0])
        window['days'][DAY.isoformat()]['sections'][0]['events'][0]['startTime'] = '14:01'
        value, _ = self.merge(agenda=agenda, window=window)
        self.assertEqual({}, value['matches'])

    def test_existing_confirmed_daily_assignment_can_prove_channel(self):
        agenda = feed(event(channels=False))
        daily = {'date': DAY.isoformat(), 'timezone': TIMEZONE,
                 'broadcasts': [{'fixtureId': 1, 'confidence': 'confirmed', 'channelGroups': ['confirmed']}]}
        value, _ = self.merge(agenda=agenda, daily=daily)
        self.assertIn('fixture-1', value['matches'])
        daily['broadcasts'][0]['confidence'] = 'review'
        value, _ = self.merge(agenda=agenda, daily=daily)
        self.assertEqual({}, value['matches'])

    def test_missing_null_negative_or_bool_score_never_becomes_zero(self):
        totals = [{}, {'home': None, 'away': 1}, {'home': 2},
                  {'home': -1, 'away': 1}, {'home': False, 'away': 1}]
        for total in totals:
            value = payload()
            source_record(value)['liveData']['matchDetails']['scores']['total'] = total
            with self.subTest(total=total):
                result, _ = self.merge(rows=self.rows(value))
                entry = result['matches']['fixture-1']
                self.assertEqual('FINISHED', entry['status'])
                self.assertNotIn('homeScore', entry)
                self.assertNotIn('awayScore', entry)

    def test_explicit_zero_zero_scores_are_retained(self):
        value = payload()
        source_record(value)['liveData']['matchDetails']['scores']['total'] = {'home': 0, 'away': 0}
        result, _ = self.merge(rows=self.rows(value))
        self.assertEqual((0, 0), (result['matches']['fixture-1']['homeScore'],
                                result['matches']['fixture-1']['awayScore']))

    def test_only_explicit_playing_status_becomes_live(self):
        for status in ('Fixture', 'Postponed', 'Cancelled', 'Suspended', None, 'LIVE'):
            value = payload()
            source_record(value)['liveData']['matchDetails']['matchStatus'] = status
            with self.subTest(status=status):
                self.assertNotIn(MOLDOVA, {row.match_id for row in self.rows(value)})
        value = payload()
        source_record(value)['liveData']['matchDetails']['matchStatus'] = 'Playing'
        row = next(row for row in self.rows(value) if row.match_id == MOLDOVA)
        self.assertEqual('LIVE', row.status)

    def test_live_has_fifteen_minute_expiry_and_finished_expires_at_art_midnight(self):
        value, _ = self.merge(rows=[self.live_row()])
        entry = value['matches']['fixture-1']
        self.assertEqual(LIVE_TTL, instant(entry['expiresAt']) - instant(entry['observedAt']))
        value, _ = self.merge()
        self.assertEqual('2026-11-14T00:00:00-03:00', value['matches']['fixture-1']['expiresAt'])

    def test_failed_source_preserves_last_good_document_without_restamping(self):
        previous, _ = self.merge(rows=[self.live_row()])
        result, report = merge_updates(feed(), DAY, NOW + timedelta(minutes=5), None,
                                      self.guides, GROUPS, previous)
        self.assertEqual(previous, result)
        self.assertIsNot(previous, result)
        self.assertTrue(report['sourceFailed'])

    def test_failed_first_source_does_not_invent_document(self):
        result, report = merge_updates(feed(), DAY, NOW, None, self.guides, GROUPS)
        self.assertIsNone(result)
        self.assertTrue(report['sourceFailed'])

    def test_missing_match_preserves_last_good_without_restamping(self):
        previous, _ = self.merge(rows=[self.live_row()])
        result, _ = self.merge(rows=[], previous=previous, now=NOW + timedelta(minutes=5))
        self.assertEqual(previous, result)

    def test_same_snapshot_does_not_extend_live_after_expiry(self):
        row = self.live_row()
        previous, _ = self.merge(rows=[row])
        result, _ = self.merge(rows=[row], previous=previous, now=NOW + timedelta(minutes=20))
        self.assertEqual(previous, result)
        self.assertLess(instant(result['matches']['fixture-1']['expiresAt']), NOW + timedelta(minutes=20))

    def test_out_of_order_source_timestamp_does_not_replace_newer_result(self):
        row = self.live_row()
        previous, _ = self.merge(rows=[row])
        older = replace(row, source_updated=NOW - timedelta(minutes=1), home_score=9)
        result, _ = self.merge(rows=[older], previous=previous, now=NOW + timedelta(minutes=1))
        self.assertEqual(previous, result)

    def test_older_observation_cannot_replace_last_good(self):
        row = self.live_row(source_updated=NOW - timedelta(minutes=2))
        previous, _ = self.merge(rows=[row])
        newer_source = replace(row, source_updated=NOW - timedelta(minutes=1), home_score=9)
        result, _ = self.merge(rows=[newer_source], previous=previous, now=NOW - timedelta(minutes=1))
        self.assertEqual(previous, result)

    def test_finished_never_regresses_to_live_even_with_newer_source(self):
        previous, _ = self.merge()
        row = self.live_row()
        result, _ = self.merge(rows=[row], previous=previous, now=NOW + timedelta(minutes=1))
        self.assertEqual(previous, result)

    def test_persisted_source_identity_does_not_silently_move(self):
        previous, _ = self.merge(rows=[self.live_row()])
        changed_id = replace(self.live_row(), match_id='differentVerifiedSourceId',
                             source_updated=NOW + timedelta(minutes=1))
        result, _ = self.merge(rows=[changed_id], previous=previous, now=NOW + timedelta(minutes=1))
        self.assertEqual(previous, result)

    def test_partial_updates_keep_other_last_good_entries(self):
        agenda = feed(event(), event(identifier='fixture-2', home='Singapur', away='Paraguay',
                                    competition='Amistosos de selecciones', start='09:00'))
        previous, _ = self.merge(agenda=agenda)
        self.assertEqual({'fixture-1', 'fixture-2'}, set(previous['matches']))
        rows = [replace(row, source_updated=NOW, home_score=3) for row in self.rows() if row.match_id == MOLDOVA]
        result, _ = self.merge(rows=rows, agenda=agenda, previous=previous)
        self.assertEqual(previous['matches']['fixture-2'], result['matches']['fixture-2'])
        self.assertEqual(3, result['matches']['fixture-1']['homeScore'])

    def test_ambiguous_same_fixture_rows_are_not_joined(self):
        row = self.live_row()
        result, _ = self.merge(rows=[row, replace(row, match_id='anotherSourceId')])
        self.assertEqual({}, result['matches'])

    def test_past_date_requires_real_current_art_clock(self):
        for now in (NOW + timedelta(days=1), NOW.replace(tzinfo=None)):
            with self.subTest(now=now):
                with self.assertRaisesRegex(MatchUpdatesError, 'current_art_date_required'):
                    self.merge(now=now)
        with self.assertRaisesRegex(MatchUpdatesError, 'agenda_date_mismatch'):
            self.merge(agenda=feed(day=date(2026, 11, 12)))
        self.assertEqual([], parse_ole_bulk(payload(), date(2026, 11, 12)))

    def test_future_kickoff_or_future_source_snapshot_is_not_accepted(self):
        for row in (replace(self.live_row(), kickoff=NOW + timedelta(minutes=1)),
                    replace(self.live_row(), source_updated=NOW + timedelta(minutes=3))):
            with self.subTest(row=row):
                result, _ = self.merge(rows=[row])
                self.assertEqual({}, result['matches'])

    def test_request_shape_and_limits_are_bounded_without_network(self):
        self.assertEqual({'date': '2026-11-13', 'status': 'all',
                          'featuredCompetitions': [{'id': 941}, {'id': 88}],
                          'pgSz': 50, 'country': 'argentina'}, request_body(DAY))
        for kwargs in ({'timeout': 9}, {'max_requests': 3}, {'timeout': True}):
            with self.assertRaises(MatchUpdatesError):
                OleBulkTransport(**kwargs)
        with self.assertRaisesRegex(MatchUpdatesError, 'invalid_json'):
            decode_json(b'{"code":200,"code":200}')
        with self.assertRaisesRegex(MatchUpdatesError, 'invalid_json_size'):
            decode_json(b' ' * 2_000_001)

    def test_strict_updates_reject_future_source_timestamp_and_different_art_date(self):
        previous, _ = self.merge()
        future_source = copy.deepcopy(previous)
        future_source['matches']['fixture-1']['sourceUpdatedAt'] = (NOW + timedelta(seconds=1)).isoformat()
        other_day = copy.deepcopy(previous)
        other_day['updatedAt'] = (NOW + timedelta(days=1)).isoformat()
        for value in (future_source, other_day):
            with self.subTest(value=value):
                with self.assertRaisesRegex(MatchUpdatesError, 'invalid_updates'):
                    validate_updates(value)

    def test_strict_updates_reject_out_of_contract_scores(self):
        previous, _ = self.merge()
        for score in (-1, 1000):
            value = copy.deepcopy(previous)
            value['matches']['fixture-1']['homeScore'] = score
            with self.subTest(score=score):
                with self.assertRaisesRegex(MatchUpdatesError, 'invalid_updates'):
                    validate_updates(value)

    def test_parser_omits_score_pair_above_contract_limit(self):
        value = payload()
        source_record(value)['liveData']['matchDetails']['scores']['total'] = {'home': 1000, 'away': 1}
        result, _ = self.merge(rows=self.rows(value))
        self.assertNotIn('homeScore', result['matches']['fixture-1'])
        self.assertNotIn('awayScore', result['matches']['fixture-1'])

    def test_source_one_second_in_future_cannot_create_or_replace_entry(self):
        row = self.live_row(source_updated=NOW + timedelta(seconds=1))
        value, _ = self.merge(rows=[row])
        self.assertEqual({}, value['matches'])
        previous, _ = self.merge(rows=[self.live_row()])
        value, _ = self.merge(rows=[replace(row, home_score=9)], previous=previous)
        self.assertEqual(previous, value)

    def test_future_source_cannot_confirm_final_latch(self):
        previous, _ = self.merge()
        now = NOW + timedelta(minutes=4)
        state = reserve_request(None, DAY, now)
        eligible = eligible_events(feed(), DAY, self.guides, GROUPS)
        row = next(row for row in self.rows() if row.match_id == MOLDOVA)
        future = replace(row, source_updated=now + timedelta(seconds=1))
        confirmed = confirm_finished(state, previous, [future], eligible, self.guides, DAY, now)
        self.assertEqual(state, confirmed)
        self.assertEqual({}, confirmed['settled'])

    def test_pending_only_requests_supported_fixtures_near_kickoff(self):
        eligible = eligible_events(feed(), DAY, self.guides, GROUPS)
        for now in (instant('2026-11-13T13:49:00-03:00'), instant('2026-11-13T22:01:00-03:00')):
            with self.subTest(now=now):
                self.assertEqual({}, pending_events(eligible, DAY, now, self.guides))
        self.assertEqual({'fixture-1'}, set(pending_events(eligible, DAY,
                         instant('2026-11-13T13:50:00-03:00'), self.guides)))
        unsupported = eligible_events(feed(event(competition='Premier League')), DAY, self.guides, GROUPS)
        self.assertEqual({}, pending_events(unsupported, DAY, NOW, self.guides))

    def test_delayed_job_can_fetch_an_unobserved_final_after_three_hours(self):
        eligible = eligible_events(feed(), DAY, self.guides, GROUPS)
        for now in (instant('2026-11-13T17:01:00-03:00'), instant('2026-11-13T20:00:00-03:00')):
            with self.subTest(now=now):
                self.assertEqual({'fixture-1'}, set(pending_events(eligible, DAY, now, self.guides)))
        # The clock makes a lookup eligible; it cannot manufacture the result.
        value, _ = merge_updates(feed(), DAY, instant('2026-11-13T20:00:00-03:00'),
                                 None, self.guides, GROUPS)
        self.assertIsNone(value)

    def test_netherlands_greece_final_matches_the_existing_holanda_card(self):
        # Regression from the public snapshot captured at 19:15 ART, after the TV
        # signal had moved on to other programming. No channel is assigned here.
        target = event(identifier='fixture-1312853287', home='Holanda', away='Grecia', start='16:45')
        row = replace(self.live_row(), match_id='7f2uuphgt26zk1eh9x152wb2s',
                      home='Netherlands', away='Greece', kickoff=instant('2026-11-13T16:45:00-03:00'),
                      status='FINISHED', source_updated=instant('2026-11-13T18:53:51-03:00'),
                      home_score=7, away_score=0)
        value, _ = merge_updates(feed(target), DAY, instant('2026-11-13T19:15:00-03:00'),
                                 [row], self.guides, GROUPS)
        entry = value['matches'][target['id']]
        self.assertEqual(('FINISHED', 7, 0), (entry['status'], entry['homeScore'], entry['awayScore']))
        self.assertEqual(target['homeTeam'], entry['fixture']['homeTeam'])
        self.assertEqual('16:45', entry['fixture']['startTime'])
        self.assertNotIn('channels', entry)

    def test_old_live_snapshot_cannot_receive_a_new_observation_expiry(self):
        value, _ = self.merge(rows=[self.live_row(source_updated=NOW - LIVE_TTL - timedelta(seconds=1))])
        self.assertEqual({}, value['matches'])

    def test_identical_live_without_source_timestamp_never_restamps(self):
        row = self.live_row(source_updated=None)
        first, _ = self.merge(rows=[row])
        repeated, _ = self.merge(rows=[row], previous=first, now=NOW + timedelta(minutes=5))
        self.assertEqual(first, repeated)

    def test_persistent_request_budget_enforces_four_minutes_and_daily_cap(self):
        state = reserve_request(None, DAY, NOW)
        self.assertEqual(1, state['requests'])
        for seconds in (0, 239):
            self.assertIsNone(reserve_request(state, DAY, NOW + timedelta(seconds=seconds)))
        following = reserve_request(state, DAY, NOW + timedelta(minutes=4))
        self.assertEqual(2, following['requests'])
        exhausted = {**state, 'requests': MAX_DAILY_REQUESTS}
        self.assertIsNone(reserve_request(exhausted, DAY, NOW + timedelta(minutes=4)))
        next_day = reserve_request(exhausted, date(2026, 11, 14), NOW + timedelta(days=1))
        self.assertEqual(1, next_day['requests'])
        self.assertEqual({}, next_day['settled'])


class FakeTransport:
    def __init__(self, fail=False):
        self.requests = 0
        self.fail = fail

    def fetch(self, day):
        self.requests += 1
        if self.fail:
            raise MatchUpdatesError('source_unavailable')
        return payload()


class MatchUpdatesRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'sports_match_updates.json'
        self.state = self.root / 'sports_match_updates_state.json'
        self.transport = FakeTransport()
        self.write('sports_today.json', feed())
        self.write('sports_agenda_guides.json',
                   json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8')))
        self.write('mbox_sports_channels_clean.json', {'schemaVersion': 1, 'groups': [
            {'id': 'confirmed', 'canonicalName': 'Existing configured signal', 'aliases': [],
             'streams': [{'streamId': 1, 'name': 'Existing configured signal', 'priority': 1, 'enabled': True}]}]})

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value) + '\n', encoding='utf8')

    def refresh(self, now=None, **kwargs):
        return refresh(self.root, self.output, DAY,
                       now or instant('2026-11-13T16:00:00-03:00'),
                       transport=self.transport, state_output=self.state, **kwargs)

    def test_no_eligible_or_idle_fixture_makes_zero_requests(self):
        for agenda in (feed(event(channels=False)), feed(event(start='20:00'))):
            self.write('sports_today.json', agenda)
            with self.subTest(agenda=agenda):
                _, report = self.refresh()
                self.assertEqual(0, report['requests'])
                self.assertEqual(0, self.transport.requests)
                self.assertFalse(self.state.exists())

    def test_finished_gets_one_post_confirmation_lookup_then_settles(self):
        now = instant('2026-11-13T16:00:00-03:00')
        first, report = self.refresh(now)
        self.assertEqual('FINISHED', first['matches']['fixture-1']['status'])
        self.assertEqual(1, report['requests'])
        overlay_before = self.output.read_bytes()
        _, confirmation = self.refresh(now + timedelta(minutes=4))
        self.assertEqual(1, confirmation['requests'])
        self.assertEqual(2, self.transport.requests)
        self.assertEqual(overlay_before, self.output.read_bytes())
        state = json.loads(self.state.read_text(encoding='utf8'))
        self.assertEqual(2, state['requests'])
        self.assertEqual('1jpjop6resgb4rdrg9s6rgs9g', state['settled']['fixture-1'][0])
        _, settled = self.refresh(now + timedelta(minutes=8))
        self.assertEqual(0, settled['requests'])
        self.assertEqual(0, settled['pending'])
        self.assertEqual(2, self.transport.requests)

    def test_failure_reserves_persistent_attempt_but_preserves_overlay_bytes(self):
        offline = self.root / 'source.json'
        self.write('source.json', payload())
        self.refresh(source_input=offline)
        self.assertFalse(self.state.exists())
        # Preserve even a valid last-good file written by another serializer.
        self.output.write_text(json.dumps(json.loads(self.output.read_text(encoding='utf8'))), encoding='utf8')
        before = self.output.read_bytes()
        self.transport.fail = True
        _, report = self.refresh()
        self.assertTrue(report['sourceFailed'])
        self.assertEqual(1, report['requests'])
        self.assertEqual(before, self.output.read_bytes())
        self.assertEqual(1, json.loads(self.state.read_text(encoding='utf8'))['requests'])
        _, repeated = self.refresh(instant('2026-11-13T16:03:00-03:00'))
        self.assertEqual(0, repeated['requests'])
        self.assertEqual(1, self.transport.requests)
        self.assertEqual(before, self.output.read_bytes())

    def test_offline_source_never_changes_existing_request_state(self):
        self.write('source.json', payload())
        self.write('sports_match_updates_state.json', {'sentinel': 'offline preview does not consult this'})
        before = self.state.read_bytes()
        value, report = self.refresh(source_input=self.root / 'source.json')
        self.assertIn('fixture-1', value['matches'])
        self.assertEqual(0, report['requests'])
        self.assertEqual(0, self.transport.requests)
        self.assertEqual(before, self.state.read_bytes())

    def test_stale_future_or_missing_agenda_skips_without_touching_persisted_bytes(self):
        self.write('source.json', payload())
        previous, _ = self.refresh(source_input=self.root / 'source.json')
        now = instant('2026-11-13T16:04:00-03:00')
        self.write('sports_match_updates_state.json', reserve_request(None, DAY, now - timedelta(minutes=4)))
        # Deliberate harmless formatting proves the skip leaves the files untouched.
        self.output.write_bytes(b'\n' + self.output.read_bytes())
        self.state.write_bytes(b'\n' + self.state.read_bytes())
        overlay_before, state_before = self.output.read_bytes(), self.state.read_bytes()
        for available in (date(2026, 11, 12), date(2026, 11, 14), None):
            if available is None:
                (self.root / 'sports_today.json').unlink()
            else:
                self.write('sports_today.json', feed(day=available))
            with self.subTest(available=available):
                value, diagnostics = self.refresh(now)
                self.assertEqual(previous, value)
                self.assertTrue(diagnostics['agendaUnavailable'])
                self.assertEqual(available.isoformat() if available else 'missing', diagnostics['availableDate'])
                self.assertEqual(0, diagnostics['requests'])
                self.assertFalse(diagnostics['changed'])
                self.assertEqual(0, self.transport.requests)
                self.assertEqual(overlay_before, self.output.read_bytes())
                self.assertEqual(state_before, self.state.read_bytes())

    def test_missing_agenda_with_no_previous_does_not_create_overlay_or_state(self):
        (self.root / 'sports_today.json').unlink()
        value, diagnostics = self.refresh()
        self.assertIsNone(value)
        self.assertTrue(diagnostics['agendaUnavailable'])
        self.assertEqual('missing', diagnostics['availableDate'])
        self.assertEqual(0, diagnostics['requests'])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(0, self.transport.requests)

    def test_unavailable_agenda_does_not_hide_malformed_configuration(self):
        (self.root / 'sports_today.json').unlink()
        self.write('sports_agenda_guides.json', {'schemaVersion': 0})
        with self.assertRaisesRegex(ValueError, 'ARGENTINA_GUIDE_CONFIGURATION_INVALID'):
            self.refresh()
        self.assertEqual(0, self.transport.requests)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
