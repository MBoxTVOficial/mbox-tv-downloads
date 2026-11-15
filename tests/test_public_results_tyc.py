"""Real public-page extracts plus rejection cases for independent result sources."""
import copy
from datetime import date, datetime
import json
from pathlib import Path
import unittest

from scripts.broadcaster_sources import ZONE, Tree
from scripts.public_results_tyc import (
    MAX_BODY, SOURCE_URLS, parse_canchallena_results, parse_tyc_results,
)
from scripts.sports_match_updates import MatchUpdatesError, fixture_key, _source_key, merge_updates


FIXTURES = Path(__file__).resolve().parent / 'fixtures'
DAY = date(2026, 11, 14)


def tyc():
    return (FIXTURES / 'tyc_results_2026_11_14.html').read_text(encoding='utf8')


def cancha():
    html = (FIXTURES / 'canchallena_results_2026_11_14.html').read_text(encoding='utf8')
    return json.JSONDecoder().raw_decode(html.split('window.__PRELOADED_STATE__ = ', 1)[1])[0]


def page(data):
    return '<script>window.__PRELOADED_STATE__ = ' + json.dumps(data) + '</script>'


def first(data):
    return data['homeReducer']['matchesData']['matchesByCompetition'][0]['matches'][0]


class PublicResultsTycTests(unittest.TestCase):
    def test_real_tyc_finals_have_original_art_kickoff_and_separate_identity(self):
        rows = parse_tyc_results(tyc(), DAY)
        self.assertEqual(3, len(rows))
        colon = next(row for row in rows if row.match_id == '795033')
        self.assertEqual(('Primera Nacional', 'FINISHED', 1, 0),
                         (colon.competition, colon.status, colon.home_score, colon.away_score))
        self.assertEqual('2026-11-14T18:00:00-03:00', colon.kickoff.isoformat())
        self.assertEqual(('tyc-results', SOURCE_URLS['tyc-results']), (colon.provider, colon.source_url))
        self.assertIsNone(colon.source_updated)  # No source timestamp is manufactured.
        self.assertEqual(rows, parse_tyc_results(tyc().encode('utf8'), DAY))

    def test_tyc_scheduled_rows_with_placeholder_scores_are_not_results(self):
        self.assertEqual([], parse_tyc_results(tyc(), date(2026, 11, 15)))

    def test_tyc_other_day_and_live_article_title_do_not_prove_a_final(self):
        self.assertEqual([], parse_tyc_results(tyc(), date(2026, 11, 13)))
        html = tyc().replace('Finalizado', 'En vivo')
        self.assertEqual([], parse_tyc_results(html, DAY))

    def test_tyc_requires_exact_competition_context(self):
        with self.assertRaises(MatchUpdatesError):
            parse_tyc_results(tyc().replace('Fixture de Primera Nacional', 'Fixture de Primera C'), DAY)

    def test_tyc_missing_team_name_is_not_replaced_with_abbreviation(self):
        html = tyc().replace('alt="F. C. Midland"', 'alt=""')
        self.assertNotIn('795033', {row.match_id for row in parse_tyc_results(html, DAY)})

    def test_tyc_invalid_score_does_not_become_zero(self):
        html = tyc().replace('<span class="partido-goles">1</span>', '<span class="partido-goles">-</span>')
        self.assertNotIn('795033', {row.match_id for row in parse_tyc_results(html, DAY)})

    def test_tyc_unbound_source_match_id_is_ignored(self):
        html = tyc().replace('deportes.futbol.nacionalb.', 'deportes.basquet.nacionalb.')
        self.assertEqual([], parse_tyc_results(html, DAY))

    def test_tyc_verified_pn_ids_resolve_two_real_published_fixture_identities(self):
        # Real agenda identities, captured separately from the result parser.
        # TV channels are deliberately empty: this is an informational final fallback.
        agenda = json.loads((FIXTURES / 'tyc_result_agenda_identities_2026_11_14.json').read_text(encoding='utf8'))
        guides = json.loads((FIXTURES.parent.parent / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        rows = parse_tyc_results(tyc(), DAY)
        published = {tuple(fixture_key(e, DAY, guides)) for s in agenda['sections'] for e in s['events']}
        self.assertEqual(2, sum(tuple(_source_key(row, guides)) in published for row in rows))
        result, report = merge_updates(agenda, DAY, datetime(2026, 11, 14, 23, 59, tzinfo=ZONE),
                                       rows, guides, {}, scope='informative')
        self.assertEqual(2, report['matched'])
        colon = result['matches']['fixture-1087525276']
        self.assertEqual(('FINISHED', 1, 0), (colon['status'], colon['homeScore'], colon['awayScore']))
        self.assertEqual('tyc-results', colon['source']['provider'])
        self.assertIn('fixture-1146901096', result['matches'])
        self.assertNotIn('fixture-1600255604', result['matches'])  # bare Estudiantes stays ambiguous
        self.assertTrue(all(not event['channels'] for s in agenda['sections'] for event in s['events']))

    def test_tyc_result_aliases_require_the_verified_crest_id_as_well_as_exact_label(self):
        rows = parse_tyc_results(tyc(), DAY)
        self.assertEqual('Midland', next(row.away for row in rows if row.match_id == '795033'))
        self.assertEqual(('Gimnasia Jujuy', 'Almirante Brown'),
                         next((row.home, row.away) for row in rows if row.match_id == '795035'))
        for wrong_url in ('https://statics-files.tycsports.com/escudos/663.png?v=6',
                          'https://unverified.invalid/escudos/785.png?v=6'):
            html = tyc().replace('https://statics-files.tycsports.com/escudos/785.png?v=6', wrong_url)
            self.assertEqual('F. C. Midland', next(row.away for row in parse_tyc_results(html, DAY) if row.match_id == '795033'))

    def test_tyc_bare_estudiantes_is_not_relabelled_caseros_or_la_plata(self):
        row = next(row for row in parse_tyc_results(tyc(), DAY) if row.match_id == '795036')
        self.assertEqual('Estudiantes', row.away)
        guides = json.loads((FIXTURES.parent.parent / 'sports_agenda_guides.json').read_text(encoding='utf8'))
        for away in ('Estudiantes BA', 'Estudiantes Caseros', 'Estudiantes La Plata', 'Estudiantes LP'):
            event = {'id': 'fixture-1', 'sport': 'football', 'competition': 'Primera Nacional',
                     'homeTeam': 'Atlanta', 'awayTeam': away, 'startTime': '15:00', 'channels': []}
            self.assertNotEqual(fixture_key(event, DAY, guides), _source_key(row, guides))

    def test_real_canchallena_finals_reuse_original_kickoff_not_last_observation(self):
        rows = parse_canchallena_results(page(cancha()), DAY)
        self.assertEqual(6, len(rows))
        copa = next(row for row in rows if row.competition == 'Copa Argentina')
        self.assertEqual(('FINISHED', 0, 3), (copa.status, copa.home_score, copa.away_score))
        self.assertEqual('2026-11-14T19:00:00-03:00', copa.kickoff.isoformat())
        self.assertEqual('2026-11-14T20:50:20-03:00', copa.source_updated.isoformat())
        self.assertEqual('Boca Juniors', copa.away)
        self.assertEqual('canchallena', copa.provider)
        self.assertEqual(SOURCE_URLS['canchallena'], copa.source_url)

    def test_canchallena_rejects_wrong_day_and_disagreeing_utc_local_clock(self):
        self.assertEqual([], parse_canchallena_results(page(cancha()), date(2026, 11, 13)))
        data = cancha()
        first(data)['matchDateText'] = '2026-11-14 19:01:00'
        self.assertNotIn(first(data)['id'], {row.match_id for row in parse_canchallena_results(page(data), DAY)})

    def test_canchallena_numeric_status_does_not_guess_unknown_scheduled_or_suspended(self):
        for status in ('0', '3', '4', '5', '6', '7', 'FINISHED', None, 1):
            data = cancha()
            first(data)['matchStatus'] = status
            with self.subTest(status=status):
                self.assertNotIn(first(data)['id'], {r.match_id for r in parse_canchallena_results(page(data), DAY)})

    def test_canchallena_source_clock_fallback_final_without_postmatch_period_is_rejected(self):
        for period in (1, 2, 16, '14', None, True):
            data = cancha()
            first(data)['periodId'] = period
            with self.subTest(period=period):
                self.assertNotIn(first(data)['id'], {r.match_id for r in parse_canchallena_results(page(data), DAY)})
        data = cancha()
        first(data)['isOverWritten'] = True
        self.assertNotIn(first(data)['id'], {r.match_id for r in parse_canchallena_results(page(data), DAY)})

    def test_canchallena_explicit_playing_enum_and_period_can_provide_live(self):
        data = cancha()
        item = first(data)
        item.update(matchStatus='2', periodId=2, matchTime="67'")
        rows = parse_canchallena_results(page(data), DAY)
        live = next(row for row in rows if row.match_id == item['id'])
        self.assertEqual('LIVE', live.status)
        self.assertEqual((0, 3), (live.home_score, live.away_score))

    def test_canchallena_duration_is_not_treated_as_timestamp_or_replaced_with_now(self):
        data = cancha()
        first(data).update(matchStatus='2', periodId=2, dateTime='PT1H7M')
        rows = parse_canchallena_results(page(data), DAY, now=datetime(2026, 11, 14, 22, tzinfo=ZONE))
        live = next(row for row in rows if row.match_id == first(data)['id'])
        self.assertIsNone(live.source_updated)

    def test_canchallena_requires_both_valid_scores_and_keeps_penalties_separate(self):
        for scores in ({}, {'homeScore': 0}, {'homeScore': True, 'awayScore': 0},
                       {'homeScore': -1, 'awayScore': 0}, {'homeScore': '0', 'awayScore': '3'}):
            data = cancha()
            first(data)['goals'] = scores
            with self.subTest(scores=scores):
                self.assertNotIn(first(data)['id'], {r.match_id for r in parse_canchallena_results(page(data), DAY)})
        data = cancha()
        first(data)['penalties'] = {'homeScore': 4, 'awayScore': 5}
        row = next(r for r in parse_canchallena_results(page(data), DAY) if r.match_id == first(data)['id'])
        self.assertEqual((0, 3), (row.home_score, row.away_score))

    def test_canchallena_requires_both_verified_competition_id_and_name(self):
        for field, value in [('id', 'unverified'), ('competitionName', 'Unknown'), ('sportName', 'basketball')]:
            data = cancha()
            data['homeReducer']['matchesData']['matchesByCompetition'][0]['competition'][field] = value
            with self.subTest(field=field):
                self.assertNotIn(first(data)['id'], {r.match_id for r in parse_canchallena_results(page(data), DAY)})

    def test_canchallena_conflicting_duplicate_source_id_is_quarantined(self):
        data = cancha()
        duplicate = copy.deepcopy(first(data))
        duplicate['goals']['awayScore'] = 4
        data['homeReducer']['matchesData']['matchesByCompetition'][0]['matches'].append(duplicate)
        self.assertNotIn(first(data)['id'], {r.match_id for r in parse_canchallena_results(page(data), DAY)})

    def test_canchallena_never_evaluates_script_or_accepts_duplicate_hydration(self):
        for raw in ('<script>alert(1)</script>', page(cancha()) + page(cancha()),
                    '<script>window.__PRELOADED_STATE__ = eval("anything")</script>',
                    '<script>window.__PRELOADED_STATE__ = {"homeReducer":{},"homeReducer":{}}</script>'):
            with self.subTest(raw=raw[:80]), self.assertRaises(MatchUpdatesError):
                parse_canchallena_results(raw, DAY)

    def test_input_validation_and_size_are_bounded(self):
        for parser in (parse_tyc_results, parse_canchallena_results):
            for raw, day in [(b'x' * (MAX_BODY + 1), DAY), (None, DAY), (tyc(), '2026-11-14')]:
                with self.subTest(parser=parser.__name__, day=day), self.assertRaises(MatchUpdatesError):
                    parser(raw, day)


if __name__ == '__main__':
    unittest.main()
