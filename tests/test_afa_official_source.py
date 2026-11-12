"""Offline AFA snapshots: fixture context, exact dates, no television inference."""
from datetime import datetime
from pathlib import Path
import unittest

from scripts.afa_official_source import (AfaSourceError, ART, afa_article_links,
                                         parse_afa_fixtures)

URL = ('https://www.afa.com.ar/es/posts/'
       'programacion-y-arbitros-para-la-final-por-el-campeonato-de-la-primera-nacional-2026')
WEEKLY = ('https://www.afa.com.ar/es/posts/'
          'programacion-y-designaciones-arbitrales-para-las-proximas-jornadas-de-ascenso')


def article(body, stamp='10/11/2026'):
    return ('<span class="btArticleDate">' + stamp + '</span>'
            '<div class="btArticleContentInnerInner">' + body + '</div>')


def schedule(pair='Club Azul - Club Verde', tv='', heading='Sábado 14 de noviembre'):
    return article('<h3>PRIMERA NACIONAL</h3><p>' + heading + '</p><p>15.00 ' + pair + tv + '</p>')


class AfaOfficialSourceTests(unittest.TestCase):
    def parse(self, html):
        return parse_afa_fixtures(html, 'Primera Nacional', URL)

    def test_verified_final_uses_explicit_competition_year_and_venue_date(self):
        html = Path(__file__).with_name('fixtures').joinpath('official_afa_primera_nacional_final_2026.html').read_text(encoding='utf8')
        rows = self.parse(html)
        self.assertEqual(1, len(rows))
        self.assertEqual(('Godoy Cruz', 'Tristán Suárez'), (rows[0]['homeTeam'], rows[0]['awayTeam']))
        self.assertEqual(datetime(2026, 11, 15, 17, tzinfo=ART), rows[0]['startTime'])
        self.assertEqual([], rows[0]['broadcasters'])
        for key in ('score', 'homeScore', 'awayScore', 'status', 'venue', 'channels'):
            self.assertNotIn(key, rows[0])

    def test_prefix_clock_publication_year_and_aware_art(self):
        row = self.parse(schedule())[0]
        self.assertEqual('2026-11-14T15:00:00-03:00', row['startTime'].isoformat())
        self.assertEqual('primera-nacional', row['competitionId'])
        self.assertEqual('afa_official', row['source'])
        self.assertEqual(URL, row['sourceUrl'])

    def test_clock_suffix_c_dot_and_numeric_day_month(self):
        html = article('<h3>Primera Nacional</h3><p>Domingo 15-11</p>'
                       '<p>Godoy Cruz c. Tristán Suárez, a las 17.00 horas en Belgrano (Cba.)</p>')
        self.assertEqual(datetime(2026, 11, 15, 17, tzinfo=ART), self.parse(html)[0]['startTime'])

    def test_inline_markup_preserves_names(self):
        html = article('<h3>PRIMERA NACIONAL</h3><p><strong>Sábado 14</strong> de noviembre</p>'
                       '<p><span>15.00 Club Azul - </span><strong>Club Verde</strong></p>')
        self.assertEqual('Club Verde', self.parse(html)[0]['awayTeam'])

    def test_no_broadcast_without_explicit_fixture_tv(self):
        html = schedule() + '<footer>TyC Sports transmite la competición.</footer>'
        self.assertEqual([], self.parse(html)[0]['broadcasters'])

    def test_fixture_tv_is_explicit_preserved_in_order_and_deduplicated(self):
        row = self.parse(schedule(tv=' TV: TyC Sports / ESPN Premium / TyC Sports'))[0]
        self.assertEqual(['TyC Sports', 'ESPN Premium'], row['broadcasters'])

    def test_foreign_tnt_signal_is_not_domestic_confirmation(self):
        self.assertEqual([], self.parse(schedule(tv=' TV: TNT Sports Chile'))[0]['broadcasters'])
        self.assertEqual([], self.parse(schedule(tv=' TV: ESPN Brasil'))[0]['broadcasters'])

    def test_tv_of_next_fixture_is_not_applied_to_previous(self):
        html = schedule().replace('</div>', '<p>17.00 Club Rojo - Club Negro TV: TyC Sports</p></div>')
        rows = self.parse(html)
        self.assertEqual([], rows[0]['broadcasters'])
        self.assertEqual(['TyC Sports'], rows[1]['broadcasters'])

    def test_standalone_tv_label_applies_only_to_fixture_in_same_section(self):
        html = schedule().replace('</div>', '<p>Televisa: TyC Sports</p><h3>PRIMERA B</h3><p>TV: ESPN Premium</p></div>')
        self.assertEqual(['TyC Sports'], self.parse(html)[0]['broadcasters'])

    def test_detached_tv_after_narrative_is_not_fixture_confirmation(self):
        html = schedule().replace('</div>', '<p>Información general de televisación del campeonato.</p><p>TV: TyC Sports</p></div>')
        self.assertEqual([], self.parse(html)[0]['broadcasters'])

    def test_other_categories_never_leak_into_primera_nacional(self):
        for heading in ('PRIMERA B', 'Primera División “B”', 'PRIMERA C', 'Proyección',
                        'Fútbol femenino', 'COPA ARGENTINA', 'PRIMERA DIVISIÓN A',
                        'PRIMERA D', 'Otra competición no reconocida', 'FINAL COPA ARGENTINA',
                        'REDUCIDO DE OTRA COMPETICIÓN'):
            with self.subTest(heading=heading):
                html = schedule().replace('</div>', '<h3>' + heading + '</h3><p>16.00 Other Azul - Other Verde</p></div>')
                self.assertEqual(1, len(self.parse(html)))

    def test_copa_argentina_fixture_and_inline_tv_cannot_enter_primera_nacional(self):
        html = schedule().replace('</div>', '<h3>COPA ARGENTINA</h3><p>17.00 Club Copa - Club Otra TV: TyC Sports</p></div>')
        rows = self.parse(html)
        self.assertEqual(1, len(rows))
        self.assertEqual([], rows[0]['broadcasters'])

    def test_plain_text_other_category_also_ends_section(self):
        html = schedule().replace('</div>', '<p>COPA ARGENTINA</p><p>17.00 Club Copa - Club Otra TV: TyC Sports</p></div>')
        self.assertEqual(1, len(self.parse(html)))

    def test_isolated_pair_line_from_another_competition_is_rejected(self):
        html = article('<h3>PRIMERA B</h3><p>Sábado 14 de noviembre</p><p>15.00 Club Azul - Club Verde</p>')
        with self.assertRaisesRegex(AfaSourceError, 'AFA_CATEGORY_NOT_RECOGNIZED'):
            self.parse(html)

    def test_missing_year_fails_without_using_footer_or_runtime_year(self):
        html = schedule().replace('<span class="btArticleDate">10/11/2026</span>', '')
        with self.assertRaisesRegex(AfaSourceError, 'AFA_MISSING_YEAR'):
            self.parse(html + '<footer>Copyright 2026</footer>')

    def test_year_from_another_category_is_not_used(self):
        html = schedule().replace('<span class="btArticleDate">10/11/2026</span>', '')
        html = html.replace('</div>', '<h3>PRIMERA B 2026</h3></div>')
        with self.assertRaisesRegex(AfaSourceError, 'AFA_MISSING_YEAR'):
            self.parse(html)

    def test_explicit_year_on_date_is_authority_without_publication(self):
        html = schedule(heading='Sábado 14 de noviembre de 2026').replace('<span class="btArticleDate">10/11/2026</span>', '')
        self.assertEqual(2026, self.parse(html)[0]['startTime'].year)

    def test_cross_year_is_not_inferred_from_publication(self):
        html = article('<h3>PRIMERA NACIONAL</h3><p>Viernes 1 de enero</p><p>15.00 Club Azul - Club Verde</p>', '30/12/2026')
        with self.assertRaises(AfaSourceError):
            self.parse(html)

    def test_weekday_conflict_fails_closed(self):
        with self.assertRaisesRegex(AfaSourceError, 'AFA_DATE_WEEKDAY_CONFLICT'):
            self.parse(schedule(heading='Domingo 14 de noviembre'))

    def test_impossible_date_or_clock_fails_closed(self):
        for html in (schedule(heading='Lunes 31 de noviembre'), schedule().replace('15.00', '25.00')):
            with self.subTest(html=html):
                with self.assertRaises(AfaSourceError):
                    self.parse(html)

    def test_scores_and_narrative_are_not_agenda(self):
        html = schedule(pair='Club Azul 2 - 1 Club Verde')
        self.assertEqual([], self.parse(html))
        html = schedule(pair='Estadio Club Azul - Club Verde')
        self.assertEqual([], self.parse(html))

    def test_fixture_without_date_or_clock_is_not_invented(self):
        for body in ('<p>15.00 Club Azul - Club Verde</p>', '<p>Sábado 14 de noviembre</p><p>Club Azul - Club Verde</p>'):
            self.assertEqual([], self.parse(article('<h3>PRIMERA NACIONAL</h3>' + body)))

    def test_exact_duplicate_deduplicates_and_changed_clock_conflicts(self):
        html = schedule().replace('</div>', '<p>15.00 Club Azul - Club Verde</p></div>')
        self.assertEqual(1, len(self.parse(html)))
        with self.assertRaisesRegex(AfaSourceError, 'AFA_CONFLICTING_FIXTURE'):
            self.parse(html.replace('<p>15.00 Club Azul', '<p>16.00 Club Azul', 1))

    def test_source_identity_stays_stable_for_kickoff_update(self):
        first = self.parse(schedule())[0]
        second = self.parse(schedule().replace('15.00', '16.00'))[0]
        self.assertEqual(first['providerFixtureId'], second['providerFixtureId'])
        self.assertTrue(first['providerFixtureId'].startswith('afa:primera-nacional:'))

    def test_source_host_competition_and_unrecognized_layout_fail_closed(self):
        for url in (URL.replace('www.afa.com.ar', 'example.test'), URL.replace('https:', 'http:'), URL + '?key=unsafe'):
            with self.subTest(url=url):
                with self.assertRaisesRegex(AfaSourceError, 'AFA_INVALID_SOURCE'):
                    parse_afa_fixtures(schedule(), 'Primera Nacional', url)
        with self.assertRaisesRegex(AfaSourceError, 'AFA_UNSUPPORTED_COMPETITION'):
            parse_afa_fixtures(schedule(), 'Liga Profesional Argentina', URL)
        with self.assertRaisesRegex(AfaSourceError, 'AFA_UNRECOGNIZED_CONTENT'):
            self.parse('<html>layout changed</html>')

    def test_article_links_are_dynamic_official_only_deduplicated_and_weekly_first(self):
        html = ''.join('<a href="' + url + '">Programación</a>' for url in
                       (URL, WEEKLY, WEEKLY, WEEKLY.replace('www.afa.com.ar', 'untrusted.test'),
                        'https://www.afa.com.ar/es/posts/otra-noticia'))
        self.assertEqual([WEEKLY, URL], afa_article_links(html, 'https://www.afa.com.ar/es/'))
        with self.assertRaisesRegex(AfaSourceError, 'AFA_INVALID_SOURCE'):
            afa_article_links(html, 'https://example.test/')


if __name__ == '__main__':
    unittest.main()
