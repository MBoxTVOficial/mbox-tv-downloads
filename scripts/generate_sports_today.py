#!/usr/bin/env python3
"""Generate MBox's daily football feed using only Python's standard library."""

import argparse
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

TIMEZONE = "America/Argentina/Buenos_Aires"
ENDPOINT = "https://v3.football.api-sports.io/fixtures"
ROOT = Path(__file__).resolve().parents[1]
REAL_OUTPUT = ROOT / "sports_today.json"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class GenerationError(Exception):
    """A safe, user-facing error: never contains API headers or remote error text."""


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
CONMEBOL_LEAGUES = (
    LeagueRule(("Copa Libertadores", "CONMEBOL Libertadores")),
    LeagueRule(("Copa Sudamericana", "CONMEBOL Sudamericana")),
    LeagueRule(("Recopa Sudamericana", "CONMEBOL Recopa")),
)
CHAMPIONS_LEAGUES = (
    LeagueRule(("UEFA Champions League",), ids=(2,)),
)
INTERNATIONAL_LEAGUES = (
    LeagueRule(("Premier League",), ("England",), (39,)),
    LeagueRule(("La Liga",), ("Spain",), (140,)),
    LeagueRule(("Serie A",), ("Italy",)),
    LeagueRule(("Bundesliga",), ("Germany",)),
    LeagueRule(("Ligue 1",), ("France",)),
    LeagueRule(("Major League Soccer", "MLS"), ("USA", "United States")),
    LeagueRule(("UEFA Europa League", "UEFA Conference League", "UEFA Europa Conference League")),
    LeagueRule(("World Cup", "FIFA World Cup", "Copa America", "Euro Championship", "UEFA Euro")),
    LeagueRule(("World Cup - Qualification South America", "World Cup - Qualification Europe",
                "World Cup - Qualification CONCACAF", "World Cup - Qualification Africa",
                "World Cup - Qualification Asia", "World Cup - Qualification Oceania")),
    LeagueRule(("UEFA Nations League",)),
)

SECTIONS = (
    ("argentina", "Fútbol - Argentina", 1, ARGENTINA_LEAGUES),
    ("conmebol", "Copas CONMEBOL", 2, CONMEBOL_LEAGUES),
    ("champions", "Champions League", 3, CHAMPIONS_LEAGUES),
    ("international", "Fútbol - Internacional", 4, INTERNATIONAL_LEAGUES),
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


def classify_league(league):
    league_id = league.get("id")
    # Identity takes precedence over names, including translated names.
    if type(league_id) is int:
        for section_id, _, _, rules in SECTIONS:
            if any(league_id in rule.ids for rule in rules):
                return section_id
    name, country = normalize(league.get("name")), normalize(league.get("country"))
    for section_id, _, _, rules in SECTIONS:
        for rule in rules:
            if name in map(normalize, rule.names) and (
                not rule.countries or country in map(normalize, rule.countries)
            ):
                return section_id
    return None


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
        section_id = classify_league(item["league"])
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
        buckets[section_id].append(event)
        ids.add(fixture_id)
    for section in sections:
        # Stable: ties preserve API order, unknown times go last, never sort by team name.
        section["events"].sort(key=lambda event: (not bool(event["startTime"]), event["startTime"]))
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
    if not isinstance(sections, list) or len(sections) != len(SECTIONS):
        raise GenerationError("Feed generado inválido: secciones.")
    event_ids = set()
    for section, (sid, title, priority, _) in zip(sections, SECTIONS):
        if not isinstance(section, dict) or set(section) != {"id", "title", "priority", "events"} or \
                type(section["priority"]) is not int or \
                (section["id"], section["title"], section["priority"]) != (sid, title, priority) or \
                not isinstance(section["events"], list):
            raise GenerationError("Feed generado inválido: sección.")
        for event in section["events"]:
            if not isinstance(event, dict) or set(event) != {
                "id", "sport", "competition", "homeTeam", "awayTeam", "homeLogo", "awayLogo", "startTime", "status"
            } or any(not isinstance(value, str) for value in event.values()):
                raise GenerationError("Feed generado inválido: evento.")
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


def generate(day, output, input_path=None, now=None):
    if input_path is not None:
        if output.resolve() == REAL_OUTPUT.resolve():
            raise GenerationError("--input es una prueba: usa una salida distinta de sports_today.json.")
        try:
            payload = decode_json(input_path.read_bytes())
        except OSError:
            raise GenerationError("No se pudo leer el archivo --input.") from None
    else:
        payload = fetch_fixtures(day)
    feed, ignored = build_feed(payload, day, now)
    changed = write_feed_atomic(feed, output)
    return feed, len(payload["response"]), ignored, changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Respuesta API guardada; no hace requests ni escribe el feed real.")
    parser.add_argument("--output", type=Path, help="Ruta alternativa de salida.")
    parser.add_argument("--date", type=date.fromisoformat, help="Fecha YYYY-MM-DD; por defecto hoy en Argentina.")
    args = parser.parse_args(argv)
    day = args.date or datetime.now(argentina_timezone()).date()
    output = args.output or (ROOT / "sports_today.preview.json" if args.input else REAL_OUTPUT)
    try:
        feed, received, ignored, changed = generate(day, output, args.input)
    except GenerationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Date: {feed['date']}")
    print(f"API fixtures received: {received}")
    for label, section in zip(("Argentina", "CONMEBOL", "Champions", "International"), feed["sections"]):
        print(f"{label} selected: {len(section['events'])}")
    print(f"Ignored fixtures: {ignored}")
    print(f"Output: {output}")
    print(f"Changed: {str(changed).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
