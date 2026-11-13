"""Source badges propagate only with exact fixture identity; all tests are offline."""
from datetime import date
import json
import unittest

from scripts.argentina_tv_sources import parse_argentina_tv_listings, parse_tyc_listings
from scripts.broadcaster_sources import Listing, instant
from scripts.ole_agenda_source import parse_ole_listings
from scripts.sports_team_logos import accepted_image_url, fixture_logos

DAY = date(2026, 11, 12)
FEV_HOME = 'https://static.futbolenlatv.com/img/32/20130513110846_turquia.webp'
FEV_AWAY = 'https://static.futbolenlatv.com/img/32/20130121023530_belgica.webp'
TYC_HOME = 'https://statics-files.tycsports.com/escudos/457.png?v=6?v=2'
TYC_AWAY = 'https://statics-files.tycsports.com/escudos/446.png?v=6?v=2'
PLACEHOLDER = 'https://statics-files.tycsports.com/frontend/tycsportsv2/img/escudo-err.png'
OLE_HOME = 'https://ole.com.ar/collections/escudos/362.png'
OLE_AWAY = 'https://ole.com.ar/collections/escudos/360.png'
EVENT_PATH = '/eventos-argentina/futbol/turquia-vs-belgica_9_fixture-example.html'
CONFIG = {'argentinaTeamAliases': {}, 'reserveTeamAliases': {}}


def row(provider='futbol-en-vivo-ar', **changes):
    result = {'competition': 'UEFA Nations League', 'homeTeam': 'Turquía', 'awayTeam': 'Bélgica',
              'startTime': '2026-11-12T14:00:00-03:00', 'broadcasters': [], 'guideId': provider,
              'homeLogo': FEV_HOME, 'awayLogo': FEV_AWAY}
    result.update(changes)
    return result


def argentina_html(home_url=FEV_HOME, away_url=FEV_AWAY, alt='Turquía'):
    return ('<table class="tablaPrincipal"><tr class="cabeceraTabla"><td>12/11/2026</td></tr>'
            '<tr class="cabeceraCompericion"><td>UEFA Nations League</td></tr><tr>'
            '<td class="hora">14:00</td><td class="local"><img alt="' + alt + '" src="' + home_url + '">Turquía</td>'
            '<td class="visitante"><img alt="Bélgica" src="' + away_url + '">Bélgica</td>'
            '<td class="listaCanales"></td></tr></table>')


def tyc_html(real=True):
    home = ' data-src="' + TYC_HOME + '"' if real else ''
    away = ' data-src="' + TYC_AWAY + '"' if real else ''
    return ('<section class="agenda_results"><h2>12 de noviembre del 2026</h2>'
            '<div class="agenda_comp_results" data-selectdeporte="futbol">'
            '<header class="header_comp_agenda"><h3>UEFA Nations League - Liga A</h3></header>'
            '<div class="item_agenda"><div class="text-teams-agenda">'
            '<img alt="Turquía" src="' + PLACEHOLDER + '"' + home + '>'
            '<img alt="Bélgica" src="' + PLACEHOLDER + '"' + away + '></div>'
            '<span class="hs-item-agenda">14:00</span></div></div></section>')


def ole_html(path=EVENT_PATH, html_names=('Türkiye', 'BÉLGICA'), logo_urls=(OLE_HOME, OLE_AWAY)):
    event = {'type': 'event', 'url': EVENT_PATH, 'eventTitle': 'Turquía vs Bélgica',
             'sections': [{'url': 'eventos-argentina/futbol'}], 'tagsEvent': {'name': 'Nations League'},
             'startDate': '2026-11-12T17:00:00Z', 'tagsChannel': []}
    payload = {'query': {'match': ['agenda-deportiva']}, 'props': {'pageProps': {'matchConfig': {'content': [event]}}}}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'
    html += '<div class="event-canal-box">'
    for side, name, url in zip(('home', 'visitor'), html_names, logo_urls):
        html += ('<div class="event-info_team_group ' + side + '"><h3 class="event-name status">' + name + '</h3>'
                 '<img alt="' + name + '" src="' + url + '"></div>')
    return html + '<a class="event-link" href="https://www.ole.com.ar' + path + '">Datos</a></div>'


class SportsTeamLogosTests(unittest.TestCase):
    def test_listing_keeps_five_argument_backwards_compatibility(self):
        listing = Listing('Turquía', 'Bélgica', instant('2026-11-12T17:00:00Z'), 'Nations League', ())
        self.assertEqual(('', ''), (listing.home_logo, listing.away_logo))

    def test_exact_public_source_raster_urls_are_preserved_unchanged(self):
        for provider, url in (('futbol-en-vivo-ar', FEV_HOME), ('tyc-agenda', TYC_HOME), ('ole-agenda', OLE_HOME)):
            with self.subTest(url=url):
                self.assertEqual(url, accepted_image_url(provider, url))

    def test_url_credentials_private_hosts_and_unexpected_queries_are_rejected(self):
        urls = ('http://static.futbolenlatv.com/img/32/team.webp',
                'https://user:password@static.futbolenlatv.com/img/32/team.webp',
                'https://127.0.0.1/img/32/team.webp', 'https://localhost/img/32/team.webp',
                FEV_HOME + '?token=not-a-real-token', FEV_HOME + '#fragment',
                FEV_HOME.replace('.com/', '.com:444/'),
                FEV_HOME.replace('/32/', '/32/../'),
                FEV_HOME.replace('/32/', '/32/%2e%2e/'),
                'data:image/png;base64,placeholder', '', None)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual('', accepted_image_url('futbol-en-vivo-ar', url))
        self.assertEqual('', accepted_image_url('tyc-agenda', TYC_HOME + '&api_key=not-a-real-key'))

    def test_logo_provider_scope_and_known_placeholders_are_rejected(self):
        self.assertEqual('', accepted_image_url('ole-agenda', FEV_HOME))
        self.assertEqual('', accepted_image_url('unknown', FEV_HOME))
        self.assertEqual('', accepted_image_url('tyc-agenda', PLACEHOLDER))
        self.assertEqual('', accepted_image_url('futbol-en-vivo-ar', FEV_HOME.replace('20130513110846_turquia', 'placeholder')))
        self.assertEqual('', accepted_image_url('ole-agenda', 'https://www.ole.com.ar/img/escudos/futbolPrimera/boca.svg'))

    def test_fev_badges_are_bound_to_the_exact_home_and_away_cells(self):
        listing = parse_argentina_tv_listings(argentina_html(), DAY)[0]
        self.assertEqual((FEV_HOME, FEV_AWAY), (listing.home_logo, listing.away_logo))
        self.assertEqual((), listing.broadcasters)

    def test_fev_wrong_alt_or_missing_badge_does_not_invent_one(self):
        listing = parse_argentina_tv_listings(argentina_html(alt='Argentina'), DAY)[0]
        self.assertEqual('', listing.home_logo)
        self.assertEqual(FEV_AWAY, listing.away_logo)
        listing = parse_argentina_tv_listings(argentina_html(home_url=''), DAY)[0]
        self.assertEqual('', listing.home_logo)

    def test_duplicate_source_rows_keep_existing_badges(self):
        html = argentina_html(home_url='') + argentina_html()
        listing = parse_argentina_tv_listings(html, DAY)[0]
        self.assertEqual((FEV_HOME, FEV_AWAY), (listing.home_logo, listing.away_logo))

    def test_tyc_uses_lazy_real_badge_instead_of_error_placeholder(self):
        listing = parse_tyc_listings(tyc_html(), DAY)[0]
        self.assertEqual((TYC_HOME, TYC_AWAY), (listing.home_logo, listing.away_logo))

    def test_tyc_placeholder_alone_remains_empty(self):
        listing = parse_tyc_listings(tyc_html(real=False), DAY)[0]
        self.assertEqual(('', ''), (listing.home_logo, listing.away_logo))

    def test_ole_exact_event_link_and_explicit_country_alias_bind_badges(self):
        listing = parse_ole_listings(ole_html(), DAY)[0]
        self.assertEqual((OLE_HOME, OLE_AWAY), (listing.home_logo, listing.away_logo))

    def test_ole_other_event_url_reversed_teams_and_navigation_cannot_supply_badges(self):
        for html in (ole_html(path=EVENT_PATH.replace('fixture-example', 'different-fixture')),
                     ole_html(html_names=('BÉLGICA', 'Türkiye'))):
            with self.subTest(html=html):
                listing = parse_ole_listings(html, DAY)[0]
                self.assertEqual(('', ''), (listing.home_logo, listing.away_logo))
        html = ole_html(logo_urls=('', '')) + '<nav><img alt="Turquía" src="' + OLE_HOME + '"></nav>'
        self.assertEqual('', parse_ole_listings(html, DAY)[0].home_logo)

    def test_ole_conflicting_cards_for_same_event_do_not_choose_random_badge(self):
        html = ole_html()
        second_card = ole_html(logo_urls=(OLE_AWAY, OLE_HOME)).split('</script>', 1)[1]
        listing = parse_ole_listings(html + second_card, DAY)[0]
        self.assertEqual(('', ''), (listing.home_logo, listing.away_logo))

    def test_fixture_logos_use_priority_independently_of_tv_confirmation(self):
        first = row()
        second = row('tyc-agenda', homeLogo=TYC_HOME, awayLogo=TYC_AWAY)
        self.assertEqual({'homeLogo': FEV_HOME, 'awayLogo': FEV_AWAY}, fixture_logos(first, [second, first], CONFIG))

    def test_missing_badge_can_fall_back_only_to_same_exact_fixture(self):
        first = row(awayLogo='')
        second = row('tyc-agenda', homeLogo=TYC_HOME, awayLogo=TYC_AWAY)
        self.assertEqual({'homeLogo': FEV_HOME, 'awayLogo': TYC_AWAY}, fixture_logos(first, [first, second], CONFIG))

    def test_wrong_day_time_competition_or_team_order_never_match(self):
        target = row()
        variants = [row(startTime='2026-11-13T14:00:00-03:00'), row(startTime='2026-11-12T14:01:00-03:00'),
                    row(competition='CONCACAF Nations League'), row(homeTeam='Bélgica', awayTeam='Turquía'),
                    row(homeTeam='Turquía U21')]
        for wrong in variants:
            with self.subTest(wrong=wrong):
                self.assertEqual({'homeLogo': '', 'awayLogo': ''}, fixture_logos(target, [wrong], CONFIG))

    def test_approved_national_alias_can_match_without_fuzzy_team_search(self):
        target = row(homeTeam='Chequia', awayTeam='España')
        source = row(homeTeam='República Checa', awayTeam='España')
        self.assertEqual(FEV_HOME, fixture_logos(target, [source], CONFIG)['homeLogo'])

    def test_legacy_cache_and_unsafe_badges_do_not_break_agenda(self):
        legacy = row()
        del legacy['homeLogo'], legacy['awayLogo']
        self.assertEqual({'homeLogo': '', 'awayLogo': ''}, fixture_logos(row(), [legacy], CONFIG))
        unsafe = row(homeLogo='http://127.0.0.1/private', awayLogo=PLACEHOLDER)
        self.assertEqual({'homeLogo': '', 'awayLogo': ''}, fixture_logos(row(), [unsafe], CONFIG))


if __name__ == '__main__':
    unittest.main()
