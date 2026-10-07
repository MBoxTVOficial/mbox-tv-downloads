"""Synthetic offline fixtures: qualifier selection, ordering and LIVE eligibility."""
import contextlib
import io
import unittest

from scripts import generate_sports_today as generator
from test_generate_sports_today import DAY, NOW, SECTION_IDS, fixture, response


def match(fixture_id=1, home="Argentina", away="Uruguay",
          league="World Cup Qualification South America", country="World",
          status="NS", time="20:00", league_id=None):
    raw_time = None if time is None else f"{DAY.isoformat()}T{time}:00-03:00"
    item = fixture(fixture_id, league=league, country=country, league_id=league_id,
                   time=raw_time, status=status)
    item["teams"]["home"]["name"] = home
    item["teams"]["away"]["name"] = away
    return item


def build(*fixtures):
    with contextlib.redirect_stdout(io.StringIO()):
        return generator.build_feed(response(*fixtures), DAY, NOW)[0]


def events(feed, section="south_america_qualifiers"):
    return next(item["events"] for item in feed["sections"] if item["id"] == section)


class QualifierClassificationTest(unittest.TestCase):
    def assert_section(self, item, section, title):
        result = build(item)
        self.assertEqual([f"fixture-{item['fixture']['id']}"],
                         [event["id"] for event in events(result, section)])
        self.assertEqual(title, next(s["title"] for s in result["sections"] if s["id"] == section))
        self.assertEqual(1, sum(len(s["events"]) for s in result["sections"]))

    def test_argentina_vs_uruguay(self):
        self.assert_section(match(), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_brazil_vs_argentina(self):
        self.assert_section(match(home="Brasil", away="Argentina"), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_brazil_vs_uruguay(self):
        self.assert_section(match(home="Brasil"), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_chile_vs_colombia(self):
        self.assert_section(match(home="Chile", away="Colombia"), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_argentina_u20_does_not_trigger_senior_rule(self):
        self.assert_section(match(home="Argentina U20"), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_argentina_women_does_not_trigger_senior_rule(self):
        self.assert_section(match(home="Argentina Women"), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_argentina_olympic_away_does_not_trigger_senior_rule(self):
        self.assert_section(match(home="Brasil", away="Argentina Olympic"), "south_america_qualifiers", "Eliminatorias Sudamericanas")

    def test_libertadores_is_still_conmebol(self):
        self.assert_section(match(league="Copa Libertadores"), "conmebol", "Copas CONMEBOL")

    def test_liga_profesional_is_still_argentina(self):
        self.assert_section(match(league="Liga Profesional", country="Argentina"),
                            "argentina", "Fútbol - Argentina")

    def test_champions_is_still_champions(self):
        self.assert_section(match(league="UEFA Champions League"), "champions", "Champions League")

    def test_all_qualifier_aliases_share_one_section(self):
        for name in ("World Cup - Qualification South America",
                     "World Cup Qualification South America", "CONMEBOL World Cup Qualifiers",
                     "  cOnMeBoL WORLD CUP QUALIFIERS  ", "WORLD CUP: QUALIFICATION SOUTH AMERICA"):
            for home, away in (("Argentina", "Chile"), ("Brasil", "Argentina"), ("Paraguay", "Ecuador")):
                with self.subTest(name=name, home=home, away=away):
                    self.assertEqual("south_america_qualifiers",
                                     generator.classify_fixture(match(home=home, away=away, league=name)))

    def test_team_names_do_not_change_qualifier_section(self):
        self.assert_section(match(home="  ARGENTÍNA  "), "south_america_qualifiers", "Eliminatorias Sudamericanas")
        for name in ("Argentina U20", "Argentina Women", "Argentina Olympic", "Club Argentina", "Argentinas"):
            with self.subTest(name=name):
                self.assertEqual("south_america_qualifiers", generator.classify_fixture(match(home=name)))

    def test_no_senior_override_outside_south_american_qualifiers(self):
        self.assert_section(match(league="World Cup - Qualification Europe"),
                            "uefa_qualifiers", "Eliminatorias UEFA")
        self.assertEqual("international", generator.classify_fixture(match(league="Unrecognized competition")))

    def test_verified_league_ids_still_take_precedence(self):
        self.assertEqual("champions", generator.classify_fixture(match(league_id=2)))

    def test_missing_team_in_relevant_fixture_remains_an_error(self):
        item = match()
        del item["teams"]["home"]
        with self.assertRaises(generator.GenerationError):
            build(item)

    def test_classification_logs_are_bounded_decisions_and_ids(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            generator.build_feed(response(match(1), match(2, home="Brasil")), DAY, NOW)
        text = output.getvalue()
        self.assertIn("SPORTS_CLASSIFY QUALIFIERS_SOUTH_AMERICA fixtureId=1", text)
        self.assertIn("SPORTS_CLASSIFY QUALIFIERS_SOUTH_AMERICA fixtureId=2", text)
        self.assertNotIn("Uruguay", text)
        self.assertNotIn("World Cup", text)


class EventOrderingTest(unittest.TestCase):
    def assert_order(self, fixtures, expected):
        result = build(*fixtures)
        self.assertEqual([f"fixture-{n}" for n in expected],
                         [event["id"] for event in events(result)])
        self.assertEqual(len(fixtures), len(events(result)))  # Finished events remain present.

    def test_requested_five_event_example(self):
        self.assert_order([match(1, status="FT", time="11:00"),
                           match(2, status="FT", time="15:45"),
                           match(3, status="LIVE", time="20:00"),
                           match(4, status="NS", time="21:30"),
                           match(5, status="NS", time="23:00")], [3, 4, 5, 2, 1])

    def test_multiple_live_ascending_and_stable_ties(self):
        self.assert_order([match(1, status="HT", time="20:00"),
                           match(2, status="2H", time="18:00"),
                           match(3, status="1H", time="18:00")], [2, 3, 1])

    def test_multiple_scheduled_ascending_and_stable_ties(self):
        self.assert_order([match(1, time="23:00"), match(2, time="21:30"),
                           match(3, time="20:00"), match(4, time="20:00")], [3, 4, 2, 1])

    def test_multiple_finished_descending_and_stable_ties(self):
        self.assert_order([match(1, status="FT", time="11:00"),
                           match(2, status="PEN", time="18:00"),
                           match(3, status="AET", time="18:00"),
                           match(4, status="FT", time="15:45")], [2, 3, 4, 1])

    def test_postponed_special_finished_cancelled_groups(self):
        self.assert_order([match(1, status="CANC", time="08:00"),
                           match(2, status="FT", time="09:00"),
                           match(3, status="PST", time="10:00"),
                           match(4, status="UNKNOWN", time="11:00"),
                           match(5, status="NS", time="22:00"),
                           match(6, status="LIVE", time="23:00")], [6, 5, 3, 4, 2, 1])

    def test_empty_and_tbd_last_only_within_their_group(self):
        self.assert_order([match(1, status="FT", time=None),
                           match(2, status="FT", time="15:45"),
                           match(3, status="TBD", time="00:00"),
                           match(4, status="NS", time="21:30"),
                           match(5, status="LIVE", time=None),
                           match(6, status="LIVE", time="20:00"),
                           match(7, status="PST", time=None),
                           match(8, status="PST", time="19:00"),
                           match(9, status="CANC", time=None),
                           match(10, status="CANC", time="09:00")], [6, 5, 4, 3, 8, 7, 2, 1, 10, 9])

    def test_live_started_two_hours_ago_precedes_scheduled(self):
        self.assert_order([match(1, time="20:30"), match(2, status="2H", time="18:00")], [2, 1])

    def test_each_section_sorted_independently_preserving_section_order(self):
        fixtures = [match(1, home="Brasil", league="Copa Libertadores", status="FT", time="11:00"),
                    match(2, status="FT", time="11:00"),
                    match(3, home="Brasil", league="Copa Libertadores", status="LIVE", time="20:00"),
                    match(4, status="LIVE", time="20:00")]
        result = build(*fixtures)
        self.assertEqual(SECTION_IDS,
                         [s["id"] for s in result["sections"]])
        self.assertEqual(["fixture-4", "fixture-2"], [e["id"] for e in events(result)])
        self.assertEqual(["fixture-3", "fixture-1"], [e["id"] for e in events(result, "conmebol")])

    def test_sort_logs_counts(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            generator.build_feed(response(match(1, status="LIVE"), match(2), match(3, status="FT")), DAY, NOW)
        self.assertIn("SPORTS_SORT section=south_america_qualifiers live=1 scheduled=1 finished=1", output.getvalue())


class QualifierLiveEligibilityTest(unittest.TestCase):
    def test_all_aliases_with_argentina_home_away_and_other_teams_are_live_candidates(self):
        for name in ("World Cup - Qualification South America", "World Cup Qualification South America",
                     "CONMEBOL World Cup Qualifiers"):
            for home, away in (("Argentina", "Brasil"), ("Brasil", "Argentina"), ("Chile", "Colombia")):
                with self.subTest(name=name, home=home):
                    previous = build(match(home=home, away=away, league=name, status="LIVE"))
                    self.assertTrue(generator.needs_live_refresh(previous, DAY, NOW))

    def test_argentina_scheduled_soon_is_a_live_candidate(self):
        previous = build(match(time="12:05"))
        self.assertTrue(generator.needs_live_refresh(previous, DAY, NOW))

    def test_finished_qualifier_is_retained_but_not_a_live_candidate(self):
        previous = build(match(status="FT", time="11:00"))
        self.assertEqual(1, len(events(previous)))
        self.assertFalse(generator.needs_live_refresh(previous, DAY, NOW))

    def test_scores_remain_from_goals_with_classification_and_ordering(self):
        live = match(1, status="LIVE")
        live["goals"] = {"home": 2, "away": 1}
        finished = match(2, status="FT", time="11:00")
        finished["goals"] = {"home": 3, "away": 2}
        scheduled = match(3, time="21:30")
        scheduled["goals"] = {"home": None, "away": None}
        result = events(build(finished, scheduled, live))
        self.assertEqual(["fixture-1", "fixture-3", "fixture-2"], [e["id"] for e in result])
        self.assertEqual((2, 1), (result[0]["homeScore"], result[0]["awayScore"]))
        self.assertNotIn("homeScore", result[1])
        self.assertNotIn("awayScore", result[1])
        self.assertEqual((3, 2), (result[2]["homeScore"], result[2]["awayScore"]))


if __name__ == "__main__":
    unittest.main()
