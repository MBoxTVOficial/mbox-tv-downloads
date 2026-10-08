"""Explicit daily assignments; offline validation and canonical group expansion."""
import copy
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile

if __package__:
    from .sports_channel_rules import (ChannelRulesError, group_id, load_clean_groups,
                                      validate_config, resolve_channel_groups, channels_for_event)
else:
    from sports_channel_rules import (ChannelRulesError, group_id, load_clean_groups,
                                     validate_config, resolve_channel_groups, channels_for_event)

TIMEZONE = "America/Argentina/Buenos_Aires"


def load_daily_context(path, day, clean_path, normalize):
    config = read_broadcasts(path)
    if config is None or config["date"] != day.isoformat() or not config["broadcasts"]:
        return None, {}  # Date-scoped assignments; yesterday cannot leak into today.
    try:
        groups = load_clean_groups(clean_path, normalize)
        for item in config["broadcasts"]:
            for identifier in item["channelGroups"]:
                if identifier not in groups:
                    raise BroadcastError(f"Unknown channelGroup: {identifier}")
                if item["confidence"] == "confirmed" and not any(stream["enabled"] for stream in groups[identifier]["streams"]):
                    raise BroadcastError(f"Empty channelGroup: {identifier} (no enabled streams)")
    except ChannelRulesError as error:
        raise BroadcastError(str(error)) from None
    return config, groups


class BroadcastError(ValueError):
    """Safe configuration errors: never include raw JSON or notes."""


def validate_broadcasts(value):
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "date", "timezone", "broadcasts"} or \
            type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise BroadcastError("sports_broadcasts_today.json inválido: schemaVersion/estructura.")
    day = value["date"]
    try:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError()
        date.fromisoformat(day)
    except ValueError:
        raise BroadcastError("sports_broadcasts_today.json inválido: date debe ser YYYY-MM-DD.") from None
    if value["timezone"] != TIMEZONE or not isinstance(value["broadcasts"], list):
        raise BroadcastError("sports_broadcasts_today.json inválido: timezone/broadcasts.")
    seen = set()
    for item in value["broadcasts"]:
        if not isinstance(item, dict) or not {"fixtureId", "channelGroups", "confidence"} <= set(item) or \
                set(item) - {"fixtureId", "channelGroups", "confidence", "notes", "source", "broadcaster", "evidenceCount"}:
            raise BroadcastError("Asignación diaria inválida: campos no permitidos o incompletos.")
        identifier = item["fixtureId"]
        if type(identifier) is not int or identifier <= 0:
            raise BroadcastError("Asignación diaria inválida: fixtureId debe ser entero positivo.")
        if identifier in seen:
            raise BroadcastError(f"Duplicate daily fixtureId: {identifier}")
        seen.add(identifier)
        if not isinstance(item["channelGroups"], list) or not item["channelGroups"]:
            raise BroadcastError("Asignación diaria inválida: channelGroups requiere una lista no vacía.")
        try:
            for identifier in item["channelGroups"]:
                group_id(identifier)
        except ChannelRulesError:
            raise BroadcastError("Asignación diaria inválida: IDs de channelGroups no válidos.") from None
        if item["confidence"] not in ("confirmed", "probable", "unknown"):
            raise BroadcastError("Asignación diaria inválida: confidence debe ser confirmed, probable o unknown.")
        if "source" in item and item["source"] not in ("manual", "auto"):
            raise BroadcastError("Asignación diaria inválida: source debe ser manual o auto.")
        if "broadcaster" in item and (not isinstance(item["broadcaster"], list) or any(
                not isinstance(name, str) or not name.strip() or len(name) > 100 or '://' in name or
                any(ord(c) < 32 for c in name) for name in item["broadcaster"])):
            raise BroadcastError("Asignación diaria inválida: broadcaster debe contener nombres simples.")
        if "evidenceCount" in item and (type(item["evidenceCount"]) is not int or item["evidenceCount"] < 0):
            raise BroadcastError("Asignación diaria inválida: evidenceCount debe ser entero no negativo.")
        notes = item.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 2000 or any(ord(c) < 32 for c in notes):
            raise BroadcastError("Asignación diaria inválida: notes debe ser texto simple.")
    return value


def read_broadcasts(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise BroadcastError("No se pudo leer sports_broadcasts_today.json; se conserva la salida.") from None
    try:
        def unique_keys(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError):
        raise BroadcastError("sports_broadcasts_today.json inválido: JSON UTF-8.") from None
    return validate_broadcasts(value)




def resolve_broadcasts(config, feed, groups, normalize):
    validate_broadcasts(config)
    if config["date"] != feed["date"] or feed["timezone"] != TIMEZONE:
        raise BroadcastError("La fecha de transmisiones no coincide con sports_today.json.")
    events = {event["id"]: event for section in feed["sections"] for event in section["events"]}
    result = {}
    for item in config["broadcasts"]:
        identifier = f"fixture-{item['fixtureId']}"
        if identifier not in events:
            raise BroadcastError(f"Unknown daily fixtureId: {item['fixtureId']} (no existe en el feed del mismo día)")
        for name in item["channelGroups"]:
            if name not in groups:
                raise BroadcastError(f"Unknown channelGroup: {name}")
        if item["confidence"] != "confirmed":
            continue
        rule = {"id": f"today-{config['date']}-{identifier}", "priority": 1, "enabled": True,
                "fixtureIds": [item["fixtureId"]], "channelGroups": item["channelGroups"]}
        try:
            resolved = resolve_channel_groups(validate_config({"schemaVersion": 1, "rules": [rule]}, normalize), groups)
            result[identifier] = channels_for_event(events[identifier], resolved, normalize)
        except ChannelRulesError as error:
            raise BroadcastError(str(error)) from None
    return result




def apply_daily_assignments(feed, assignments):
    for section in feed["sections"]:
        for event in section["events"]:
            if event["id"] in assignments:
                # Authoritative fixture override: lower-level rules cannot add unconfirmed channels.
                event["channels"] = copy.deepcopy(assignments[event["id"]])
    return feed


def write_broadcasts_atomic(value, path):
    validate_broadcasts(value)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        read_broadcasts(temporary)
        os.replace(temporary, path)
    except (OSError, ValueError):
        raise BroadcastError("No se pudo guardar la asignación diaria; se conserva el archivo anterior.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
