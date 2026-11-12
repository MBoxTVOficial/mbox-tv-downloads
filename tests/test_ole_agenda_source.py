"""Offline Olé agenda extraction; no searches, API credentials or channel assignment."""
from datetime import date
import json
from pathlib import Path
import unittest

from scripts.broadcaster_sources import DiscoveryError
from scripts.ole_agenda_source import SOURCE_URL, parse_ole_listings

DAY = date(2026, 11, 12)


def event(title='Club Azul vs. Club Verde', competition='Primera Nacional',
          kickoff='2026-11-12T22:00:00Z', channels=None, section='eventos-argentina/futbol'):
    row = {'type': 'event', 'eventTitle': title, 'startDate': kickoff,
           'sections': [{'url': section}], 'tagsEvent': {'name': competition}}
    if channels is not None:
        row['tagsChannel'] = [{'name': channel} for channel in channels]
    return row


def document(events, canonical=SOURCE_URL, query=None):
    payload = {'query': {'match': ['agenda-deportiva'] if query is None else query},
               'props': {'pageProps': {'matchConfig': {'content': events}}}}
    return ('<link rel="canonical" href="' + canonical + '">'
            '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>')


class OleAgendaSourceTests(unittest.TestCase):
    def test_real_minimal_snapshot_preserves_explicit_competitions_and_tv(self):
        html = Path(__file__).with_name('fixtures').joinpath('ole_agenda_2026_11_12.html').read_text(encoding='utf8')
        rows = parse_ole_listings(html, DAY)
        self.assertEqual(3, len(rows))
        self.assertEqual('Turquía', rows[0].home)
        self.assertEqual('Nations League', rows[0].competition)
        self.assertEqual(('ESPN 2', 'Disney + Premium'), rows[0].broadcasters)
        self.assertEqual('Reserva', rows[1].competition)
        self.assertEqual(('LPF PLAY',), rows[1].broadcasters)
        self.assertEqual('Supercopa Argentina', rows[2].competition)
        self.assertEqual(('Disney + Premium',), rows[2].broadcasters)

    def test_kickoff_is_aware_art(self):
        row = parse_ole_listings(document([event()]), DAY)[0]
        self.assertEqual('2026-11-12T19:00:00-03:00', row.kickoff.isoformat())

    def test_next_utc_day_belongs_to_previous_art_date(self):
        html = document([event(kickoff='2026-11-13T00:30:00Z')])
        self.assertEqual('2026-11-12T21:30:00-03:00', parse_ole_listings(html, DAY)[0].kickoff.isoformat())
        self.assertEqual([], parse_ole_listings(html, date(2026, 11, 13)))

    def test_different_day_returns_empty_not_stale_rows(self):
        self.assertEqual([], parse_ole_listings(document([event()]), date(2026, 11, 14)))

    def test_missing_explicit_year_or_timezone_fails_closed(self):
        for kickoff in ('11-12T19:00:00', '2026-11-12T19:00:00', None):
            with self.subTest(kickoff=kickoff):
                with self.assertRaisesRegex(DiscoveryError, 'OLE_INVALID_EVENT_KICKOFF'):
                    parse_ole_listings(document([event(kickoff=kickoff)]), DAY)

    def test_nonfootball_sports_are_excluded(self):
        rows = parse_ole_listings(document([event(section='eventos-argentina/basquet', competition='NBA')]), DAY)
        self.assertEqual([], rows)

    def test_reserva_femenina_nacional_and_primera_are_separate(self):
        competitions = ('Reserva', 'Champions League Femenina', 'Primera Nacional', 'Liga Profesional')
        rows = parse_ole_listings(document([event(competition=value) for value in competitions]), DAY)
        self.assertEqual(set(competitions), {row.competition for row in rows})
        self.assertEqual(4, len(rows))

    def test_blank_or_missing_competition_is_not_invented(self):
        for value in ('', '   ', None):
            with self.subTest(value=value):
                with self.assertRaises(DiscoveryError):
                    parse_ole_listings(document([event(competition=value)]), DAY)

    def test_no_channel_tag_is_valid_empty_broadcaster_agenda(self):
        self.assertEqual((), parse_ole_listings(document([event()]), DAY)[0].broadcasters)

    def test_general_rights_and_other_events_do_not_assign_a_signal(self):
        html = document([event(), event(title='Club Rojo vs Club Negro', channels=['ESPN Premium'])])
        html += '<p>La competición también se transmite por TNT Sports.</p>'
        by_home = {row.home: row for row in parse_ole_listings(html, DAY)}
        self.assertEqual((), by_home['Club Azul'].broadcasters)
        self.assertEqual(('ESPN Premium',), by_home['Club Rojo'].broadcasters)

    def test_multiple_explicit_channels_keep_source_order_and_dedupe(self):
        rows = parse_ole_listings(document([event(channels=['ESPN 2', 'Disney+', 'ESPN 2'])]), DAY)
        self.assertEqual(('ESPN 2', 'Disney+'), rows[0].broadcasters)

    def test_external_platform_is_not_changed_to_a_television_channel(self):
        rows = parse_ole_listings(document([event(channels=['Disney + Premium'])]), DAY)
        self.assertEqual(('Disney + Premium',), rows[0].broadcasters)

    def test_title_vs_variants_and_whitespace_are_explicit_only(self):
        rows = parse_ole_listings(document([event(title=' Barracas Central vs.\tArgentinos Juniors ')]), DAY)
        self.assertEqual(('Barracas Central', 'Argentinos Juniors'), (rows[0].home, rows[0].away))
        for title in ('Club Azul - Club Verde', 'Agenda de hoy', 'Club Azul vs Club Verde vs Club Rojo'):
            with self.subTest(title=title):
                self.assertEqual([], parse_ole_listings(document([event(title=title)]), DAY))

    def test_scores_status_venue_and_metadata_are_never_copied(self):
        row = event()
        row.update({'homeScore': 7, 'status': 'LIVE', 'venue': 'Unverified venue', 'channels': ['wrong']})
        listing = parse_ole_listings(document([row]), DAY)[0]
        self.assertEqual({'home', 'away', 'kickoff', 'competition', 'broadcasters'}, set(vars(listing)))
        self.assertEqual((), listing.broadcasters)

    def test_duplicate_events_merge_only_explicit_tags(self):
        rows = parse_ole_listings(document([event(channels=['ESPN']), event(channels=['ESPN', 'Disney+'])]), DAY)
        self.assertEqual(1, len(rows))
        self.assertEqual(('ESPN', 'Disney+'), rows[0].broadcasters)

    def test_layout_changed_invalid_json_and_wrong_route_fail_closed(self):
        for html in ('<html>layout changed</html>', '<script id="__NEXT_DATA__" type="application/json">{</script>',
                     document([event()], query=['otra-seccion']), document([event()], canonical='https://example.test/agenda-deportiva')):
            with self.subTest(html=html):
                with self.assertRaises(DiscoveryError):
                    parse_ole_listings(html, DAY)

    def test_duplicate_payloads_are_ambiguous(self):
        with self.assertRaisesRegex(DiscoveryError, 'OLE_UNRECOGNIZED_AGENDA'):
            parse_ole_listings(document([event()]) + document([event()]), DAY)

    def test_invalid_channel_structure_or_unsafe_text_fails_closed(self):
        for value in ({'name': 'ESPN'}, [{'name': 'https://example.test/private'}], [1]):
            row = event()
            row['tagsChannel'] = value
            with self.subTest(value=value):
                with self.assertRaises(DiscoveryError):
                    parse_ole_listings(document([row]), DAY)

    def test_recognized_empty_agenda_is_valid(self):
        self.assertEqual([], parse_ole_listings(document([]), DAY))


if __name__ == '__main__':
    unittest.main()
