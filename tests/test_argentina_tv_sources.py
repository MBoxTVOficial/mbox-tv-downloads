"""Offline dated-guide contracts. Listing TV is not rights-holder confirmation."""
from datetime import date, timezone
from pathlib import Path
import unittest

from scripts.argentina_tv_sources import parse_argentina_tv_listings, parse_tyc_listings
from scripts.broadcaster_sources import DiscoveryError, ZONE


DAY = date(2026, 11, 12)
FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def guide_row(home='Club Azul', away='Club Verde', clock='17:00', channels=('ESPN Premium',)):
    tv = '' if channels is None else '<ul class="listaCanales">' + ''.join('<li>' + c + '</li>' for c in channels) + '</ul>'
    return f'<tr><td class="hora">{clock}</td><td class="local">{home}</td><td class="visitante">{away}</td><td>{tv}<meta itemprop="startDate" content="2026-11-12T16:00:00"></td></tr>'


def guide_page(body=None, heading='Partidos de hoy jueves, 12/11/2026', competition='Liga Profesional Argentina'):
    return '<table class="tablaPrincipal"><tr class="cabeceraTabla"><td>' + heading + '</td></tr><tr class="cabeceraCompericion"><td>' + competition + '</td></tr>' + (guide_row() if body is None else body) + '</table>'


def tyc_item(home='Club Azul', away='Club Verde', clock='17:00', channels=('ESPN',)):
    tv = '' if channels is None else '<div class="text-channels-agenda">' + ''.join('<span>' + c + '</span>' for c in channels) + '</div>'
    return f'<div class="item_agenda"><div class="hs-item-agenda">{clock}</div><div class="text-teams-agenda"><img alt="{home}"><h3>{home} VS {away}</h3><img alt="{away}"></div>{tv}</div>'


def tyc_page(body=None, heading='PARTIDOS DE Hoy 12 de Noviembre del 2026', competition='Argentina - Liga Profesional', sport='futbol'):
    return '<div class="agenda_results"><h2>' + heading + '</h2><div class="agenda_comp_results" data-selectdeporte="' + sport + '"><div class="header_comp_agenda"><h2>' + competition + '</h2><a>Fixture</a><a>Posiciones</a></div>' + (tyc_item() if body is None else body) + '</div></div>'


class ArgentinaTvSourceTests(unittest.TestCase):
    def test_real_argentina_table_uses_visible_art_hour_not_structured_clock(self):
        rows = parse_argentina_tv_listings((FIXTURES / 'argentina_tv_futbolenvivo.html').read_text(encoding='utf8'), DAY)
        self.assertEqual(36, len(rows))
        first = rows[0]
        self.assertEqual('Boca Juniors Femenino', first.home)
        self.assertEqual('Campeonato Femenino', first.competition)
        self.assertEqual('2026-11-12T11:00:00-03:00', first.kickoff.isoformat())
        self.assertEqual(('LPF Play (plan plus)',), first.broadcasters)

    def test_real_tyc_keeps_matches_without_tv_and_ignores_basketball(self):
        rows = parse_tyc_listings((FIXTURES / 'argentina_tv_tyc.html').read_text(encoding='utf8'), DAY)
        self.assertEqual(16, len(rows))
        self.assertEqual(3, sum(bool(row.broadcasters) for row in rows))
        cerro = next(row for row in rows if row.home == 'Cerro Largo')
        self.assertEqual((), cerro.broadcasters)
        self.assertEqual('Uruguay - Primera División', cerro.competition)
        self.assertFalse(any('NBA' in row.competition for row in rows))

    def test_future_table_does_not_enter_requested_day(self):
        rows = parse_argentina_tv_listings(guide_page() + guide_page(heading='Mañana 13/11/2026'), DAY)
        self.assertEqual(1, len(rows))

    def test_previous_year_cannot_be_reused(self):
        self.assertEqual([], parse_argentina_tv_listings(guide_page(heading='12/11/2025'), DAY))
        self.assertEqual([], parse_tyc_listings(tyc_page(heading='12 de noviembre del 2025'), DAY))

    def test_explicit_year_is_required(self):
        for parser, body in [(parse_argentina_tv_listings, guide_page(heading='Jueves 12/11')),
                             (parse_tyc_listings, tyc_page(heading='Hoy 12 de noviembre'))]:
            with self.subTest(parser=parser.__name__), self.assertRaises(DiscoveryError):
                parser(body, DAY)

    def test_invalid_dates_fail_closed(self):
        with self.assertRaises(DiscoveryError):
            parse_argentina_tv_listings(guide_page(heading='32/11/2026'), DAY)
        with self.assertRaises(DiscoveryError):
            parse_tyc_listings(tyc_page(heading='32 de noviembre del 2026'), DAY)

    def test_clock_stays_art_even_when_utc_belongs_to_next_day(self):
        row = parse_argentina_tv_listings(guide_page(guide_row(clock='23:30')), DAY)[0]
        self.assertEqual(DAY, row.kickoff.date())
        self.assertEqual(date(2026, 11, 13), row.kickoff.astimezone(timezone.utc).date())
        self.assertEqual(ZONE, row.kickoff.tzinfo)

    def test_argentina_displayed_hour_wins_over_naive_jsonld(self):
        row = parse_argentina_tv_listings(guide_page(), DAY)[0]
        self.assertEqual(17, row.kickoff.hour)
        self.assertEqual(-3 * 3600, row.kickoff.utcoffset().total_seconds())

    def test_no_tv_is_valid_agenda_in_both_guides(self):
        self.assertEqual((), parse_argentina_tv_listings(guide_page(guide_row(channels=None)), DAY)[0].broadcasters)
        self.assertEqual((), parse_tyc_listings(tyc_page(tyc_item(channels=None)), DAY)[0].broadcasters)

    def test_unknown_channel_does_not_become_a_broadcaster(self):
        rows = parse_argentina_tv_listings(guide_page(guide_row(channels=('Canal por confirmar',))), DAY)
        self.assertEqual((), rows[0].broadcasters)

    def test_channel_order_and_duplicate_dedupe(self):
        channels = ('ESPN Premium', 'TNT Sports Premium', 'ESPN Premium')
        row = parse_argentina_tv_listings(guide_page(guide_row(channels=channels)), DAY)[0]
        self.assertEqual(('ESPN Premium', 'TNT Sports Premium'), row.broadcasters)

    def test_repeated_same_source_row_merges_explicit_channels_only_once(self):
        body = guide_row(channels=('ESPN',)) + guide_row(channels=('DSports',))
        rows = parse_argentina_tv_listings(guide_page(body), DAY)
        self.assertEqual(1, len(rows))
        self.assertEqual(('ESPN', 'DSports'), rows[0].broadcasters)

    def test_reserve_female_and_national_categories_are_not_merged(self):
        html = ''.join(guide_page(guide_row(home='Club A', away='Club B'), competition=competition)
                       for competition in ('Liga Profesional Argentina', 'Torneo Proyección', 'Campeonato Femenino', 'Amistoso Sub-21'))
        rows = parse_argentina_tv_listings(html, DAY)
        self.assertEqual(4, len(rows))
        self.assertEqual({'Liga Profesional Argentina', 'Torneo Proyección', 'Campeonato Femenino', 'Amistoso Sub-21'}, {r.competition for r in rows})

    def test_tyc_competition_does_not_include_navigation_text(self):
        row = parse_tyc_listings(tyc_page(competition='Argentina - Primera División Femenina'), DAY)[0]
        self.assertEqual('Argentina - Primera División Femenina', row.competition)

    def test_tyc_brand_never_implies_tyc_transmission(self):
        row = parse_tyc_listings(tyc_page(tyc_item(channels=('ESPN',))), DAY)[0]
        self.assertEqual(('ESPN',), row.broadcasters)
        self.assertNotIn('TyC Sports', row.broadcasters)

    def test_tyc_future_section_is_not_today(self):
        self.assertEqual([], parse_tyc_listings(tyc_page(heading='13 de noviembre del 2026'), DAY))

    def test_tyc_non_football_is_ignored(self):
        self.assertEqual([], parse_tyc_listings(tyc_page(sport='basquet', competition='NBA'), DAY))

    def test_no_usable_clock_or_team_is_not_invented(self):
        for clock in ('A confirmar', '24:00', '17:99'):
            with self.subTest(clock=clock):
                self.assertEqual([], parse_argentina_tv_listings(guide_page(guide_row(clock=clock)), DAY))
                self.assertEqual([], parse_tyc_listings(tyc_page(tyc_item(clock=clock)), DAY))
        self.assertEqual([], parse_argentina_tv_listings(guide_page(guide_row(home='')), DAY))

    def test_stats_table_cannot_supply_tv_rights_to_fixture(self):
        stats = '<table class="tablaPrincipal"><tr><td>ESPN normalmente transmite esta liga</td></tr></table>'
        row = parse_argentina_tv_listings(guide_page(guide_row(channels=None)) + stats, DAY)[0]
        self.assertEqual((), row.broadcasters)

    def test_valid_empty_layout_is_empty_but_changed_layout_is_error(self):
        self.assertEqual([], parse_argentina_tv_listings(guide_page(''), DAY))
        self.assertEqual([], parse_tyc_listings(tyc_page(''), DAY))
        for parser in (parse_argentina_tv_listings, parse_tyc_listings):
            with self.subTest(parser=parser.__name__), self.assertRaises(DiscoveryError):
                parser('<html>Service unavailable</html>', DAY)

    def test_plain_date_and_text_html_required(self):
        with self.assertRaises(DiscoveryError):
            parse_argentina_tv_listings(guide_page().encode(), DAY)
        with self.assertRaises(DiscoveryError):
            parse_tyc_listings(tyc_page(), '2026-11-12')


if __name__ == '__main__':
    unittest.main()
