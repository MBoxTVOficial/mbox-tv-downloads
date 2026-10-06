"""Synthetic fixtures only: no real API/network, output always in temporary directories."""
import ast
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import generate_sports_today as generator
from test_generate_sports_today import DAY, NOW, fixture, response, feed


def scored(home=None, away=None, status="NS"):
    item = fixture(status=status)
    item["goals"] = {"home": home, "away": away}
    return item


class RealtimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "sports_today.json"

    def run_api(self, item, now=NOW):
        with patch.object(generator, "fetch_fixtures", return_value=response(item)) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(DAY, self.output, now=now)
        fetch.assert_called_once_with(DAY)
        return result

    def event(self):
        return json.loads(self.output.read_bytes())["sections"][0]["events"][0]

    def test_real_response_transition_scheduled_live_and_finished(self):
        stages = [("NS", None, None), ("1H", 0, 0), ("2H", 1, 0),
                  ("2H", 1, 1), ("2H", 2, 1), ("FT", 2, 1)]
        for index, (status, home, away) in enumerate(stages):
            with self.subTest(status=status, home=home, away=away):
                result = self.run_api(scored(home, away, status), NOW + timedelta(minutes=10 * index))
                self.assertTrue(result[-1])
                event = self.event()
                if home is None:
                    self.assertNotIn("homeScore", event)
                    self.assertNotIn("awayScore", event)
                else:
                    self.assertEqual((home, away), (event["homeScore"], event["awayScore"]))
                self.assertEqual(generator.normalize_status(status), event["status"])

    def test_all_live_statuses_use_received_goals_without_fallback(self):
        for status in ("1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"):
            with self.subTest(status=status):
                self.run_api(scored(7, 4, status))
                self.assertEqual(("LIVE", 7, 4), (self.event()["status"], self.event()["homeScore"], self.event()["awayScore"]))

    def test_all_finished_statuses_use_received_final_goals(self):
        for status in ("FT", "AET", "PEN"):
            with self.subTest(status=status):
                self.run_api(scored(3, 2, status))
                self.assertEqual(("FINISHED", 3, 2), (self.event()["status"], self.event()["homeScore"], self.event()["awayScore"]))

    def test_null_goals_never_invent_zero_for_live_or_scheduled(self):
        for status in ("NS", "TBD", "1H", "FT"):
            self.run_api(scored(status=status))
            self.assertNotIn("homeScore", self.event())
            self.assertNotIn("awayScore", self.event())

    def test_identical_score_has_no_logical_commit_or_timestamp_change(self):
        first = self.run_api(scored(1, 0, "1H"))[0]
        original = self.output.read_bytes()
        result = self.run_api(scored(1, 0, "1H"), NOW + timedelta(minutes=10))
        self.assertFalse(result[-1])
        self.assertEqual(first["updatedAt"], result[0]["updatedAt"])
        self.assertEqual(original, self.output.read_bytes())

    def test_score_only_and_status_only_changes_are_written(self):
        self.run_api(scored(1, 0, "1H"))
        self.assertTrue(self.run_api(scored(1, 1, "1H"), NOW + timedelta(minutes=10))[-1])
        self.assertTrue(self.run_api(scored(1, 1, "FT"), NOW + timedelta(minutes=20))[-1])

    def test_api_errors_preserve_previous_feed(self):
        self.run_api(scored(1, 0, "1H"))
        original = self.output.read_bytes()
        with patch.object(generator, "fetch_fixtures", return_value={**response(), "errors": {"quota": "private"}}):
            with self.assertRaises(generator.GenerationError):
                generator.generate(DAY, self.output)
        self.assertEqual(original, self.output.read_bytes())

    def test_empty_same_day_response_does_not_erase_existing_agenda(self):
        self.run_api(scored(1, 0, "1H"))
        original = self.output.read_bytes()
        with patch.object(generator, "fetch_fixtures", return_value=response()):
            with self.assertRaisesRegex(generator.GenerationError, "Agenda vacía"):
                generator.generate(DAY, self.output)
        self.assertEqual(original, self.output.read_bytes())

    def test_first_day_or_new_day_empty_agenda_can_be_written(self):
        with patch.object(generator, "fetch_fixtures", return_value=response()), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(generator.generate(DAY, self.output, now=NOW)[-1])
        generator.write_feed_atomic(feed(scored(1, 0, "FT")), self.output)
        with patch.object(generator, "fetch_fixtures", return_value=response()), contextlib.redirect_stdout(io.StringIO()):
            result = generator.generate(DAY + timedelta(days=1), self.output, now=NOW + timedelta(days=1))
        self.assertTrue(result[-1])

    def test_no_hardcoded_production_scores_or_examples_fallback(self):
        source = Path(generator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.keyword)
                       and node.arg in ("homeScore", "awayScore")]
        self.assertEqual({"homeScore", "awayScore"}, {node.arg for node in assignments})
        for node in assignments:
            self.assertIsInstance(node.value, ast.Subscript)
            self.assertEqual("goals", node.value.value.id)
            self.assertEqual("home" if node.arg == "homeScore" else "away", node.value.slice.value)
        fetch_source = ast.get_source_segment(source, next(node for node in tree.body
                             if isinstance(node, ast.FunctionDef) and node.name == "fetch_fixtures"))
        self.assertNotIn("examples", fetch_source)
        self.assertNotIn("fixtures_sample", source)

    def test_secure_change_logs_have_only_fixture_scores_and_status(self):
        previous = feed(scored(1, 0, "1H"))
        incoming = feed(scored(1, 1, "FT"))
        logs = io.StringIO()
        with contextlib.redirect_stdout(logs):
            generator.log_feed_changes(previous, incoming)
        self.assertIn("SCORE_CHANGED fixture=100 old=1-0 new=1-1", logs.getvalue())
        self.assertIn("STATUS_CHANGED fixture=100 old=LIVE new=FINISHED", logs.getvalue())
        self.assertNotIn("https", logs.getvalue())


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(generator.os.environ, {"GITHUB_EVENT_NAME": "schedule"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_live_keeps_refreshing_until_finished(self):
        self.assertTrue(generator.needs_live_refresh(feed(scored(1, 0, "2H")), DAY, NOW))
        self.assertFalse(generator.needs_live_refresh(feed(scored(1, 0, "FT")), DAY, NOW))

    def test_scheduled_near_kickoff_and_four_hour_cutoff(self):
        scheduled = feed(scored())  # 20:00 ART
        for hour, minute, expected in ((19, 49, False), (19, 50, True), (20, 10, True), (23, 59, True)):
            clock = NOW.replace(hour=hour, minute=minute)
            self.assertEqual(expected, generator.needs_live_refresh(scheduled, DAY, clock))
        self.assertFalse(generator.needs_live_refresh(scheduled, DAY, NOW + timedelta(hours=13)))

    def test_no_events_finished_cancelled_postponed_and_unknown_time_skip(self):
        for previous in (feed(), feed(scored(status="FT")), feed(scored(status="CANC")),
                         feed(scored(status="PST")), feed(scored(status="TBD"))):
            self.assertFalse(generator.needs_live_refresh(previous, DAY, NOW))

    def test_missing_or_previous_day_feed_gets_real_seed(self):
        self.assertTrue(generator.needs_live_refresh(None, DAY, NOW))
        self.assertTrue(generator.needs_live_refresh(feed(), DAY + timedelta(days=1), NOW + timedelta(days=1)))

    def test_inactive_live_skip_never_calls_api_or_changes_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sports_today.json"
            generator.write_feed_atomic(feed(scored(2, 1, "FT")), output)
            original = output.read_bytes()
            with patch.object(generator, "fetch_fixtures") as fetch:
                with self.assertRaises(generator.RefreshSkipped):
                    generator.generate(DAY, output, now=NOW, live_only=True)
            fetch.assert_not_called()
            self.assertEqual(original, output.read_bytes())

    def test_manual_live_dispatch_can_query_real_api_without_active_events(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sports_today.json"
            generator.write_feed_atomic(feed(scored(2, 1, "FT")), output)
            with patch.dict(generator.os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                    patch.object(generator, "fetch_fixtures", return_value=response(scored(2, 1, "FT"))) as fetch, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = generator.generate(DAY, output, now=NOW, live_only=True)
            fetch.assert_called_once_with(DAY)
            self.assertFalse(result[-1])


class BudgetTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(generator.os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MBoxTVOficial/mbox-tv-downloads",
            "GITHUB_REF": "refs/heads/main", "GITHUB_TOKEN": "test-github-placeholder",
            "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
            "API_FOOTBALL_KEY": "test-football-placeholder",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.utc = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)

    def history(self, count, offset=0):
        return {"total_count": count, "workflow_runs": [
            {"id": offset + index + 1, "run_attempt": 1, "created_at": "2026-10-06T14:00:00Z",
             "event": "workflow_dispatch", "head_branch": "main"} for index in range(count)]}

    def mock_history(self, opener, *payloads):
        remote = opener.return_value.open.return_value.__enter__.return_value
        remote.status = 200
        remote.read.side_effect = [json.dumps(payload).encode() for payload in payloads]

    def test_shared_general_live_manual_limit_allows_ninetieth_slot(self):
        with patch.object(generator, "build_opener") as opener, contextlib.redirect_stdout(io.StringIO()):
            self.mock_history(opener, self.history(4), self.history(86, 4))
            generator.check_github_budget(self.utc)
            self.assertEqual(2, opener.return_value.open.call_count)
            urls = [call.args[0].full_url for call in opener.return_value.open.call_args_list]
            self.assertTrue(all("api.github.com" in url and "created=2026-10-06" in url for url in urls))

    def test_more_than_ninety_runs_skip_before_football(self):
        with patch.object(generator, "build_opener") as opener:
            self.mock_history(opener, self.history(4), self.history(87, 4))
            with self.assertRaisesRegex(generator.RefreshSkipped, "daily_budget"):
                generator.check_github_budget(self.utc)

    def test_production_fetch_at_limit_uses_one_football_request_for_all_fixtures(self):
        with patch.object(generator, "build_opener") as opener, \
                patch.object(generator, "datetime", wraps=datetime) as clock, \
                contextlib.redirect_stdout(io.StringIO()):
            clock.now.return_value = self.utc
            self.mock_history(opener, self.history(4), self.history(86, 4), response(scored(7, 4, "2H")))
            payload = generator.fetch_fixtures(DAY)
            self.assertEqual(7, payload["response"][0]["goals"]["home"])
            requests = [call.args[0] for call in opener.return_value.open.call_args_list]
            self.assertEqual(3, len(requests))  # Two GitHub reads, exactly one API-Football GET.
            self.assertTrue(all("api.github.com" in request.full_url for request in requests[:2]))
            self.assertTrue(requests[2].full_url.startswith(generator.ENDPOINT + "?"))
            self.assertEqual("test-football-placeholder", requests[2].get_header("X-apisports-key"))
            self.assertIsNone(requests[2].get_header("Authorization"))

    def test_production_exhausted_budget_preserves_feed_and_makes_no_football_request(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sports_today.json"
            generator.write_feed_atomic(feed(scored(1, 0, "1H")), output)
            original = output.read_bytes()
            with patch.object(generator, "build_opener") as opener, \
                    patch.object(generator, "datetime", wraps=datetime) as clock:
                clock.now.return_value = self.utc
                self.mock_history(opener, self.history(4), self.history(87, 4))
                with self.assertRaises(generator.RefreshSkipped):
                    generator.generate(DAY, output, now=NOW)
                self.assertEqual(2, opener.return_value.open.call_count)
                self.assertTrue(all("api.github.com" in call.args[0].full_url
                                    for call in opener.return_value.open.call_args_list))
            self.assertEqual(original, output.read_bytes())

    def test_missing_token_cannot_silently_reset_counter_or_query_football(self):
        with patch.dict(generator.os.environ, {"GITHUB_TOKEN": ""}), patch.object(generator, "build_opener") as opener:
            with self.assertRaises(generator.GenerationError):
                generator.fetch_fixtures(DAY)
            opener.assert_not_called()

    def test_historical_rerun_attempts_also_consume_slots(self):
        old = self.history(4)
        old["workflow_runs"][2]["run_attempt"] = 2
        with patch.object(generator, "build_opener") as opener:
            self.mock_history(opener, old, self.history(86, 4))
            with self.assertRaises(generator.RefreshSkipped):
                generator.check_github_budget(self.utc)

    def test_rerun_is_denied_without_network(self):
        with patch.dict(generator.os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), patch.object(generator, "build_opener") as opener:
            with self.assertRaises(generator.RefreshSkipped):
                generator.fetch_fixtures(DAY)
            opener.assert_not_called()

    def test_missing_current_run_or_incomplete_history_fail_closed(self):
        for payload in (self.history(0), {**self.history(1), "total_count": 2}):
            with patch.object(generator, "build_opener") as opener:
                self.mock_history(opener, payload, self.history(0))
                with self.assertRaises(generator.GenerationError):
                    generator.check_github_budget(self.utc)

    def test_old_day_run_cannot_spend_new_day_quota(self):
        old = self.history(1)
        old["workflow_runs"][0]["created_at"] = "2026-10-05T23:59:00Z"
        with patch.object(generator, "build_opener") as opener:
            self.mock_history(opener, old, self.history(0))
            with self.assertRaises(generator.GenerationError):
                generator.check_github_budget(self.utc)

    def test_github_http_error_has_no_football_request_or_token_in_logs(self):
        with patch.object(generator, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://api.github.com", 403, "", {}, io.BytesIO())
            with self.assertRaises(generator.GenerationError) as caught:
                generator.fetch_fixtures(DAY)
            self.assertNotIn("placeholder", str(caught.exception))
            self.assertEqual(1, opener.return_value.open.call_count)
            self.assertIn("api.github.com", opener.return_value.open.call_args.args[0].full_url)

    def test_off_github_does_not_use_github_network_or_token(self):
        with patch.dict(generator.os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(generator, "build_opener") as opener:
            generator.check_github_budget(self.utc)
            opener.assert_not_called()


class WorkflowTest(unittest.TestCase):
    def test_general_and_live_share_serialization_and_real_generator(self):
        workflows = generator.ROOT / ".github/workflows"
        for name in generator.SPORTS_WORKFLOWS:
            text = (workflows / name).read_text(encoding="utf-8")
            self.assertIn("group: sports-today-main", text)
            self.assertIn("cancel-in-progress: false", text)
            self.assertIn("secrets.API_FOOTBALL_KEY", text)
            self.assertIn("GITHUB_TOKEN: ${{ github.token }}", text)
            self.assertIn("actions: read", text)
            self.assertIn("git add -- sports_today.json", text)
            self.assertIn("--only -- sports_today.json", text)
            self.assertNotIn("--input", text)
            self.assertNotIn("examples/", text)
        live = (workflows / "update-sports-live.yml").read_text(encoding="utf-8")
        self.assertIn("generate_sports_today.py --live", live)
        self.assertIn('git commit -m "Update live sports scores"', live)

    def test_crons_have_sixty_live_slots_plus_four_unchanged_general_slots(self):
        live = (generator.ROOT / ".github/workflows/update-sports-live.yml").read_text(encoding="utf-8")
        self.assertIn("7,17,27,37,47,57 18-23 * * *", live)
        self.assertIn("7,17,27,37,47,57 0-3 * * *", live)
        self.assertEqual(60, len(range(18, 24)) * 6 + len(range(0, 4)) * 6)
        general = (generator.ROOT / ".github/workflows/update-sports-today.yml").read_text(encoding="utf-8")
        for hour in (11, 15, 20, 23):
            self.assertIn(f"cron: '0 {hour} * * *'", general)


if __name__ == "__main__":
    unittest.main()
