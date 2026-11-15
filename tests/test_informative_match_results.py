"""Real public Copa/Primera Nacional rows, joined without using channel evidence."""
import copy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import unittest

from scripts.broadcaster_sources import instant
from scripts.sports_match_updates import eligible_events, merge_updates, parse_ole_bulk, pending_events


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 14)
NOW = instant('2026-11-14T23:00:00-03:00')
ATLANTA = '3kphptu6ilognhed58wf3y3o4'
TEMPERLEY = '3lzdiqmazfn9x5pbmooczlvkk'
BOCA = 'dbcssu950tmvx7uthp6esg93o'
GROUPS = {'existing': {'streams': [{'streamId': 1, 'enabled': True}]}}


def payload():
    return json.loads((ROOT / 'tests/fixtures/ole_argentina_match_updates_2026_11_14.json').read_text(encoding='utf8'))


def event(identifier, home, away, start, competition='Primera Nacional', channels=False):
    value = {'id': identifier, 'sport': 'football', 'competition': competition,
             'homeTeam': home, 'awayTeam': away, 'startTime': start, 'status': 'SCHEDULED'}
    if channels:
        value['channels'] = [{'name': 'Existing channel', 'streamId': 1, 'enabled': True}]
    return value


def feed(*events):
    return {'date': DAY.isoformat(), 'timezone': 'America/Argentina/Buenos_Aires',
            'sections': [{'events': list(events)}]}


class InformativeMatchResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guides = json.loads((ROOT / 'sports_agenda_guides.json').read_text(encoding='utf8'))

    def merge(self, agenda, raw=None, rows=None, previous=None):
        if rows is None:
            rows = parse_ole_bulk(payload() if raw is None else raw, DAY)
        return merge_updates(agenda, DAY, NOW, rows, self.guides, GROUPS, previous=previous)

    def test_real_source_verifies_copa_and_primera_nacional_ids_and_statuses(self):
        rows = {row.match_id: row for row in parse_ole_bulk(payload(), DAY)}
        self.assertEqual(('Copa Argentina', 'LIVE'), (rows[BOCA].competition, rows[BOCA].status))
        self.assertEqual(('Primera Nacional', 'FINISHED', 1, 1),
                         (rows[ATLANTA].competition, rows[ATLANTA].status,
                          rows[ATLANTA].home_score, rows[ATLANTA].away_score))
        self.assertEqual(('FINISHED', 1, 2),
                         (rows[TEMPERLEY].status, rows[TEMPERLEY].home_score, rows[TEMPERLEY].away_score))
        self.assertEqual(5, len(rows))  # The scheduled sixth match is not a result.

    def test_real_finals_match_existing_aliases_without_adding_channels(self):
        agenda = feed(event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '15:00'),
                      event('fixture-11', 'Temperley', 'Ciudad de Bolivar', '16:00'))
        before = copy.deepcopy(agenda)
        value, diagnostics = self.merge(agenda)
        self.assertEqual({'fixture-10', 'fixture-11'}, set(value['matches']))
        self.assertEqual((1, 1), (value['matches']['fixture-10']['homeScore'], value['matches']['fixture-10']['awayScore']))
        self.assertEqual((1, 2), (value['matches']['fixture-11']['homeScore'], value['matches']['fixture-11']['awayScore']))
        self.assertEqual(2, diagnostics['matched'])
        self.assertEqual(before, agenda)
        self.assertTrue(all('channels' not in row and row['status'] == 'FINISHED'
                            for row in value['matches'].values()))

    def test_current_copa_playing_snapshot_does_not_change_informative_card(self):
        agenda = feed(event('fixture-12', 'Banfield', 'Boca', '19:00', 'Copa Argentina'))
        row = next(row for row in parse_ole_bulk(payload(), DAY) if row.match_id == BOCA)
        row = replace(row, source_updated=NOW)  # A fresh observation of the same explicit Playing state.
        value, _ = self.merge(agenda, rows=[row])
        self.assertEqual({}, value['matches'])
        agenda['sections'][0]['events'][0]['channels'] = [{'name': 'Existing channel', 'streamId': 1}]
        value, _ = self.merge(agenda, rows=[row])
        self.assertEqual('LIVE', value['matches']['fixture-12']['status'])  # Existing confirmed-card policy.

    def test_scheduled_or_unobserved_final_remains_informative_even_after_kickoff(self):
        agenda = feed(event('fixture-13', 'Gimnasia Jujuy', 'Almirante Brown', '20:00'))
        value, _ = self.merge(agenda)
        self.assertEqual({}, value['matches'])
        eligible = eligible_events(agenda, DAY, self.guides, GROUPS)
        self.assertEqual({'fixture-13'}, set(pending_events(eligible, DAY, NOW, self.guides)))

    def test_no_result_keeps_existing_informative_final_unchanged(self):
        agenda = feed(event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '15:00'))
        previous, _ = self.merge(agenda)
        incomplete = payload()
        first = next(row for group in incomplete['data']['fixture'] for row in group['match']
                     if row['matchInfo']['id'] == ATLANTA)
        first['liveData']['matchDetails']['scores'] = {'total': {'home': 1}}
        value, _ = self.merge(agenda, raw=incomplete, previous=previous)
        self.assertEqual(previous, value)

    def test_exact_competition_home_away_and_kickoff_are_required(self):
        mismatches = [event('fixture-10', 'Estudiantes BA', 'CA Atlanta', '15:00'),
                      event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '15:01'),
                      event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '15:00', 'Copa Argentina'),
                      event('fixture-10', 'Club Atlético Atlanta', 'Estudiantes BA', '15:00')]
        for target in mismatches:
            with self.subTest(target=target):
                value, _ = self.merge(feed(target))
                self.assertEqual({}, value['matches'])

    def test_following_utc_date_still_joins_the_previous_art_day(self):
        raw = payload()
        first = next(row for group in raw['data']['fixture'] for row in group['match']
                     if row['matchInfo']['id'] == ATLANTA)
        first['matchInfo'].update(date='2026-11-15Z', time='00:30:00Z', lastUpdated='2026-11-15T01:30:00Z')
        target = event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '21:30')
        value, _ = self.merge(feed(target), raw=raw)
        self.assertEqual('2026-11-14T21:30:00-03:00', value['matches']['fixture-10']['fixtureKey'][3])
        self.assertNotIn(ATLANTA, {row.match_id for row in parse_ole_bulk(raw, date(2026, 11, 15))})

    def test_unverified_competition_id_cannot_enable_a_result(self):
        raw = payload()
        for group in raw['data']['fixture']:
            for row in group['match']:
                row['matchInfo']['competition']['id'] = 'unvalidatedCompetition'
        value, _ = self.merge(feed(event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '15:00')), raw=raw)
        self.assertEqual({}, value['matches'])

    def test_unknown_agenda_competition_does_not_block_independently_valid_final(self):
        agenda = feed(event('fixture-10', 'CA Atlanta', 'Estudiantes BA', '15:00'),
                      event('fixture-20', 'Other', 'Teams', '16:00', 'Unvalidated tournament'))
        value, diagnostics = self.merge(agenda)
        self.assertEqual({'fixture-10'}, set(value['matches']))
        self.assertEqual(1, diagnostics['eligible'])


if __name__ == '__main__':
    unittest.main()
