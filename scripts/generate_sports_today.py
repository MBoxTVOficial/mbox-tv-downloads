#!/usr/bin/env python3
"""Generate MBox's daily football feed using only Python's standard library."""

import argparse
import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__:
    from .sports_channel_rules import ChannelRulesError, validate_references
    from .sports_daily_broadcasts import (BroadcastError, load_daily_context,
                                        resolve_broadcasts, apply_daily_assignments, write_broadcasts_atomic)
    from .broadcaster_discovery import DiscoveryError, discover_from_root, atomic_json
    from .sports_playback_policy import apply_playback_policy
else:
    from sports_channel_rules import ChannelRulesError, validate_references
    from sports_daily_broadcasts import (BroadcastError, load_daily_context,
                                       resolve_broadcasts, apply_daily_assignments, write_broadcasts_atomic)
    from broadcaster_discovery import DiscoveryError, discover_from_root, atomic_json
    from sports_playback_policy import apply_playback_policy

TIMEZONE = "America/Argentina/Buenos_Aires"
ENDPOINT = "https://v3.football.api-sports.io/fixtures"
ROOT = Path(__file__).resolve().parents[1]
REAL_OUTPUT = ROOT / "sports_today.json"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DAILY_API_CALLS = 90
SPORTS_WORKFLOWS = ("update-sports-today.yml", "update-sports-live.yml")


class GenerationError(Exception):
    """A safe, user-facing error: never contains API headers or remote error text."""


class RefreshSkipped(GenerationError):
    """A deliberate skip before any API-Football request, preserving the feed."""


def realtime_log(event, **fields):
    # Only bounded identifiers/counts/statuses, never remote bodies, URLs or credentials.
    values = " ".join(f"{key}={re.sub(r'[^A-Za-z0-9_.:+-]', '_', str(value))[:80]}"
                      for key, value in fields.items())
    print(f"SPORTS_REALTIME {event}" + (f" {values}" if values else ""))


def check_github_budget(now=None):
    """GitHub run history is a persistent, conservative upper bound, including manual runs.

    Each first attempt can issue at most one request. Re-runs and runs created on
    another UTC day cannot issue requests. Failed/skipped runs also consume slots.
    No ephemeral runner counter or mutable quota file is needed.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    real_clock = now is None
    now = now or datetime.now(timezone.utc)
    utc_day = now.astimezone(timezone.utc).date()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or \
            not token or not run_id.isdigit() or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise GenerationError("No se puede verificar el presupuesto GitHub; no se consulta API-Football.")
    if os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
        raise RefreshSkipped("rerun_requires_new_workflow_dispatch")
    total = 0
    current = None
    for workflow in SPORTS_WORKFLOWS:
        request = Request(
            f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?"
            + urlencode({"created": utc_day.isoformat(), "per_page": 100}),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2026-03-10"},
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=20) as response:
                if response.status != 200:
                    raise GenerationError("Historial GitHub no disponible; no se consulta API-Football.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GenerationError("Historial GitHub demasiado grande; no se consulta API-Football.")
            payload = decode_json(raw)
        except HTTPError as error:
            error.close()
            raise GenerationError("Historial GitHub no disponible; no se consulta API-Football.") from None
        except (URLError, TimeoutError, OSError, ValueError, HTTPException):
            raise GenerationError("Historial GitHub no disponible; no se consulta API-Football.") from None
        if not isinstance(payload, dict) or type(payload.get("total_count")) is not int or \
                payload["total_count"] < 0 or not isinstance(payload.get("workflow_runs"), list):
            raise GenerationError("Historial GitHub inválido; no se consulta API-Football.")
        total += payload["total_count"]
        if total > MAX_DAILY_API_CALLS:
            raise RefreshSkipped("daily_budget_exhausted")
        runs = payload["workflow_runs"]
        # <=90 results fit on one page. An incomplete/eventually consistent response fails closed.
        if len(runs) != payload["total_count"] or any(not isinstance(run, dict) for run in runs):
            raise GenerationError("Historial GitHub incompleto; no se consulta API-Football.")
        for run in runs:
            attempt = run.get("run_attempt")
            if type(attempt) is not int or attempt < 1:
                raise GenerationError("Intentos GitHub no verificados; no se consulta API-Football.")
            total += attempt - 1  # Also reserve slots for historical re-runs, conservatively.
            if total > MAX_DAILY_API_CALLS:
                raise RefreshSkipped("daily_budget_exhausted")
            if str(run.get("id")) == run_id:
                current = run
    if current is None or current.get("run_attempt") != 1 or \
            current.get("event") not in ("schedule", "workflow_dispatch") or \
            current.get("head_branch") != "main":
        raise GenerationError("Ejecución actual no verificada; no se consulta API-Football.")
    try:
        created = datetime.fromisoformat(current["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or created.astimezone(timezone.utc).date() != utc_day:
            raise ValueError("Wrong day")
    except (KeyError, AttributeError, TypeError, ValueError):
        raise GenerationError("Fecha de ejecución no verificada; no se consulta API-Football.") from None
    # A midnight rollover during history checks must not use yesterday's quota decision.
    if real_clock and datetime.now(timezone.utc).date() != utc_day:
        raise RefreshSkipped("utc_day_changed")
    realtime_log("API_BUDGET", utcDate=utc_day, reservedRuns=total, max=MAX_DAILY_API_CALLS)


@dataclass(frozen=True)
class LeagueRule:
    names: tuple[str, ...]
    countries: tuple[str, ...] = ()
    ids: tuple[int, ...] = ()


# Only IDs verified in API-Football's official beginner guide are seeded here:
# 2 = UEFA Champions League, 39 = Premier League, 140 = La Liga.
# https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide
# Add other IDs after verifying them in /leagues or the API-Sports dashboard.
# Exact normalized aliases + country prevent similarly named minor leagues from entering.
ARGENTINA_LEAGUES = (
    LeagueRule(("Liga Profesional Argentina", "Liga Profesional", "Liga Profesional de Fútbol",
                "Primera Division"), ("Argentina",)),
    LeagueRule(("Primera Nacional",), ("Argentina",)),
    LeagueRule(("Copa Argentina",), ("Argentina",)),
    LeagueRule(("Supercopa Argentina", "Super Copa"), ("Argentina",)),
    LeagueRule(("Copa de la Liga Profesional", "Trofeo de Campeones"), ("Argentina",)),
)
SOUTH_AMERICAN_QUALIFIERS = LeagueRule((
    "World Cup - Qualification South America",
    "World Cup Qualification South America",
    "CONMEBOL World Cup Qualifiers",
    "World Cup Qualification CONMEBOL",
    "South America World Cup Qualifiers",
))  # No ID seeded until verified via /leagues or the official dashboard.
UEFA_QUALIFIERS = LeagueRule((
    "World Cup - Qualification Europe", "World Cup Qualification UEFA",
    "UEFA World Cup Qualifiers", "Euro Championship - Qualification",
    "UEFA Euro Qualifiers", "UEFA European Championship Qualification",
))
UEFA_NATIONS = LeagueRule(("UEFA Nations League",))
CONMEBOL_LEAGUES = (
    LeagueRule(("Copa Libertadores", "CONMEBOL Libertadores")),
    LeagueRule(("Copa Sudamericana", "CONMEBOL Sudamericana")),
    LeagueRule(("Recopa Sudamericana", "CONMEBOL Recopa")),
)
CHAMPIONS_LEAGUES = (
    LeagueRule(("UEFA Champions League",), ids=(2,)),
)
SPAIN_LEAGUES = (
    LeagueRule(("La Liga", "Primera Division"), ("Spain",), (140,)),
)
FRANCE_LEAGUES = (
    LeagueRule(("Ligue 1",), ("France",)),
)
ENGLAND_LEAGUES = (
    LeagueRule(("Premier League",), ("England",), (39,)),
)
ITALY_LEAGUES = (
    LeagueRule(("Serie A",), ("Italy",)),
)
INTERNATIONAL_LEAGUES = (
    LeagueRule(("Bundesliga",), ("Germany",)),
    LeagueRule(("Major League Soccer", "MLS"), ("USA", "United States")),
    LeagueRule(("UEFA Europa League", "UEFA Conference League", "UEFA Europa Conference League")),
    LeagueRule(("World Cup", "FIFA World Cup", "Copa America", "Euro Championship", "UEFA Euro")),
    LeagueRule(("World Cup - Qualification CONCACAF", "World Cup - Qualification Africa",
                "World Cup - Qualification Asia", "World Cup - Qualification Oceania")),
)

SOUTH_AMERICAN_NATIONAL_TEAMS = frozenset((
    "argentina", "bolivia", "brazil", "brasil", "chile", "colombia",
    "ecuador", "paraguay", "peru", "uruguay", "venezuela",
))  # Full normalized names only; Peru/Perú normalize to the same entry.

SECTIONS = (
    ("south_america_qualifiers", "Eliminatorias Sudamericanas", 1, (SOUTH_AMERICAN_QUALIFIERS,)),
    ("south_america_national_teams", "Selecciones Sudamericanas", 2, ()),
    ("uefa_qualifiers", "Eliminatorias UEFA", 3, (UEFA_QUALIFIERS,)),
    ("uefa_nations", "UEFA Nations League", 4, (UEFA_NATIONS,)),
    ("argentina", "Fútbol - Argentina", 5, ARGENTINA_LEAGUES),
    ("conmebol", "Copas CONMEBOL", 6, CONMEBOL_LEAGUES),
    ("champions", "Champions League", 7, CHAMPIONS_LEAGUES),
    ("spain", "Liga de España", 8, SPAIN_LEAGUES),
    ("england", "Premier League", 9, ENGLAND_LEAGUES),
    ("france", "Liga de Francia", 10, FRANCE_LEAGUES),
    ("italy", "Serie A", 11, ITALY_LEAGUES),
    ("international", "Fútbol - Internacional", 12, INTERNATIONAL_LEAGUES),
)
# Read old schemaVersion 1 caches without resetting the existing LIVE eligibility/empty-feed guard.
LEGACY_SECTIONS = (
    ("argentina", "Fútbol - Argentina", 1),
    ("conmebol", "Copas CONMEBOL", 2),
    ("champions", "Champions League", 3),
    ("international", "Fútbol - Internacional", 4),
)
PREVIOUS_COMPETITION_SECTIONS = (
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
)
STATUS_MAP = {
    "NS": "SCHEDULED", "TBD": "SCHEDULED", "PST": "POSTPONED",
    "CANC": "CANCELLED", "ABD": "CANCELLED",
    **dict.fromkeys(("1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"), "LIVE"),
    **dict.fromkeys(("FT", "AET", "PEN"), "FINISHED"),
}


def argentina_timezone():
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        # Windows may have no IANA database. Argentina currently uses UTC-3 year round.
        return timezone(timedelta(hours=-3), TIMEZONE)


def normalize(value):
    text = unicodedata.normalize("NFKD", value if isinstance(value, str) else "")
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def matching_league_rule(league):
    league_id = league.get("id")
    # Identity takes precedence over names, including translated names.
    if type(league_id) is int:
        for section_id, _, _, rules in SECTIONS:
            for rule in rules:
                if league_id in rule.ids:
                    return section_id, rule
    name, country = normalize(league.get("name")), normalize(league.get("country"))
    for section_id, _, _, rules in SECTIONS:
        for rule in rules:
            if name in map(normalize, rule.names) and (
                not rule.countries or country in map(normalize, rule.countries)
            ):
                return section_id, rule
    # Unconfigured competitions remain visible in the residual section, never a specific league.
    return "international", None


def classify_league(league):
    return matching_league_rule(league)[0]


def classify_fixture(item):
    section_id = classify_league(item["league"])
    if section_id != "international":
        return section_id  # Specific competitions always win, including real qualifiers.
    teams = item.get("teams")
    if isinstance(teams, dict):
        for side in ("home", "away"):
            team = teams.get(side)
            if isinstance(team, dict) and normalize(team.get("name")) in SOUTH_AMERICAN_NATIONAL_TEAMS:
                return "south_america_national_teams"
    return section_id


def event_sort_key(event):
    # Unknown/special statuses share the postponed group. Python's sort keeps ties stable.
    status = event["status"]
    priority = {"LIVE": 0, "SCHEDULED": 1, "FINISHED": 3, "CANCELLED": 4}.get(status, 2)
    time = event["startTime"]
    if not time or time == "TBD":
        return priority, True, 0
    hour, minute = map(int, time.split(":"))
    minutes = hour * 60 + minute
    return priority, False, -minutes if status == "FINISHED" else minutes


def normalize_status(value):
    status = value.strip().upper() if isinstance(value, str) else ""
    return STATUS_MAP.get(status, status or "UNKNOWN")


def reject_constant(_):
    raise GenerationError("JSON inválido: contiene valores numéricos no permitidos.")


def decode_json(raw):
    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise GenerationError("JSON remoto/de entrada inválido; se conserva la salida anterior.") from error


def validate_api_response(payload):
    if not isinstance(payload, dict):
        raise GenerationError("Respuesta API inválida: se esperaba un objeto.")
    if payload.get("errors") not in ([], {}):
        raise GenerationError("API-Football devolvió errors; se conserva la salida anterior.")
    fixtures, results = payload.get("response"), payload.get("results")
    if not isinstance(fixtures, list) or type(results) is not int or results != len(fixtures):
        raise GenerationError("Respuesta API inválida: results/response inconsistentes.")
    paging = payload.get("paging")
    if paging is not None and (
        not isinstance(paging, dict) or paging.get("current") != 1 or paging.get("total") != 1
    ):
        raise GenerationError("Respuesta API incompleta: paginación inesperada; no se reemplaza el feed.")
    return fixtures


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the authentication header to a redirected endpoint.
        return None


def fetch_fixtures(day):
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise GenerationError("Falta la variable de entorno API_FOOTBALL_KEY.")
    check_github_budget()
    realtime_log("REALTIME_REQUEST", date=day.isoformat())
    request = Request(
        ENDPOINT + "?" + urlencode({"date": day.isoformat(), "timezone": TIMEZONE}),
        headers={"x-apisports-key": key, "Accept": "application/json"},
    )
    try:
        with build_opener(NoRedirects()).open(request, timeout=30) as response:
            if response.status != 200:
                raise GenerationError(f"API-Football devolvió HTTP {response.status}; no se reemplaza el feed.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise GenerationError(f"API-Football devolvió HTTP {status}; no se reemplaza el feed.") from None
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        raise GenerationError("No se pudo consultar API-Football; se conserva la salida anterior.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GenerationError("Respuesta API demasiado grande; no se reemplaza el feed.")
    return decode_json(raw)


def required_text(value, field):
    text = value.get(field)
    if not isinstance(text, str) or not text.strip():
        raise GenerationError("Fixture relevante inválido: faltan nombres de competencia/equipos.")
    return text.strip()


def kickoff_time(fixture, raw_status, day):
    raw_date = fixture.get("date")
    if raw_date is None:
        return "", True
    try:
        instant = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("Missing offset")
        local = instant.astimezone(argentina_timezone())
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise GenerationError("Fixture relevante inválido: fecha/hora no válida.") from None
    return ("" if raw_status == "TBD" else local.strftime("%H:%M")), local.date() == day


def build_feed(payload, day, now=None):
    fixtures = validate_api_response(payload)
    sections = [{"id": sid, "title": title, "priority": priority, "events": []}
                for sid, title, priority, _ in SECTIONS]
    buckets = {section["id"]: section["events"] for section in sections}
    ids = set()
    for item in fixtures:
        if not isinstance(item, dict) or not isinstance(item.get("league"), dict):
            raise GenerationError("Respuesta API inválida: fixture/league malformado.")
        required_text(item["league"], "name")
        section_id = classify_fixture(item)
        if section_id is None:
            continue
        fixture, teams = item.get("fixture"), item.get("teams")
        if not isinstance(fixture, dict) or not isinstance(teams, dict):
            raise GenerationError("Fixture relevante inválido: faltan fixture/teams.")
        fixture_id = fixture.get("id")
        if type(fixture_id) is not int or fixture_id <= 0 or fixture_id in ids:
            raise GenerationError("Fixture relevante inválido: ID ausente, inválido o duplicado.")
        raw_status = fixture.get("status", {})
        raw_status = raw_status.get("short") if isinstance(raw_status, dict) else None
        raw_status = raw_status.strip().upper() if isinstance(raw_status, str) else ""
        time, same_day = kickoff_time(fixture, raw_status, day)
        if not same_day:
            continue
        home, away = teams.get("home"), teams.get("away")
        if not isinstance(home, dict) or not isinstance(away, dict):
            raise GenerationError("Fixture relevante inválido: faltan equipos.")
        event = {
            "id": f"fixture-{fixture_id}", "sport": "football",
            "competition": required_text(item["league"], "name"),
            "homeTeam": required_text(home, "name"), "awayTeam": required_text(away, "name"),
            "homeLogo": home.get("logo") if isinstance(home.get("logo"), str) else "",
            "awayLogo": away.get("logo") if isinstance(away.get("logo"), str) else "",
            "startTime": time, "status": normalize_status(raw_status),
        }
        # Optional scores: a malformed/incomplete pair must not discard a valid fixture.
        # Use goals only, never halftime/fulltime/extra-time/penalty breakdowns.
        goals = item.get("goals")
        if isinstance(goals, dict) and all(
            type(goals.get(side)) is int and goals[side] >= 0 for side in ("home", "away")
        ):
            event.update(homeScore=goals["home"], awayScore=goals["away"])
        buckets[section_id].append(event)
        ids.add(fixture_id)
        if section_id in ("south_america_qualifiers", "uefa_qualifiers"):
            decision = "QUALIFIERS_SOUTH_AMERICA" if section_id == "south_america_qualifiers" else "QUALIFIERS_UEFA"
            print(f"SPORTS_CLASSIFY {decision} fixtureId={fixture_id}")
    for section in sections:
        section["events"].sort(key=event_sort_key)
        events = section["events"]
        print(f"SPORTS_SORT section={section['id']} "
              f"live={sum(event['status'] == 'LIVE' for event in events)} "
              f"scheduled={sum(event['status'] == 'SCHEDULED' for event in events)} "
              f"finished={sum(event['status'] == 'FINISHED' for event in events)}")
    now = now or datetime.now(argentina_timezone())
    feed = {"schemaVersion": 1, "date": day.isoformat(), "timezone": TIMEZONE,
            "updatedAt": now.astimezone(argentina_timezone()).isoformat(timespec="seconds"),
            "demo": False, "sections": sections}
    validate_feed(feed)
    return feed, len(fixtures) - sum(len(section["events"]) for section in sections)


def validate_feed(feed):
    if not isinstance(feed, dict) or set(feed) != {
        "schemaVersion", "date", "timezone", "updatedAt", "demo", "sections"
    }:
        raise GenerationError("Feed generado inválido: estructura base.")
    if type(feed["schemaVersion"]) is not int or feed["schemaVersion"] != 1 or \
            feed["timezone"] != TIMEZONE or feed["demo"] is not False:
        raise GenerationError("Feed generado inválido: metadata.")
    try:
        date.fromisoformat(feed["date"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-03:00", feed["updatedAt"]):
            raise ValueError("Invalid updatedAt")
        datetime.fromisoformat(feed["updatedAt"])
    except (TypeError, ValueError):
        raise GenerationError("Feed generado inválido: date/updatedAt.") from None
    sections = feed["sections"]
    # Generation always emits the current sections; reading a legacy cache keeps realtime intact.
    expected_sections = tuple((sid, title, priority) for sid, title, priority, _ in SECTIONS)
    if isinstance(sections, list):
        for legacy_sections in (LEGACY_SECTIONS, PREVIOUS_COMPETITION_SECTIONS):
            if len(sections) == len(legacy_sections):
                expected_sections = legacy_sections
                break
    if not isinstance(sections, list) or len(sections) != len(expected_sections):
        raise GenerationError("Feed generado inválido: secciones.")
    event_ids = set()
    event_fields = {
        "id", "sport", "competition", "homeTeam", "awayTeam", "homeLogo", "awayLogo", "startTime", "status"
    }
    score_fields = {"homeScore", "awayScore"}
    for section, (sid, title, priority) in zip(sections, expected_sections):
        if not isinstance(section, dict) or set(section) != {"id", "title", "priority", "events"} or \
                type(section["priority"]) is not int or \
                (section["id"], section["title"], section["priority"]) != (sid, title, priority) or \
                not isinstance(section["events"], list):
            raise GenerationError("Feed generado inválido: sección.")
        for event in section["events"]:
            if not isinstance(event, dict) or not event_fields <= set(event) or \
                    set(event) - event_fields - score_fields - {"channels"} or \
                    any(not isinstance(event[key], str) for key in event_fields):
                raise GenerationError("Feed generado inválido: evento.")
            if "channels" in event:
                try:
                    validate_references(event["channels"], normalize)
                except ChannelRulesError:
                    raise GenerationError("Feed generado inválido: referencias de canales.") from None
            present_scores = score_fields.intersection(event)
            if present_scores and (present_scores != score_fields or any(
                type(event[key]) is not int or event[key] < 0 for key in present_scores
            )):
                raise GenerationError("Feed generado inválido: marcador incompleto o no entero no negativo.")
            if not re.fullmatch(r"fixture-[1-9]\d*", event["id"]) or event["id"] in event_ids or \
                    event["sport"] != "football" or any(not event[key].strip() for key in
                    ("competition", "homeTeam", "awayTeam", "status")) or (event["startTime"] and
                    not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", event["startTime"])):
                raise GenerationError("Feed generado inválido: campos del evento.")
            event_ids.add(event["id"])


def write_feed_atomic(feed, output):
    validate_feed(feed)
    # Keep last content-change timestamp (and original bytes) on identical successful refreshes.
    try:
        previous = decode_json(output.read_bytes())
        validate_feed(previous)
        if {key: value for key, value in previous.items() if key != "updatedAt"} == \
                {key: value for key, value in feed.items() if key != "updatedAt"}:
            feed["updatedAt"] = previous["updatedAt"]
            return False
    except (OSError, GenerationError):
        pass
    temporary = output.with_name(output.name + ".tmp")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(feed, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        validate_feed(decode_json(temporary.read_bytes()))
        os.replace(temporary, output)
    except (OSError, TypeError, ValueError) as error:
        raise GenerationError("No se pudo escribir/reemplazar la salida; se conserva el archivo anterior.") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # A filesystem cleanup failure must not hide the safe generation error.
            pass
    return True


def previous_feed(output):
    try:
        previous = decode_json(output.read_bytes())
        validate_feed(previous)
        return previous
    except (OSError, GenerationError):
        return None


def needs_live_refresh(previous, day, now):
    if previous is None or previous["date"] != day.isoformat():
        return True  # Seed the new day's real feed, never substitute examples.
    local_now = now.astimezone(argentina_timezone())
    for section in previous["sections"]:
        for event in section["events"]:
            if event["status"] == "LIVE":
                return True
            if event["status"] == "SCHEDULED" and event["startTime"]:
                kickoff = datetime.fromisoformat(f"{day.isoformat()}T{event['startTime']}:00") \
                    .replace(tzinfo=argentina_timezone())
                if -timedelta(minutes=10) <= local_now - kickoff <= timedelta(hours=4):
                    return True
    return False


def log_feed_changes(previous, incoming):
    if previous is None or previous["date"] != incoming["date"]:
        return
    old_events = {event["id"]: event for section in previous["sections"] for event in section["events"]}
    for section in incoming["sections"]:
        for event in section["events"]:
            old = old_events.get(event["id"])
            if old is None:
                continue
            old_scores = (old.get("homeScore"), old.get("awayScore"))
            new_scores = (event.get("homeScore"), event.get("awayScore"))
            if old_scores != new_scores:
                def label(scores):
                    return "none" if None in scores else f"{scores[0]}-{scores[1]}"
                realtime_log("SCORE_CHANGED", fixture=event["id"].removeprefix("fixture-"),
                             old=label(old_scores), new=label(new_scores))
            if old["status"] != event["status"]:
                safe = {"LIVE", "FINISHED", "SCHEDULED", "CANCELLED", "POSTPONED"}
                realtime_log("STATUS_CHANGED", fixture=event["id"].removeprefix("fixture-"),
                             old=old["status"] if old["status"] in safe else "UNKNOWN",
                             new=event["status"] if event["status"] in safe else "UNKNOWN")


def daily_context(day):
    try:
        return load_daily_context(ROOT / "sports_broadcasts_today.json", day,
                                  ROOT / "mbox_sports_channels_clean.json", normalize)
    except (BroadcastError, ChannelRulesError) as error:
        raise GenerationError(str(error)) from None


def apply_broadcast_context(feed, context, now=None):
    config, groups = context
    if config is not None:
        try:
            # Resolve against freshly generated fixtures, never against the cache.
            apply_daily_assignments(feed, resolve_broadcasts(config, feed, groups, normalize))
        except BroadcastError as error:
            raise GenerationError(str(error)) from None
    # Evidence remains in the daily file; availability is decided after every expansion.
    apply_playback_policy(feed, now or datetime.now(argentina_timezone()), argentina_timezone(), groups)
    validate_feed(feed)
    return feed


def refresh_channels(day, output, now=None):
    """Offline regeneration on current sports data; no API call or timestamp change."""
    feed = previous_feed(output)
    if feed is None or feed["date"] != day.isoformat():
        raise GenerationError("Se requiere un feed válido del día objetivo para regenerar canales.")
    for section in feed["sections"]:
        for event in section["events"]:
            event.pop("channels", None)
    feed = apply_broadcast_context(feed, daily_context(day), now)
    return feed, write_feed_atomic(feed, output)


def generate(day, output, input_path=None, now=None, live_only=False, discover_broadcasts=False, discovery_scope='all'):
    if live_only and discover_broadcasts:
        raise GenerationError('LIVE reutiliza broadcasts diarios; no ejecuta discovery.')
    real_clock = now is None and input_path is None
    now = now or datetime.now(argentina_timezone())
    started_day = now.astimezone(argentina_timezone()).date()
    previous = previous_feed(output)
    manual = input_path is None and os.environ.get("GITHUB_ACTIONS") == "true" and \
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    context = daily_context(day)  # Validate dependencies before any write or API request.
    if live_only and not manual and not needs_live_refresh(previous, day, now):
        # Legacy finished caches may still expose channels even without an API candidate.
        if previous is not None and previous["date"] == day.isoformat():
            safe = apply_playback_policy(copy.deepcopy(previous), now, argentina_timezone(), context[1])
            if safe != previous:
                if real_clock and datetime.now(argentina_timezone()).date() != started_day:
                    raise RefreshSkipped("argentina_day_changed")
                safe["updatedAt"] = now.astimezone(argentina_timezone()).isoformat(timespec="seconds")
                changed = write_feed_atomic(safe, output)
                realtime_log("PLAYBACK_CACHE_SANITIZED", source="cache", api_calls=0)
                return safe, sum(len(s["events"]) for s in safe["sections"]), 0, changed
        raise RefreshSkipped("no_live_or_near_kickoff")
    if input_path is not None:
        if output.resolve() == REAL_OUTPUT.resolve():
            raise GenerationError("--input es una prueba: usa una salida distinta de sports_today.json.")
        try:
            payload = decode_json(input_path.read_bytes())
        except OSError:
            raise GenerationError("No se pudo leer el archivo --input.") from None
    else:
        payload = fetch_fixtures(day)
    realtime_log("OFFLINE_RESPONSE" if input_path is not None else "REALTIME_RESPONSE",
                 fixtures=len(validate_api_response(payload)))
    feed, ignored = build_feed(payload, day, now)
    config, groups = context
    if config is not None:
        # Reject invalid IDs before discovery can reconstruct/drop an assignment.
        try:
            resolve_broadcasts(config, feed, groups, normalize)
        except BroadcastError as error:
            raise GenerationError(str(error)) from None
    discovery_outputs = None
    if discover_broadcasts:
        countries = {item['fixture']['id']: item.get('league', {}).get('country', '') for item in payload['response']}
        try:
            config, cache, report, groups = discover_from_root(feed, ROOT, previous=config,
                now=now, scope=discovery_scope, countries=countries)
            discovery_outputs = (cache, report)
            realtime_log('BROADCAST_DISCOVERY', investigated=report['investigated'],
                confirmed=report['counts']['CONFIRMED_MBOX'], requests=report['requests'],
                warnings=len(report.get('providerWarnings', [])))
        except (DiscoveryError, ChannelRulesError, BroadcastError, OSError, ValueError, TypeError, KeyError) as error:
            realtime_log('BROADCAST_DISCOVERY_WARNING', reason=type(error).__name__)
            if config is None:
                config = {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': TIMEZONE, 'broadcasts': []}
            retained = {item['fixtureId']: item for item in config['broadcasts'] if item['confidence'] == 'confirmed'}
            details = [{'fixtureId': int(event['id'].removeprefix('fixture-')), 'homeTeam': event['homeTeam'],
                'awayTeam': event['awayTeam'], 'competition': event['competition'], 'startTime': event['startTime'],
                'state': 'CONFIRMED_MBOX' if int(event['id'].removeprefix('fixture-')) in retained else 'UNRESOLVED',
                'broadcaster': retained.get(int(event['id'].removeprefix('fixture-')), {}).get('broadcaster', []),
                'channelGroups': retained.get(int(event['id'].removeprefix('fixture-')), {}).get('channelGroups', []),
                'evidence': []} for section in feed['sections'] for event in section['events']]
            discovery_outputs = (None, {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': TIMEZONE,
                'state': 'DISCOVERY_FAILED', 'warning': type(error).__name__, 'totalFixtures': len(details),
                'investigated': 0, 'requests': 0, 'fixtures': details,
                'counts': {state: sum(row['state'] == state for row in details) for state in
                    ('CONFIRMED_MBOX', 'CONFIRMED_EXTERNAL', 'REVIEW', 'UNRESOLVED')}})
        context = (config, groups)
    apply_broadcast_context(feed, context, now)
    realtime_log("LIVE_FIXTURES", count=sum(event["status"] == "LIVE" for section in feed["sections"]
                                           for event in section["events"]),
                 source="offline" if input_path is not None else "api")
    if previous is not None and previous["date"] == feed["date"] and \
            any(section["events"] for section in previous["sections"]) and \
            not any(section["events"] for section in feed["sections"]):
        raise GenerationError("Agenda vacía inesperada para el mismo día; se conserva el feed anterior.")
    if discovery_outputs is not None:
        if real_clock and started_day == day and datetime.now(argentina_timezone()).astimezone(argentina_timezone()).date() != day:
            raise RefreshSkipped('argentina_day_changed')
        try:
            write_broadcasts_atomic(config, ROOT / 'sports_broadcasts_today.json')
        except BroadcastError as error:
            raise GenerationError(str(error)) from None
        for filename, value in zip(('sports_broadcast_discovery_cache.json', 'sports_broadcast_discovery_report.json'), discovery_outputs):
            if value is not None:
                try:
                    atomic_json(ROOT / filename, value)
                except OSError:
                    realtime_log('BROADCAST_DISCOVERY_WARNING', reason='DiagnosticWriteFailed')
    changed = write_feed_atomic(feed, output)
    if changed:
        log_feed_changes(previous, feed)
    return feed, len(payload["response"]), ignored, changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Respuesta API guardada; no hace requests ni escribe el feed real.")
    parser.add_argument("--output", type=Path, help="Ruta alternativa de salida.")
    parser.add_argument("--date", type=date.fromisoformat, help="Fecha YYYY-MM-DD; por defecto hoy en Argentina.")
    parser.add_argument("--live", action="store_true", help="Consultar solo si hay LIVE, inicio cercano o fecha nueva.")
    parser.add_argument('--discover-broadcasts', action='store_true', help='Descubrimiento público antes de generar (solo GENERAL).')
    parser.add_argument('--discovery-scope', choices=('all', 'unresolved'), default='all', help='Todos o futuros sin resolver/REVIEW.')
    parser.add_argument("--refresh-channels", action="store_true",
                        help="Regenerar canales diarios sobre el feed actual sin consultar APIs ni cambiar metadata.")
    args = parser.parse_args(argv)
    if args.discover_broadcasts and (args.live or args.input is not None or args.refresh_channels):
        parser.error('--discover-broadcasts no se combina con LIVE ni modos offline.')
    if args.refresh_channels and (args.input is not None or args.live):
        parser.error("--refresh-channels no se combina con --input o --live.")
    day = args.date or datetime.now(argentina_timezone()).date()
    output = args.output or (ROOT / "sports_today.preview.json" if args.input else REAL_OUTPUT)
    try:
        if args.refresh_channels:
            feed, changed = refresh_channels(day, output)
            print(f"Date: {feed['date']}")
            print(f"Changed: {str(changed).lower()}")
            return 0
        feed, received, ignored, changed = generate(day, output, args.input, live_only=args.live,
            discover_broadcasts=args.discover_broadcasts, discovery_scope=args.discovery_scope)
    except RefreshSkipped as error:
        realtime_log("REFRESH_SKIPPED", reason=str(error))
        print("Changed: false")
        return 0
    except GenerationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Date: {feed['date']}")
    print(f"API fixtures received: {received}")
    for section in feed["sections"]:
        print(f"{section['title']} selected: {len(section['events'])}")
    print(f"Ignored fixtures: {ignored}")
    print(f"Output: {output}")
    print(f"Changed: {str(changed).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
