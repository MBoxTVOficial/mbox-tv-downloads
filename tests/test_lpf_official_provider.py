"""Official LPF snapshots only; no internet, credentials or broadcaster inference."""
from datetime import date
from unittest.mock import Mock
import unittest

from scripts.broadcaster_sources import DiscoveryError, lpf_listings, lpf_article_links
from scripts.football_data_provider import ProviderError
from scripts.lpf_official_provider import LpfOfficialProvider, INDEX_URL

DAY = date(2026, 11, 11)
ARTICLE = 'https://www.ligaprofesional.ar/notas/primera/2026/11/09/dias-y-horarios-de-la-fecha/'
OLDER = 'https://www.ligaprofesional.ar/notas/primera/2026/11/08/agenda-de-la-fecha/'


def article(body, year='2026'):
    return '<div class="elementor-widget-theme-post-content"><p>Torneo Clausura ' + year + '</p>' + body + '</div>'


def fixture(clock='19.30', tv='', heading='Miércoles 11 de noviembre'):
    return article('<p>' + heading + '<br>' + clock + ' Club Azul – Club Verde (Zona A)' + tv + '</p>')


def source(pages, links=(ARTICLE,)):
    network = Mock()
    responses = {INDEX_URL: ''.join('<a href="' + url + '">Lo que dejó la fecha</a>' for url in links), **pages}
    network.fetch.side_effect = lambda url: responses[url]
    return LpfOfficialProvider(network), network


class LpfOfficialProviderTests(unittest.TestCase):
    def test_generic_article_is_not_a_successful_empty_agenda(self):
        provider, _ = source({ARTICLE: article('<p>Noticias del torneo.</p>')})
        with self.assertRaisesRegex(ProviderError, '^LPF_OFFICIAL_UNAVAILABLE$'):
            provider.matches('lpf-primera', DAY)

    def test_fixture_without_tv_is_agenda_but_not_broadcaster_evidence(self):
        html = fixture()
        self.assertEqual([], lpf_listings(html, DAY))
        listing = lpf_listings(html, DAY, require_broadcaster=False)[0]
        self.assertEqual((), listing.broadcasters)
        provider, _ = source({ARTICLE: html})
        row = provider.matches('lpf-primera', DAY)[0]
        self.assertEqual('2026-11-11T19:30:00-03:00', row['startTime'])
        self.assertEqual('lpf_official', row['source'])
        self.assertEqual(ARTICLE, row['sourceUrl'])
        for forbidden in ('score', 'status', 'venue', 'channels', 'broadcaster', 'homeScore', 'awayScore'):
            self.assertNotIn(forbidden, row)

    def test_explicit_dashed_dual_signals_are_preserved_in_order(self):
        listing = lpf_listings(fixture(tv=' -TNT Sports / ESPN Premium-'), DAY)[0]
        self.assertEqual(('TNT Sports', 'ESPN Premium'), listing.broadcasters)

    def test_parenthesized_signal_and_team_qualifier(self):
        html = article('<p>Miércoles 11 de noviembre<br>18.30 Gimnasia (Mza.) – Newell’s (Zona A) (TNT Sports)</p>')
        listing = lpf_listings(html, DAY)[0]
        self.assertEqual('Gimnasia (Mza.)', listing.home)
        self.assertEqual('Newell’s', listing.away)

    def test_ordinal_heading_keeps_actual_day(self):
        listing = lpf_listings(fixture(heading='Domingo 1° de noviembre'), date(2026, 11, 1), False)[0]
        self.assertEqual('2026-11-01T19:30:00-03:00', listing.kickoff.isoformat())

    def test_no_year_requires_trusted_current_primera_context(self):
        html = fixture().replace('Clausura 2026', 'Clausura')
        with self.assertRaises(DiscoveryError):
            lpf_listings(html, DAY, False)
        self.assertEqual(1, len(lpf_listings(html, DAY, False, ARTICLE)))
        with self.assertRaises(DiscoveryError):
            lpf_listings(html, DAY, False, ARTICLE.replace('/2026/', '/2025/'))
        with self.assertRaises(DiscoveryError):
            lpf_listings(html, date(2027, 1, 11), False, ARTICLE)

    def test_other_competition_context_is_rejected(self):
        for html, url in ((fixture(), ARTICLE.replace('/primera/', '/proyeccion/')),
                          (fixture().replace('Clausura', 'Clausura de Proyección'), ARTICLE),
                          (fixture(), ARTICLE.replace('www.ligaprofesional.ar', 'example.test'))):
            with self.subTest(url=url):
                with self.assertRaises(DiscoveryError):
                    lpf_listings(html, DAY, False, url)

    def test_results_and_narrative_clock_do_not_become_fixtures(self):
        html = article('<p>Miércoles 11 de noviembre<br>19.30 Club Azul 2 – 1 Club Verde (Zona A)<br>Ambos jugaron a las 18.00 porque Azul – Verde debían descansar.</p>')
        self.assertEqual([], lpf_listings(html, DAY, False))

    def test_source_without_fixture_of_requested_day_returns_empty(self):
        provider, _ = source({ARTICLE: fixture(heading='Jueves 12 de noviembre')})
        self.assertEqual([], provider.matches('lpf-primera', DAY))

    def test_source_failure_is_safe_and_not_retried(self):
        transport = Mock()
        transport.fetch.side_effect = TimeoutError('untrusted response text')
        provider = LpfOfficialProvider(transport)
        with self.assertRaisesRegex(ProviderError, '^LPF_OFFICIAL_UNAVAILABLE$'):
            provider.matches('lpf-primera', DAY)
        self.assertEqual(1, transport.fetch.call_count)

    def test_unrecognized_article_fails_closed(self):
        provider, _ = source({ARTICLE: '<html>layout changed</html>'})
        with self.assertRaisesRegex(ProviderError, '^LPF_OFFICIAL_UNAVAILABLE$'):
            provider.matches('lpf-primera', DAY)

    def test_source_identity_is_stable_when_only_clock_changes(self):
        first, _ = source({ARTICLE: fixture('19.30')})
        second, _ = source({ARTICLE: fixture('20.30')})
        before, after = first.matches('lpf-primera', DAY)[0], second.matches('lpf-primera', DAY)[0]
        self.assertEqual(before['providerFixtureId'], after['providerFixtureId'])
        self.assertNotEqual(before['startTime'], after['startTime'])

    def test_repeated_documents_deduplicate_exact_fixture(self):
        provider, _ = source({ARTICLE: fixture(), OLDER: fixture()}, (ARTICLE, OLDER))
        self.assertEqual(1, len(provider.matches('lpf-primera', DAY)))
        self.assertEqual(3, provider.requests)

    def test_conflicting_official_clocks_fail_closed(self):
        provider, _ = source({ARTICLE: fixture(), OLDER: fixture('20.30')}, (ARTICLE, OLDER))
        with self.assertRaisesRegex(ProviderError, '^LPF_OFFICIAL_AMBIGUOUS$'):
            provider.matches('lpf-primera', DAY)

    def test_per_instance_three_fetch_ceiling_and_cache(self):
        third = ARTICLE.replace('/09/', '/07/')
        provider, network = source({ARTICLE: fixture(), OLDER: fixture(), third: fixture()}, (ARTICLE, OLDER, third))
        rows = provider.matches('lpf-primera', DAY)
        rows[0]['homeTeam'] = 'mutated caller'
        self.assertEqual('Club Azul', provider.matches('lpf-primera', DAY)[0]['homeTeam'])
        self.assertEqual(3, network.fetch.call_count)
        with self.assertRaisesRegex(ProviderError, '^LPF_OFFICIAL_REQUEST_LIMIT$'):
            provider.matches('lpf-primera', date(2026, 11, 12))

    def test_index_uses_changed_title_slug_but_not_other_sections(self):
        html = '<a href="' + ARTICLE + '">Lo que dejó la fecha</a>'
        html += '<a href="' + ARTICLE.replace('/primera/', '/proyeccion/') + '">Agenda</a>'
        html += '<a href="https://example.test/notas/primera/2026/11/09/agenda/">Agenda</a>'
        self.assertEqual([ARTICLE], lpf_article_links(html, INDEX_URL, DAY))

    def test_invalid_competition_never_requests(self):
        network = Mock()
        with self.assertRaisesRegex(ProviderError, '^INVALID_COMPETITION$'):
            LpfOfficialProvider(network).matches('CL', DAY)
        network.fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
