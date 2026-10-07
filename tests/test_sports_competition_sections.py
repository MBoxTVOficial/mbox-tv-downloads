"""Offline classification and schemaVersion 1 transition regression tests."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_sports_today as generator
from test_generate_sports_today import DAY, NOW, response, section_events
from test_sports_classification_and_order import build, match


EXPECTED_SECTIONS = [
    ("south_america_qualifiers", "Eliminatorias Sudamericanas", 1),
    ("uefa_qualifiers", "Eliminatorias UEFA", 2),
    ("uefa_nations", "UEFA Nations League", 3),
    ("argentina", "Fútbol - Argentina", 4),
    ("conmebol", "Copas CONMEBOL", 5),
    ("champions", "Champions League", 6),
    ("spain", "Liga de España", 7),
    ("england", "Premier League", 8),
    ("france", "Liga de Francia", 9),
    ("italy", "Serie A", 10),
    ("international", "Fútbol - Internacional", 11),
]


class CompetitionSectionsTest(unittest.TestCase):
    def assert_section(self, item, expected):
        result = build(item)
        self.assertEqual(1, len(section_events(result, expected)))
        self.assertEqual(1, sum(len(s["events"]) for s in result["sections"]))
        self.assertEqual(1, result["schemaVersion"])

    def test_argentina_uruguay_in_south_american_section(self):
        self.assert_section(match(), "south_america_qualifiers")

    def test_brazil_chile_in_south_american_section(self):
        self.assert_section(match(home="Brazil", away="Chile"), "south_america_qualifiers")

    def test_colombia_ecuador_in_south_american_section(self):
        self.assert_section(match(home="Colombia", away="Ecuador"), "south_america_qualifiers")

    def test_argentina_variants_never_choose_argentine_club_section(self):
        for name in ("Argentina", "Argentina U20", "Argentina Women", "Argentina Olympic", "Club Argentina"):
            with self.subTest(name=name):
                self.assert_section(match(home=name), "south_america_qualifiers")
                self.assert_section(match(home=name, league="Unknown competition"), "international")

    def test_south_american_aliases_normalized_without_new_ids(self):
        for name in ("CONMEBOL World Cup Qualifiers", "World Cup - Qualification South America",
                     "World Cup Qualification CONMEBOL", "South America World Cup Qualifiers",
                     "  world CUP: Qualification conmebol  "):
            with self.subTest(name=name):
                self.assert_section(match(league=name), "south_america_qualifiers")
        self.assertEqual((), generator.SOUTH_AMERICAN_QUALIFIERS.ids)

    def test_libertadores_stays_in_club_conmebol_section(self):
        self.assert_section(match(league="Copa Libertadores"), "conmebol")

    def test_sudamericana_stays_in_club_conmebol_section(self):
        self.assert_section(match(league="Copa Sudamericana"), "conmebol")

    def test_argentine_club_competitions_remain_argentina(self):
        for name in ("Liga Profesional", "Copa Argentina", "Primera Nacional"):
            with self.subTest(name=name):
                self.assert_section(match(home="River Plate", away="Boca", league=name, country="Argentina"),
                                    "argentina")

    def test_la_liga_and_spanish_primera_division(self):
        for name in ("La Liga", "Primera División"):
            with self.subTest(name=name):
                self.assert_section(match(league=name, country="Spain"), "spain")

    def test_ligue_1_france(self):
        self.assert_section(match(league="Ligue 1", country="France"), "france")

    def test_premier_league_england(self):
        self.assert_section(match(league="Premier League", country="England"), "england")

    def test_serie_a_italy(self):
        self.assert_section(match(league="Serie A", country="Italy"), "italy")

    def test_national_league_lookalikes_do_not_enter_specific_sections(self):
        for league, country in (("Serie A", "Brazil"), ("Premier League", "Egypt"),
                                ("Ligue 1", "Algeria"), ("Primera Division", "Chile"),
                                ("Championship", "England"), ("Unknown Spain League", "Spain")):
            with self.subTest(league=league, country=country):
                self.assert_section(match(league=league, country=country), "international")

    def test_champions_stays_separate(self):
        self.assert_section(match(league="UEFA Champions League"), "champions")

    def test_only_existing_verified_ids_are_used(self):
        for league_id, expected in ((2, "champions"), (39, "england"), (140, "spain")):
            with self.subTest(league_id=league_id):
                self.assert_section(match(league="Translated name", league_id=league_id), expected)
        self.assertEqual({2, 39, 140}, {lid for _, _, _, rules in generator.SECTIONS
                                      for rule in rules for lid in rule.ids})

    def test_uefa_world_cup_and_euro_qualifiers(self):
        for league in ("World Cup - Qualification Europe", "World Cup Qualification UEFA",
                       "UEFA World Cup Qualifiers", "Euro Championship - Qualification",
                       "UEFA Euro Qualifiers", "UEFA European Championship Qualification"):
            with self.subTest(league=league):
                self.assert_section(match(home="Spain", away="Croatia", league=league), "uefa_qualifiers")

    def test_nations_league_has_own_section(self):
        self.assert_section(match(home="France", away="Germany", league="UEFA Nations League"), "uefa_nations")

    def test_euro_finals_are_not_qualifiers(self):
        self.assert_section(match(league="Euro Championship"), "international")

    def test_mls_remains_international(self):
        self.assert_section(match(home="Chicago Fire", away="Vancouver Whitecaps",
                                  league="Major League Soccer", country="USA"), "international")

    def test_unknown_competition_is_international(self):
        self.assert_section(match(league="Unconfigured competition"), "international")

    def test_international_friendlies_are_residual(self):
        self.assert_section(match(league="Friendlies"), "international")

    def test_live_score_and_event_shape_unchanged(self):
        item = match(status="LIVE")
        item["goals"] = {"home": 1, "away": 0}
        event = section_events(build(item), "south_america_qualifiers")[0]
        self.assertEqual(("LIVE", 1, 0), (event["status"], event["homeScore"], event["awayScore"]))
        self.assertEqual({"id", "sport", "competition", "homeTeam", "awayTeam", "homeLogo", "awayLogo",
                          "startTime", "status", "homeScore", "awayScore"}, set(event))

    def test_finished_score_unchanged(self):
        item = match(home="Spain", away="Croatia", league="World Cup - Qualification Europe", status="FT")
        item["goals"] = {"home": 2, "away": 1}
        event = section_events(build(item), "uefa_qualifiers")[0]
        self.assertEqual(("FINISHED", 2, 1), (event["status"], event["homeScore"], event["awayScore"]))

    def test_section_metadata_and_visual_order(self):
        result = build()
        self.assertEqual(EXPECTED_SECTIONS, [(s["id"], s["title"], s["priority"]) for s in result["sections"]])
        self.assertTrue(all(s["events"] == [] for s in result["sections"]))
        generator.validate_feed(result)

    def test_internal_order_live_scheduled_finished_still_applies(self):
        result = build(match(1, status="FT", time="11:00"), match(2, status="FT", time="15:45"),
                       match(3, status="LIVE", time="20:00"), match(4, time="21:30"), match(5, time="23:00"))
        self.assertEqual(["fixture-3", "fixture-4", "fixture-5", "fixture-2", "fixture-1"],
                         [e["id"] for e in section_events(result, "south_america_qualifiers")])


def legacy_feed(status="FT"):
    result = build(match(league="Liga Profesional", country="Argentina", status=status))
    argentina_events = copy.deepcopy(section_events(result))
    result["sections"] = [
        {"id": sid, "title": title, "priority": priority,
         "events": argentina_events if sid == "argentina" else []}
        for sid, title, priority in (("argentina", "Fútbol - Argentina", 1),
                                    ("conmebol", "Copas CONMEBOL", 2),
                                    ("champions", "Champions League", 3),
                                    ("international", "Fútbol - Internacional", 4))
    ]
    return result


class LegacyCacheCompatibilityTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "sports_today.json"

    def save_legacy(self, status="FT"):
        previous = legacy_feed(status)
        self.output.write_text(json.dumps(previous), encoding="utf-8")
        return previous

    def test_legacy_four_section_cache_remains_readable(self):
        previous = self.save_legacy()
        self.assertEqual(previous, generator.previous_feed(self.output))

    def test_legacy_finished_cache_does_not_trigger_extra_api_call(self):
        self.save_legacy()
        original = self.output.read_bytes()
        with patch.object(generator, "fetch_fixtures") as fetch:
            with self.assertRaises(generator.RefreshSkipped):
                generator.generate(DAY, self.output, now=NOW, live_only=True)
        fetch.assert_not_called()
        self.assertEqual(original, self.output.read_bytes())

    def test_legacy_same_day_empty_response_keeps_guard(self):
        self.save_legacy("LIVE")
        original = self.output.read_bytes()
        with patch.object(generator, "fetch_fixtures", return_value=response()), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(generator.GenerationError, "Agenda vacía"):
                generator.generate(DAY, self.output, now=NOW)
        self.assertEqual(original, self.output.read_bytes())

    def test_next_successful_generation_migrates_sections(self):
        self.save_legacy()
        item = match()
        with patch.object(generator, "fetch_fixtures", return_value=response(item)) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(DAY, self.output, now=NOW)
        fetch.assert_called_once_with(DAY)
        self.assertTrue(result[-1])
        self.assertEqual(11, len(result[0]["sections"]))
        self.assertEqual(1, len(section_events(result[0], "south_america_qualifiers")))

    def test_legacy_metadata_is_still_validated(self):
        invalid = legacy_feed()
        invalid["sections"][0]["priority"] = 99
        with self.assertRaises(generator.GenerationError):
            generator.validate_feed(invalid)


if __name__ == "__main__":
    unittest.main()
