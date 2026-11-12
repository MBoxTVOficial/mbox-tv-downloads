"""Offline contracts from sanitized official server-rendered cards, never live TV guesses."""
from datetime import datetime, timezone
from pathlib import Path
import unittest

from scripts.broadcaster_sources import DiscoveryError, ZONE
from scripts.conmebol_official_source import parse_conmebol_fixtures, SOURCE_URLS


ROOT = Path(__file__).resolve().parent / 'fixtures'
LIB = 'CONMEBOL Libertadores'
SUDA = 'CONMEBOL Sudamericana'


def card(slug='libertadores', identifier=7, stamp='1795896000', names='Club LocalvsClub Visitante'):
    return f'''<div class="m-fixture-teaser-card m-fixture-teaser-card--played">
      <a class="m-fixture-teaser-card__match-centre-link"
         href="https://gol.conmebol.com/{slug}/es/fixture/view/{identifier}">
        <span class="visually-hidden">{names}</span></a>
      <div class="m-fixture-teaser-card__date">
        <span class="js--usertime" data-timestamp="{stamp}" data-format="d MMM">28 Noviembre</span>
        <span class="js--usertime" data-timestamp="{stamp}" data-format="HH:mm">17:00</span>
      </div><div class="m-fixture-teaser-card__score">5 - 4</div>
    </div>'''


def page(body=''):
    return ('<div class="m-tournament-strip paragraph--type--tournament-fixtures-results">'
            '<div class="m-tournament-strip__slider">' + body + '</div></div>')


class ConmebolOfficialSourceTests(unittest.TestCase):
    def parse(self, body, competition=LIB):
        return parse_conmebol_fixtures(body, competition, SOURCE_URLS[competition])

    def test_actual_libertadores_cards_keep_complete_names_and_stable_official_identity(self):
        rows = self.parse((ROOT / 'official_conmebol_libertadores.html').read_text(encoding='utf8'))
        self.assertEqual(2, len(rows))
        final = rows[-1]
        self.assertEqual('libertadores:1683', final['providerFixtureId'])
        self.assertEqual('SE Palmeiras', final['homeTeam'])
        self.assertEqual('Club Estudiantes de La Plata', final['awayTeam'])
        self.assertEqual('2026-11-28T17:00:00-03:00', final['startTime'].isoformat())
        self.assertEqual('https://gol.conmebol.com/libertadores/es/fixture/view/1683', final['sourceUrl'])

    def test_actual_sudamericana_card_has_its_own_competition_identity(self):
        rows = self.parse((ROOT / 'official_conmebol_sudamericana.html').read_text(encoding='utf8'), SUDA)
        final = rows[-1]
        self.assertEqual('sudamericana:1727', final['providerFixtureId'])
        self.assertEqual('CA Mineiro', final['homeTeam'])
        self.assertEqual('CA Boca Juniors', final['awayTeam'])
        self.assertEqual('2026-11-21T19:30:00-03:00', final['startTime'].isoformat())
        self.assertEqual(SUDA, final['competition'])

    def test_utc_next_day_is_converted_before_art_membership(self):
        rows = self.parse((ROOT / 'official_conmebol_libertadores.html').read_text(encoding='utf8'))
        start = rows[0]['startTime']
        self.assertEqual('2026-08-19', start.astimezone(timezone.utc).date().isoformat())
        self.assertEqual('2026-08-18T21:30:00-03:00', start.isoformat())
        self.assertEqual(ZONE, start.tzinfo)

    def test_output_whitelist_has_no_scores_state_venue_or_channels(self):
        row = self.parse(page(card()))[0]
        self.assertEqual({'competition', 'competitionId', 'providerFixtureId', 'homeTeam',
                          'awayTeam', 'startTime', 'source', 'sourceUrl', 'broadcasters'}, set(row))
        self.assertEqual('conmebol_official', row['source'])
        self.assertEqual([], row['broadcasters'])

    def test_rights_or_widget_text_never_confirms_fixture_television(self):
        body = page(card() + '<p>ESPN / DSPORTS Argentina titulares de derechos</p>')
        body += '<script>{"widgetKey":"synthetic-do-not-use"}</script>'
        row = self.parse(body)[0]
        self.assertEqual([], row['broadcasters'])
        self.assertNotIn('synthetic-do-not-use', repr(row))

    def test_unknown_competition_is_rejected(self):
        with self.assertRaises(DiscoveryError):
            parse_conmebol_fixtures(page(card()), 'UEFA Champions League', SOURCE_URLS[LIB])

    def test_wrong_competition_card_cannot_enter_source(self):
        with self.assertRaisesRegex(DiscoveryError, 'competition mismatch'):
            self.parse(page(card(slug='sudamericana')))

    def test_identical_numeric_ids_stay_isolated_between_competitions(self):
        lib = self.parse(page(card()))[0]
        suda = self.parse(page(card(slug='sudamericana')), SUDA)[0]
        self.assertNotEqual(lib['providerFixtureId'], suda['providerFixtureId'])

    def test_untrusted_source_urls_are_rejected(self):
        for url in ('http://gol.conmebol.com/libertadores/es',
                    'https://example.com/libertadores/es',
                    SOURCE_URLS[SUDA], SOURCE_URLS[LIB] + '?key=forbidden'):
            with self.subTest(url=url), self.assertRaises(DiscoveryError):
                parse_conmebol_fixtures(page(card()), LIB, url)

    def test_untrusted_fixture_destination_is_rejected(self):
        with self.assertRaises(DiscoveryError):
            self.parse(page(card().replace('https://gol.conmebol.com/', 'https://example.com/')))

    def test_missing_or_invalid_timestamp_fails_closed(self):
        for value in ('', 'tomorrow', '-1', '1795896000000'):
            with self.subTest(value=value), self.assertRaises(DiscoveryError):
                self.parse(page(card(stamp=value)))

    def test_two_different_clocks_are_ambiguous(self):
        # Use distinct timestamps, without relying on duplicate HTML attributes.
        broken = card().replace('data-timestamp="1795896000" data-format="HH:mm"',
                                'data-timestamp="1795896060" data-format="HH:mm"')
        with self.assertRaises(DiscoveryError):
            self.parse(page(broken))

    def test_ambiguous_or_incomplete_team_labels_are_rejected(self):
        for names in ('Local', 'LocalvsvsVisitante', 'vsVisitante', 'Mismo ClubvsMismo Club'):
            with self.subTest(names=names), self.assertRaises(DiscoveryError):
                self.parse(page(card(names=names)))

    def test_same_fixture_duplicate_is_deduplicated_despite_score_text(self):
        rows = self.parse(page(card() + card().replace('5 - 4', '6 - 4')))
        self.assertEqual(1, len(rows))

    def test_conflicting_fixture_duplicate_fails_closed(self):
        with self.assertRaisesRegex(DiscoveryError, 'duplicate'):
            self.parse(page(card() + card(stamp='1795896060')))

    def test_recognized_empty_fixture_strip_is_valid_empty(self):
        self.assertEqual([], self.parse(page()))

    def test_missing_or_changed_layout_is_not_reported_as_empty(self):
        for body in ('<html>Service unavailable</html>',
                     page(card()).replace('m-fixture-teaser-card"', 'new-card-layout"').replace('m-fixture-teaser-card m-fixture-teaser-card--played', 'new-card-layout')):
            with self.subTest(body=body[:50]), self.assertRaises(DiscoveryError):
                self.parse(body)


if __name__ == '__main__':
    unittest.main()
