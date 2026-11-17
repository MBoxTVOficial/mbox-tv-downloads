"""Offline exact fixture provenance for one bounded TyC article lookup."""
from datetime import date, datetime
from pathlib import Path
import unittest

from scripts.broadcaster_sources import DiscoveryError, Listing, MAX_BODY, ZONE
from scripts.tyc_fixture_tv_source import (
    find_tyc_fixture_tv_link, parse_tyc_fixture_tv, validate_tyc_fixture_tv_url,
)


DAY = date(2026, 11, 17)
URL = ('https://www.tycsports.com/amistosos-de-selecciones/'
       'amistosos-internacionales-masc-2026-argentina-vs-costa-rica-amistosos-id771718.html')
FIXTURES = Path(__file__).with_name('fixtures')
ARTICLE = FIXTURES.joinpath('tyc_fixture_tv_argentina_costa_rica_2026_11_17.html').read_text(encoding='utf8')
AGENDA = FIXTURES.joinpath('tyc_agenda_fixture_link_2026_11_17.html').read_text(encoding='utf8')


def fixture(**changes):
    row = {'homeTeam': 'Argentina', 'awayTeam': 'Costa Rica',
           'competition': 'Amistosos de selecciones', 'startTime': '2026-11-17T20:00:00-03:00'}
    row.update(changes)
    return row


def parse(article=ARTICLE, row=None, agenda=AGENDA, day=DAY, source_url=URL):
    return parse_tyc_fixture_tv(article, fixture() if row is None else row, day,
                                source_url=source_url, agenda_html=agenda)


class TycFixtureTvSourceTests(unittest.TestCase):
    def test_real_dated_agenda_links_exact_fixture(self):
        self.assertEqual(URL, find_tyc_fixture_tv_link(AGENDA, fixture(), DAY))

    def test_real_editorial_body_confirms_owned_tyc_not_telefe_or_dgo(self):
        self.assertEqual(('TyC Sports',), parse())
        self.assertNotIn('Telefe', parse())
        self.assertNotIn('DGO', parse())

    def test_liveblog_start_date_is_not_an_authoritative_fixture_date(self):
        # The observed source has yesterday's LiveBlog startDate but today's
        # exact agenda row, editorial publication and match-data clock.
        self.assertIn('2026-11-16T20:00:00-03:00', ARTICLE)
        self.assertEqual(('TyC Sports',), parse())

    def test_listing_and_utc_equivalent_fixture_have_the_same_identity(self):
        listing = Listing('Argentina', 'Costa Rica', datetime(2026, 11, 17, 20, tzinfo=ZONE),
                          'Amistosos Internacionales', ())
        self.assertEqual(URL, find_tyc_fixture_tv_link(AGENDA, listing, DAY))
        self.assertEqual(('TyC Sports',), parse(row=listing))
        self.assertEqual(('TyC Sports',), parse(row=fixture(startTime='2026-11-17T23:00:00Z')))

    def test_order_date_and_precise_kickoff_must_match(self):
        for row in (fixture(homeTeam='Costa Rica', awayTeam='Argentina'),
                    fixture(startTime='2026-11-17T19:00:00-03:00'),
                    fixture(startTime='2026-11-18T20:00:00-03:00'),
                    fixture(startTime='2026-11-17T20:00:01-03:00')):
            with self.subTest(row=row):
                self.assertIsNone(find_tyc_fixture_tv_link(AGENDA, row, DAY))
                with self.assertRaisesRegex(DiscoveryError, 'DATED_AGENDA_MISMATCH'):
                    parse(row=row)

    def test_youth_women_reserve_unknown_and_other_competitions_not_generalized(self):
        for row in (fixture(homeTeam='Argentina Sub-17'), fixture(homeTeam='Argentina Femenino'),
                    fixture(awayTeam='Costa Rica Reserva'), fixture(homeTeam='Argentinna'),
                    fixture(homeTeam='Boca Juniors'), fixture(competition='Copa Argentina'),
                    fixture(competition='UEFA Nations League'), fixture(homeTeam='Costa Rica')):
            with self.subTest(row=row):
                self.assertIsNone(find_tyc_fixture_tv_link(AGENDA, row, DAY))

    def test_naive_fixture_clock_and_implicit_day_are_rejected(self):
        with self.assertRaisesRegex(DiscoveryError, 'INVALID_FIXTURE'):
            parse(row=fixture(startTime='2026-11-17T20:00:00'))
        for day in ('2026-11-17', datetime(2026, 11, 17)):
            with self.subTest(day=day), self.assertRaisesRegex(DiscoveryError, 'EXPLICIT_DATE_REQUIRED'):
                parse(day=day)

    def test_missing_wrong_dated_or_wrong_competition_agenda_never_proves_article(self):
        for agenda in (AGENDA.replace('17 de Noviembre', '18 de Noviembre'),
                       AGENDA.replace('>Amistosos<', '>Sub-17<'),
                       AGENDA.replace('data-selectdeporte="futbol"', 'data-selectdeporte="basquet"'),
                       AGENDA.replace('20:00', '20:30'),
                       AGENDA.replace('alt="Argentina"', 'alt="Uruguay"')):
            with self.subTest(agenda=agenda):
                self.assertIsNone(find_tyc_fixture_tv_link(agenda, fixture(), DAY))
                with self.assertRaises(DiscoveryError):
                    parse(agenda=agenda)

    def test_agenda_unrecognized_or_undated_layout_fails_closed(self):
        for agenda in ('<h1>TV: TyC Sports</h1>', AGENDA.replace('2026', '26')):
            with self.subTest(agenda=agenda), self.assertRaises(DiscoveryError):
                find_tyc_fixture_tv_link(agenda, fixture(), DAY)

    def test_article_must_be_the_exact_own_link_from_the_matching_row(self):
        different = URL.replace('id771718', 'id771719')
        with self.assertRaisesRegex(DiscoveryError, 'DATED_AGENDA_MISMATCH'):
            parse(source_url=different)
        self.assertIsNone(find_tyc_fixture_tv_link(AGENDA.replace('//www.tycsports.com', '//example.com'), fixture(), DAY))

    def test_ambiguous_links_fail_closed_identical_duplicate_markup_is_deduped(self):
        other = AGENDA.replace('id771718', 'id771719')
        self.assertIsNone(find_tyc_fixture_tv_link(AGENDA + other, fixture(), DAY))
        self.assertEqual(URL, find_tyc_fixture_tv_link(AGENDA + AGENDA, fixture(), DAY))

    def test_exact_canonical_and_one_editorial_article_are_required(self):
        for article in (ARTICLE.replace('rel="canonical"', 'rel="alternate"'),
                        ARTICLE.replace('<body>', '<link rel="canonical" href="' + URL + '"><body>'),
                        ARTICLE.replace('data-name="article-left-1"', 'data-name="other"'),
                        ARTICLE.replace('<h1>', '<h1>Argentina Sub-17 '),
                        ARTICLE.replace('Selección Argentina vs. Costa Rica', 'Costa Rica vs. Argentina')):
            with self.subTest(article=article[:150]), self.assertRaises(DiscoveryError):
                parse(article)

    def test_publication_metadata_day_section_and_headline_must_match(self):
        for article in (ARTICLE.replace('2026-11-17T07:43:53-03:00', '2026-11-16T07:43:53-03:00'),
                        ARTICLE.replace('"articleSection": "Amistosos de Selecciones"', '"articleSection": "Sub-17"'),
                        ARTICLE.replace('"headline": "Selección Argentina', '"headline": "Argentina Sub-17'),
                        ARTICLE.replace('datetime="2026-11-17T07:43:53-03:00"', 'datetime="2026-11-17T08:43:53-03:00"'),
                        ARTICLE.replace('"@type": "NewsArticle"', '"@type": "LiveBlogPosting"')):
            with self.subTest(article=article[:150]), self.assertRaises(DiscoveryError):
                parse(article)

    def test_unrelated_liveblog_comment_or_navigation_tv_never_confirms_channel(self):
        no_owned_body = ARTICLE.replace('TV: TyC Sports', 'TV: DGO')
        for extra in ('<nav>TV: TyC Sports</nav>', '<div class="articleComments">TV: TyC Sports</div>',
                      '<section class="liveBlog"><h2>TV: TyC Sports</h2></section>'):
            with self.subTest(extra=extra):
                self.assertEqual((), parse(no_owned_body.replace('</article>', extra + '</article>')))

    def test_only_exact_owned_channel_body_line_is_accepted(self):
        for channel in ('DGO', 'Telefe', 'ESPN 2', 'TyC Sports Play', 'TyC Sports y Telefe',
                        'TyC Sports por confirmar', 'sin TV', 'TyC Sports Internacional'):
            with self.subTest(channel=channel):
                self.assertEqual((), parse(ARTICLE.replace('TV: TyC Sports', 'TV: ' + channel)))

    def test_body_match_facts_must_be_adjacent_exact_teams_and_clock(self):
        for article in (ARTICLE.replace('Horario: 20.00', 'Horario: 19.00'),
                        ARTICLE.replace('Horario: 20.00', 'Horario: por confirmar'),
                        ARTICLE.replace('<ul>', '<p>TV: TyC Sports</p><ul>', 1),
                        ARTICLE.replace('y datos del partido', 'y derechos generales'),
                        ARTICLE.replace('<li>TV: TyC Sports</li>', '<li>TV: TyC Sports</li><li>TV: DGO</li>')):
            with self.subTest(article=article[:150]), self.assertRaises(DiscoveryError):
                parse(article)

    def test_url_validator_rejects_unsafe_foreign_and_unobserved_routes(self):
        values = ('http:' + URL[6:], URL + '?x=1', URL + '#live', URL + '?', URL + '#', URL.replace('www.', ''),
                  URL.replace('www.tycsports.com', 'www.tycsports.com.evil.test'),
                  URL.replace('https://', 'https://user:pass@'),
                  URL.replace('www.tycsports.com', 'www.tycsports.com:443'),
                  URL.replace('/amistosos-de-selecciones/', '/video/'),
                  URL.replace('id771718', 'id0'), URL.replace('argentina-vs', '../argentina-vs'),
                  URL + '\n', None)
        for value in values:
            with self.subTest(value=value), self.assertRaisesRegex(DiscoveryError, 'UNSAFE_URL'):
                validate_tyc_fixture_tv_url(value)
        self.assertEqual(URL, validate_tyc_fixture_tv_url(URL))
        self.assertEqual(URL, validate_tyc_fixture_tv_url(URL[6:]))

    def test_article_and_agenda_payloads_are_bounded(self):
        with self.assertRaisesRegex(DiscoveryError, 'INVALID_HTML'):
            parse('x' * (MAX_BODY + 1))
        with self.assertRaisesRegex(DiscoveryError, 'INVALID_HTML'):
            find_tyc_fixture_tv_link('x' * (MAX_BODY + 1), fixture(), DAY)


if __name__ == '__main__':
    unittest.main()
