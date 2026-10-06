import contextlib
from datetime import date, datetime, timedelta, timezone
import io
from http.client import IncompleteRead
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from scripts import generate_sports_today as generator

DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone(timedelta(hours=-3)))


def fixture(fixture_id=100, league="Liga Profesional Argentina", country="Argentina",
            league_id=None, time="2026-10-06T20:00:00-03:00", status="NS"):
    return {
        "fixture": {"id": fixture_id, "date": time, "status": {"short": status}},
        "league": {"id": league_id, "name": league, "country": country},
        "teams": {
            "home": {"name": "Local", "logo": "https://example.test/home.png"},
            "away": {"name": "Visitante", "logo": "https://example.test/away.png"},
        },
    }


def response(*fixtures):
    return {"errors": [], "results": len(fixtures), "paging": {"current": 1, "total": 1},
            "response": list(fixtures)}


def feed(*fixtures, day=DAY, now=NOW):
    return generator.build_feed(response(*fixtures), day, now)[0]


class ClassificationTest(unittest.TestCase):
    def classify(self, name, country="World", league_id=None):
        return generator.classify_league({"id": league_id, "name": name, "country": country})

    def test_argentina_major_competitions(self):
        for name in ("Liga Profesional Argentina", "Primera División", "Copa Argentina",
                     "Supercopa Argentina", "Primera Nacional", "Copa de la Liga Profesional"):
            with self.subTest(name=name):
                self.assertEqual("argentina", self.classify(name, "Argentina"))

    def test_libertadores(self):
        self.assertEqual("conmebol", self.classify("CONMEBOL Libertadores"))
        self.assertEqual("conmebol", self.classify("Copa Libertadores"))

    def test_sudamericana_and_recopa(self):
        self.assertEqual("conmebol", self.classify("Copa Sudamericana"))
        self.assertEqual("conmebol", self.classify("Recopa Sudamericana"))

    def test_champions(self):
        self.assertEqual("champions", self.classify("UEFA Champions League"))

    def test_premier_league(self):
        self.assertEqual("international", self.classify("Premier League", "England"))

    def test_irrelevant_league_is_ignored_even_in_argentina(self):
        self.assertIsNone(self.classify("Torneo Regional Amateur", "Argentina"))
        self.assertIsNone(self.classify("National League", "England"))

    def test_country_prevents_ambiguous_name_matches(self):
        self.assertIsNone(self.classify("Premier League", "Egypt"))
        self.assertIsNone(self.classify("Serie A", "Brazil"))
        self.assertIsNone(self.classify("Primera Division", "Chile"))

    def test_verified_ids_take_precedence_over_translated_names(self):
        self.assertEqual("champions", self.classify("Translated", league_id=2))
        self.assertEqual("international", self.classify("Traducido", league_id=39))
        self.assertEqual("international", self.classify("Traducido", league_id=140))

    def test_normalized_accents_case_and_punctuation(self):
        self.assertEqual("argentina", self.classify("  PRIMERA   DIVISIÓN ", "ARGENTINA"))
        self.assertEqual("conmebol", self.classify("Copa-Sudamericana"))


class TransformationTest(unittest.TestCase):
    def test_ns_is_scheduled(self):
        self.assertEqual("SCHEDULED", generator.normalize_status("NS"))

    def test_first_half_is_live(self):
        self.assertEqual("LIVE", generator.normalize_status("1H"))

    def test_ft_is_finished(self):
        self.assertEqual("FINISHED", generator.normalize_status("FT"))

    def test_all_requested_statuses_and_unknown_values(self):
        groups = {"SCHEDULED": ("NS", "TBD"), "POSTPONED": ("PST",),
                  "CANCELLED": ("CANC", "ABD"),
                  "LIVE": ("1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"),
                  "FINISHED": ("FT", "AET", "PEN")}
        for expected, statuses in groups.items():
            for status in statuses:
                with self.subTest(status=status):
                    self.assertEqual(expected, generator.normalize_status(status))
        self.assertEqual("NEW_STATUS", generator.normalize_status("NEW_STATUS"))
        self.assertEqual("UNKNOWN", generator.normalize_status(None))

    def test_chronological_order_unknown_time_last_and_ties_stable(self):
        result = feed(fixture(1, time="2026-10-06T22:00:00-03:00"),
                      fixture(2, status="TBD", time="2026-10-06T00:00:00-03:00"),
                      fixture(3, time="2026-10-06T16:00:00-03:00"),
                      fixture(4, time="2026-10-06T16:00:00-03:00"))
        events = result["sections"][0]["events"]
        self.assertEqual(["fixture-3", "fixture-4", "fixture-1", "fixture-2"],
                         [event["id"] for event in events])
        self.assertEqual("", events[-1]["startTime"])

    def test_logos_are_preserved(self):
        event = feed(fixture())["sections"][0]["events"][0]
        self.assertEqual("https://example.test/home.png", event["homeLogo"])
        self.assertEqual("https://example.test/away.png", event["awayLogo"])

    def test_fixture_id_becomes_event_id(self):
        self.assertEqual("fixture-123456", feed(fixture(123456))["sections"][0]["events"][0]["id"])

    def test_empty_sections_and_zero_results_stay_valid(self):
        result = feed()
        self.assertEqual(["argentina", "conmebol", "champions", "international"],
                         [section["id"] for section in result["sections"]])
        self.assertEqual([1, 2, 3, 4], [section["priority"] for section in result["sections"]])
        self.assertTrue(all(section["events"] == [] for section in result["sections"]))
        self.assertFalse(result["demo"])
        generator.validate_feed(result)

    def test_utc_kickoff_uses_argentina_day_and_clock(self):
        result = feed(fixture(time="2026-10-07T01:15:00Z"))
        self.assertEqual("22:15", result["sections"][0]["events"][0]["startTime"])
        other_day = fixture(101, time="2026-10-06T01:15:00Z")
        result, ignored = generator.build_feed(response(other_day), DAY, NOW)
        self.assertEqual(1, ignored)
        self.assertEqual([], result["sections"][0]["events"])

    def test_updated_at_has_argentina_offset(self):
        result = feed(now=datetime(2026, 10, 6, 15, tzinfo=timezone.utc))
        self.assertEqual("2026-10-06T12:00:00-03:00", result["updatedAt"])

    def test_missing_iana_database_needs_no_external_dependency(self):
        with patch.object(generator, "ZoneInfo", side_effect=generator.ZoneInfoNotFoundError):
            self.assertEqual(timedelta(hours=-3), generator.argentina_timezone().utcoffset(None))

    def test_selected_and_ignored_counts(self):
        result, ignored = generator.build_feed(response(fixture(), fixture(101, league="Minor League")), DAY, NOW)
        self.assertEqual(1, ignored)
        self.assertEqual(1, len(result["sections"][0]["events"]))

    def test_invalid_relevant_fixture_or_duplicate_id_fails(self):
        invalid = fixture()
        del invalid["teams"]["home"]["name"]
        with self.assertRaises(generator.GenerationError):
            feed(invalid)
        with self.assertRaises(generator.GenerationError):
            feed(fixture(), fixture())
        with self.assertRaises(generator.GenerationError):
            feed(fixture(time="not-a-date"))


class GenerationSafetyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "sports_today.json"
        self.output.write_bytes(b"ORIGINAL FEED")
        self.input = self.root / "fixtures.json"

    def write_input(self, payload):
        self.input.write_text(json.dumps(payload), encoding="utf-8")

    def test_response_errors_never_write_output(self):
        payload = response()
        payload["errors"] = {"token": "sensitive-remote-message"}
        self.write_input(payload)
        with self.assertRaises(generator.GenerationError) as caught:
            generator.generate(DAY, self.output, self.input, NOW)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_invalid_json_never_replaces_existing_output(self):
        self.input.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(generator.GenerationError):
            generator.generate(DAY, self.output, self.input, NOW)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_inconsistent_results_missing_errors_or_pagination_fail(self):
        for payload in ({"results": 0, "response": []},
                        {**response(), "results": 1},
                        {**response(), "paging": {"current": 1, "total": 2}},
                        {**response(), "results": False}):
            with self.subTest(payload=payload):
                self.write_input(payload)
                with self.assertRaises(generator.GenerationError):
                    generator.generate(DAY, self.output, self.input, NOW)
                self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_offline_input_never_calls_api(self):
        self.write_input(response(fixture()))
        with patch.object(generator, "fetch_fixtures") as fetch:
            generated, received, ignored, changed = generator.generate(DAY, self.output, self.input, NOW)
        fetch.assert_not_called()
        self.assertEqual((1, 0, True), (received, ignored, changed))
        self.assertEqual(generated, json.loads(self.output.read_text(encoding="utf-8")))

    def test_offline_input_cannot_replace_real_feed(self):
        self.write_input(response(fixture()))
        with patch.object(generator, "REAL_OUTPUT", self.output):
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output, self.input, NOW)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_missing_api_key_fails_without_network_or_output_write(self):
        with patch.dict(generator.os.environ, {}, clear=True), patch.object(generator, "build_opener") as opener:
            with self.assertRaisesRegex(generator.GenerationError, "API_FOOTBALL_KEY"):
                generator.generate(DAY, self.output)
        opener.assert_not_called()
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_http_failure_keeps_output_and_hides_remote_body(self):
        failure = HTTPError(generator.ENDPOINT, 429, "Quota", {}, io.BytesIO(b"sensitive-body"))
        with patch.dict(generator.os.environ, {"API_FOOTBALL_KEY": "unit-test-placeholder"}), \
                patch.object(generator, "build_opener") as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaisesRegex(generator.GenerationError, "HTTP 429") as caught:
                generator.generate(DAY, self.output)
        self.assertNotIn("sensitive-body", str(caught.exception))
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_http_200_with_invalid_json_keeps_output(self):
        with patch.dict(generator.os.environ, {"API_FOOTBALL_KEY": "unit-test-placeholder"}), \
                patch.object(generator, "build_opener") as opener:
            remote = opener.return_value.open.return_value.__enter__.return_value
            remote.status, remote.read.return_value = 200, b"not-json"
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_request_uses_date_timezone_header_and_timeout(self):
        with patch.dict(generator.os.environ, {"API_FOOTBALL_KEY": "unit-test-placeholder"}), \
                patch.object(generator, "build_opener") as opener:
            remote = opener.return_value.open.return_value.__enter__.return_value
            remote.status, remote.read.return_value = 200, json.dumps(response()).encode()
            generator.generate(DAY, self.output, now=NOW)
            args, kwargs = opener.return_value.open.call_args
            request = args[0]
            self.assertEqual({"date": ["2026-10-06"], "timezone": [generator.TIMEZONE]},
                             parse_qs(urlparse(request.full_url).query))
            self.assertEqual("unit-test-placeholder", request.get_header("X-apisports-key"))
            self.assertEqual(30, kwargs["timeout"])
        self.assertTrue(all(not section["events"] for section in json.loads(self.output.read_bytes())["sections"]))

    def test_no_redirect_forwards_key(self):
        self.assertIsNone(generator.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_network_failure_keeps_output(self):
        with patch.dict(generator.os.environ, {"API_FOOTBALL_KEY": "unit-test-placeholder"}), \
                patch.object(generator, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError("network problem")
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_incomplete_http_body_keeps_output(self):
        with patch.dict(generator.os.environ, {"API_FOOTBALL_KEY": "unit-test-placeholder"}), \
                patch.object(generator, "build_opener") as opener:
            remote = opener.return_value.open.return_value.__enter__.return_value
            remote.status = 200
            remote.read.side_effect = IncompleteRead(b"partial")
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_malformed_league_does_not_clear_schedule(self):
        malformed = fixture()
        malformed["league"] = {}
        self.write_input(response(malformed))
        with self.assertRaises(generator.GenerationError):
            generator.generate(DAY, self.output, self.input, NOW)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_invalid_temporary_json_cannot_replace_original(self):
        generated = feed()
        with patch.object(generator, "validate_feed", side_effect=[None, generator.GenerationError("Invalid temp")]):
            with self.assertRaises(generator.GenerationError):
                generator.write_feed_atomic(generated, self.output)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())
        self.assertFalse(self.output.with_name("sports_today.json.tmp").exists())

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(self):
        with patch.object(generator.os, "replace", side_effect=OSError("read only")):
            with self.assertRaises(generator.GenerationError):
                generator.write_feed_atomic(feed(), self.output)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())
        self.assertFalse(self.output.with_name("sports_today.json.tmp").exists())

    def test_identical_content_keeps_timestamp_and_exact_bytes(self):
        first = feed(fixture())
        self.assertTrue(generator.write_feed_atomic(first, self.output))
        original_bytes = self.output.read_bytes()
        later = feed(fixture(), now=NOW + timedelta(hours=4))
        self.assertFalse(generator.write_feed_atomic(later, self.output))
        self.assertEqual(first["updatedAt"], later["updatedAt"])
        self.assertEqual(original_bytes, self.output.read_bytes())

    def test_status_change_updates_feed_and_timestamp(self):
        generator.write_feed_atomic(feed(fixture()), self.output)
        updated = feed(fixture(status="1H"), now=NOW + timedelta(hours=4))
        self.assertTrue(generator.write_feed_atomic(updated, self.output))
        self.assertEqual("2026-10-06T16:00:00-03:00", updated["updatedAt"])

    def test_invalid_generated_feed_is_rejected_before_write(self):
        invalid = feed(fixture())
        invalid["sections"][0]["events"][0]["startTime"] = "29:99"
        with self.assertRaises(generator.GenerationError):
            generator.write_feed_atomic(invalid, self.output)
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())

    def test_cli_summary_is_safe_and_offline_default_is_preview(self):
        self.write_input(response(fixture()))
        summary = io.StringIO()
        with patch.object(generator, "ROOT", self.root), contextlib.redirect_stdout(summary):
            result = generator.main(["--input", str(self.input), "--date", "2026-10-06"])
        self.assertEqual(0, result)
        self.assertIn("Argentina selected: 1", summary.getvalue())
        self.assertIn("Ignored fixtures: 0", summary.getvalue())
        self.assertTrue((self.root / "sports_today.preview.json").exists())
        self.assertEqual(b"ORIGINAL FEED", self.output.read_bytes())


if __name__ == "__main__":
    unittest.main()
