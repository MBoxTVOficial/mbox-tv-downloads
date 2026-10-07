"""Synthetic fixtures only; no network and no production feed writes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_sports_today as generator
from test_generate_sports_today import DAY, NOW, section_events
from test_sports_classification_and_order import build, match


NATIONAL = "south_america_national_teams"


def friendly(home, away="Benin", **kwargs):
    return match(home=home, away=away, league="Friendlies", **kwargs)


class NationalTeamsTest(unittest.TestCase):
    def assert_section(self, item, section=NATIONAL):
        result = build(item)
        event = section_events(result, section)[0]
        self.assertEqual(item["league"]["name"], event["competition"])
        self.assertEqual(1, sum(len(s["events"]) for s in result["sections"]))
        self.assertEqual(1, result["schemaVersion"])
        return event

    def test_argentina_benin_friendlies(self):
        self.assert_section(friendly("Argentina"))

    def test_benin_argentina_friendlies(self):
        self.assert_section(friendly("Benin", "Argentina"))

    def test_colombia_japan_friendlies(self):
        self.assert_section(friendly("Colombia", "Japón"))

    def test_brazil_usa_friendlies(self):
        self.assert_section(friendly("Brazil", "USA"))

    def test_uruguay_mexico_friendlies(self):
        self.assert_section(friendly("Uruguay", "México"))

    def test_argentina_uruguay_true_qualifiers_stay_qualifiers(self):
        self.assert_section(match(home="Argentina", away="Uruguay"), "south_america_qualifiers")

    def test_brazil_chile_true_qualifiers_stay_qualifiers(self):
        self.assert_section(match(home="Brazil", away="Chile"), "south_america_qualifiers")

    def test_argentina_u20_does_not_activate_rule(self):
        self.assert_section(friendly("Argentina U20"), "international")

    def test_argentina_women_does_not_activate_rule(self):
        self.assert_section(friendly("Argentina Women"), "international")

    def test_colombia_u20_does_not_activate_rule(self):
        self.assert_section(friendly("Colombia U20"), "international")

    def test_croatia_spain_nations_league(self):
        self.assert_section(match(home="Croatia", away="Spain", league="UEFA Nations League"), "uefa_nations")

    def test_chicago_vancouver_mls(self):
        self.assert_section(match(home="Chicago Fire", away="Vancouver", league="Major League Soccer",
                                  country="USA"), "international")

    def test_goias_athletic_brazilian_serie_b(self):
        self.assert_section(match(home="Goiás", away="Athletic Club", league="Serie B", country="Brazil"),
                            "international")

    def test_live_scores_unchanged(self):
        item = friendly("Argentina", status="LIVE")
        item["goals"] = {"home": 1, "away": 0}
        event = self.assert_section(item)
        self.assertEqual(("LIVE", 1, 0), (event["status"], event["homeScore"], event["awayScore"]))

    def test_finished_scores_and_events_retained(self):
        item = friendly("Colombia", status="FT")
        item["goals"] = {"home": 2, "away": 1}
        event = self.assert_section(item)
        self.assertEqual(("FINISHED", 2, 1), (event["status"], event["homeScore"], event["awayScore"]))

    def test_twelve_section_ids_priorities_and_new_title(self):
        result = build()
        self.assertEqual(["south_america_qualifiers", NATIONAL, "uefa_qualifiers", "uefa_nations", "argentina",
                          "conmebol", "champions", "spain", "england", "france", "italy", "international"],
                         [s["id"] for s in result["sections"]])
        self.assertEqual(list(range(1, 13)), [s["priority"] for s in result["sections"]])
        self.assertEqual("Selecciones Sudamericanas", result["sections"][1]["title"])
        self.assertTrue(all(s["events"] == [] for s in result["sections"]))

    def test_all_exact_team_names_normalized_home_and_away(self):
        for name in ("Argentina", "Bolivia", "Brazil", "Brasil", "Chile", "Colombia", "Ecuador",
                     "Paraguay", "Peru", "Perú", "Uruguay", "Venezuela", "  aRgEnTiNa  ", "  PERÚ  "):
            for home, away in ((name, "Benin"), ("Benin", name)):
                with self.subTest(home=home, away=away):
                    self.assert_section(friendly(home, away))

    def test_youth_women_olympic_and_partial_names_excluded(self):
        for name in ("Argentina", "Bolivia", "Brazil", "Brasil", "Chile", "Colombia", "Ecuador",
                     "Paraguay", "Perú", "Uruguay", "Venezuela"):
            for suffix in ("U17", "U20", "U23", "Women", "Olympic", "Reserves", "FC"):
                for home, away in ((f"{name} {suffix}", "Benin"), ("Benin", f"{name} {suffix}")):
                    with self.subTest(home=home, away=away):
                        self.assert_section(friendly(home, away), "international")

    def test_specific_competitions_always_win(self):
        cases = [("World Cup - Qualification South America", "World", "south_america_qualifiers"),
                 ("World Cup - Qualification Europe", "World", "uefa_qualifiers"),
                 ("UEFA Nations League", "World", "uefa_nations"),
                 ("Liga Profesional", "Argentina", "argentina"),
                 ("Copa Libertadores", "World", "conmebol"),
                 ("Copa Sudamericana", "World", "conmebol"),
                 ("Recopa Sudamericana", "World", "conmebol"),
                 ("UEFA Champions League", "World", "champions"),
                 ("La Liga", "Spain", "spain"), ("Premier League", "England", "england"),
                 ("Ligue 1", "France", "france"), ("Serie A", "Italy", "italy")]
        for league, country, section in cases:
            with self.subTest(league=league):
                self.assert_section(match(home="Argentina", away="Colombia", league=league, country=country), section)

    def test_verified_id_wins_before_team_fallback(self):
        self.assert_section(match(league="Translated name", league_id=2), "champions")

    def test_unknown_competition_with_senior_team_is_not_a_qualifier(self):
        self.assert_section(match(league="Unknown competition", home="Argentina", away="Benin"))

    def test_scheduled_null_goals_do_not_invent_scores(self):
        item = friendly("Argentina")
        item["goals"] = {"home": None, "away": None}
        event = self.assert_section(item)
        self.assertNotIn("homeScore", event)
        self.assertNotIn("awayScore", event)

    def test_internal_order_preserves_states_times_and_stable_ties(self):
        fixtures = [friendly("Argentina", fixture_id=1, status="FT", time="11:00"),
                    friendly("Colombia", fixture_id=2, status="FT", time="15:45"),
                    friendly("Brazil", fixture_id=3, status="LIVE", time="20:00"),
                    friendly("Uruguay", fixture_id=4, time="21:30"),
                    friendly("Perú", fixture_id=5, time="21:30"),
                    friendly("Bolivia", fixture_id=6, status="PST", time="10:00"),
                    friendly("Chile", fixture_id=7, status="CANC", time="09:00"),
                    friendly("Paraguay", fixture_id=8, status="TBD", time=None)]
        self.assertEqual(["fixture-3", "fixture-4", "fixture-5", "fixture-8", "fixture-6",
                          "fixture-2", "fixture-1", "fixture-7"],
                         [e["id"] for e in section_events(build(*fixtures), NATIONAL)])


def previous_eleven_feed(status="FT"):
    result = build(friendly("Argentina", status=status))
    national_events = copy.deepcopy(section_events(result, NATIONAL))
    result["sections"] = [s for s in result["sections"] if s["id"] != NATIONAL]
    for index, section in enumerate(result["sections"], 1):
        section["priority"] = index
        if section["id"] == "international":
            section["events"] = national_events
    return result


class PreviousElevenCacheTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(generator.os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "schedule"})
        environment.start()
        self.addCleanup(environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "sports_today.json"

    def save_previous(self, status="FT"):
        previous = previous_eleven_feed(status)
        self.output.write_text(json.dumps(previous), encoding="utf-8")
        return previous

    def test_previous_eleven_sections_accepted(self):
        previous = self.save_previous()
        self.assertEqual(11, len(previous["sections"]))
        self.assertEqual(previous, generator.previous_feed(self.output))

    def test_previous_finished_cache_skips_without_extra_api_call(self):
        self.save_previous()
        original = self.output.read_bytes()
        with patch.object(generator, "fetch_fixtures", side_effect=AssertionError("Unexpected API call")) as fetch:
            with self.assertRaises(generator.RefreshSkipped):
                generator.generate(DAY, self.output, now=NOW, live_only=True)
        fetch.assert_not_called()
        self.assertEqual(original, self.output.read_bytes())

    def test_previous_live_cache_remains_a_candidate(self):
        previous = self.save_previous("LIVE")
        self.assertTrue(generator.needs_live_refresh(generator.previous_feed(self.output), DAY, NOW))
        self.assertEqual(previous, generator.previous_feed(self.output))

    def test_previous_metadata_is_not_relaxed(self):
        invalid = previous_eleven_feed()
        invalid["sections"][0]["title"] = "Invalid title"
        with self.assertRaises(generator.GenerationError):
            generator.validate_feed(invalid)


if __name__ == "__main__":
    unittest.main()
