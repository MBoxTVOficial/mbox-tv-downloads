"""Manual references and offline canonical groups. No IPTV requests or authentication."""
import json
import re


class ChannelRulesError(ValueError):
    pass


def fail(field):
    raise ChannelRulesError(f"sports_channels.json inválido: {field}.")


def text(value, field, normalize):
    if not isinstance(value, str) or not value.strip() or not normalize(value) or "://" in value:
        fail(field)
    return value.strip()


def names(value, field, normalize):
    if not isinstance(value, list):
        fail(field)
    result, seen = [], set()
    for item in value:
        item = text(item, field, normalize)
        key = normalize(item)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def fixture_id(value):
    if type(value) is int and value > 0:
        return f"fixture-{value}"
    if isinstance(value, str):
        match = re.fullmatch(r"(?:fixture-)?([0-9]+)", value.strip())
        if match and int(match[1]) > 0:
            return f"fixture-{int(match[1])}"
    fail("fixtureIds requiere IDs positivos enteros o fixture-XXXX")


def channel(value, normalize, require_name=True):
    allowed = {"name", "streamId", "aliases", "priority", "enabled"}
    if not isinstance(value, dict) or set(value) - allowed:
        fail("campos de canal")
    name = text(value.get("name"), "name de canal", normalize) if require_name or value.get("name") is not None else None
    stream_id = value.get("streamId")
    if stream_id is not None and (type(stream_id) is not int or stream_id <= 0):
        fail("streamId debe ser null o entero positivo")
    priority, enabled = value.get("priority", 100), value.get("enabled", True)
    if type(priority) is not int or type(enabled) is not bool:
        fail("priority entero y enabled boolean de canal")
    aliases = names(value.get("aliases", []), "aliases de canal", normalize)
    if name is None and stream_id is None and not aliases:
        fail("canal sin identidad")
    return {"name": name, "streamId": stream_id, "aliases": aliases, "priority": priority, "enabled": enabled}


def group_id(value):
    # Exact lookup; no normalization, substring search or inferred identity.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        fail("channelGroups requiere IDs exactos válidos")
    return value


def clean_fail(field):
    raise ChannelRulesError(f"mbox_sports_channels_clean.json inválido: {field}.")


def clean_text(value, field, normalize):
    if not isinstance(value, str) or not value.strip() or not normalize(value) or any(ord(c) < 32 for c in value) or \
            re.search(r"(?i)([a-z][a-z0-9+.-]*://|player_api\.php|get\.php|\b(?:username|password|token|api[_ -]?key|host|baseurl|serverurl)\s*[=:])", value):
        clean_fail(field)
    return value  # Preserve curated spelling, whitespace and aliases.


def clean_aliases(value, field, normalize):
    if not isinstance(value, list):
        clean_fail(field)
    return [clean_text(alias, field, normalize) for alias in value]


def validate_clean_groups(config, normalize):
    if not isinstance(config, dict) or type(config.get("schemaVersion")) is not int or config["schemaVersion"] != 1 or \
            not isinstance(config.get("groups"), list):
        clean_fail("schemaVersion/groups")
    groups = list(config["groups"])
    # Optional special references require an ID actually stored in the curated file.
    # eventNumber and array position are never turned into invented canonical IDs.
    for field in ("libertadoresEvents", "sudamericanaEvents"):
        entries = config.get(field, [])
        if not isinstance(entries, list):
            clean_fail(field)
        for entry in entries:
            if not isinstance(entry, dict):
                clean_fail(field)
            if "id" in entry:
                groups.append({"id": entry["id"], "canonicalName": entry.get("canonicalName", entry.get("name")),
                    "aliases": entry.get("aliases", []), "streams": [{**entry, "priority": entry.get("priority", 1)}]})
    result = {}
    for value in groups:
        if not isinstance(value, dict):
            clean_fail("grupo")
        try:
            identifier = group_id(value.get("id"))
        except ChannelRulesError:
            clean_fail("id de grupo")
        if identifier in result:
            clean_fail("IDs de grupos duplicados")
        canonical = clean_text(value.get("canonicalName"), "canonicalName", normalize)
        aliases = clean_aliases(value.get("aliases"), "aliases de grupo", normalize)
        if not isinstance(value.get("streams"), list):
            clean_fail("streams")
        streams, ids = [], set()
        for item in value["streams"]:
            if not isinstance(item, dict):
                clean_fail("stream")
            stream_id, priority, enabled = item.get("streamId"), item.get("priority"), item.get("enabled")
            if type(stream_id) is not int or stream_id <= 0 or type(priority) is not int or priority <= 0 or type(enabled) is not bool:
                clean_fail("streamId/priority positivos enteros y enabled boolean")
            if stream_id in ids:
                clean_fail("streamId duplicado dentro de grupo")
            ids.add(stream_id)
            raw_name = item.get("name")
            if raw_name is None or (isinstance(raw_name, str) and not raw_name.strip()):
                name = canonical
            else:
                name = clean_text(raw_name, "name de stream", normalize)
            streams.append({"name": name, "streamId": stream_id, "priority": priority, "enabled": enabled,
                "aliases": clean_aliases(item.get("aliases", []), "aliases de stream", normalize)})
        region = value.get('signalRegion') or ''
        if not isinstance(region, str):
            clean_fail('signalRegion')
        result[identifier] = {"id": identifier, "canonicalName": canonical, "aliases": aliases, "signalRegion": region,
                              "streams": sorted(streams, key=lambda item: item["priority"])}
    return result


def load_clean_groups(path, normalize):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ChannelRulesError("Falta mbox_sports_channels_clean.json: channelGroups requiere la base curada; se conserva el feed.") from None
    except OSError:
        raise ChannelRulesError("No se pudo leer mbox_sports_channels_clean.json; se conserva el feed.") from None
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        clean_fail("JSON UTF-8")
    return validate_clean_groups(value, normalize)


def resolve_channel_groups(rules, groups):
    result = []
    for rule in rules:
        if not rule["channelGroups"]:
            result.append(rule)
            continue
        channels = []
        for identifier in rule["channelGroups"]:
            if identifier not in groups:
                raise ChannelRulesError(f"Unknown channelGroup: {identifier}")
            group = groups[identifier]
            enabled = [item for item in group["streams"] if item["enabled"]]
            if not enabled:
                raise ChannelRulesError(f"Empty channelGroup: {identifier} (no enabled streams)")
            for item in enabled:
                channels.append({**item, "aliases": list(dict.fromkeys(item["aliases"] + group["aliases"]))})
        channels.extend(sorted(rule["channels"], key=lambda item: item["priority"]))
        # Consecutive priorities cannot overflow fixed-size group buckets.
        channels = [{**item, "priority": index + 1} for index, item in enumerate(channels)]
        result.append({**rule, "channels": channels, "groupsResolved": True})
    return result


def validate_config(config, normalize):
    if not isinstance(config, dict) or set(config) != {"schemaVersion", "rules"} or \
            type(config.get("schemaVersion")) is not int or config["schemaVersion"] != 1 or \
            not isinstance(config.get("rules"), list):
        fail("schemaVersion/rules")
    allowed = {"id", "enabled", "priority", "competitionAliases", "homeTeams", "awayTeams",
               "teamsAny", "fixtureIds", "channels", "channelGroups", "fallbackOnly"}
    result, ids = [], set()
    for value in config["rules"]:
        if not isinstance(value, dict) or set(value) - allowed:
            fail("campos de regla")
        rule_id = text(value.get("id"), "id de regla", normalize)
        if rule_id in ids:
            fail("ids de reglas duplicados")
        ids.add(rule_id)
        priority, enabled = value.get("priority", 100), value.get("enabled", True)
        if type(priority) is not int or type(enabled) is not bool:
            fail("priority entero y enabled boolean de regla")
        fallback = value.get("fallbackOnly", False)
        if type(fallback) is not bool:
            fail("fallbackOnly debe ser boolean")
        if "channels" not in value and "channelGroups" not in value:
            fail("channels o channelGroups requerido")
        if not isinstance(value.get("channels", []), list):
            fail("channels de regla debe ser lista")
        if not isinstance(value.get("channelGroups", []), list):
            fail("channelGroups de regla debe ser lista")
        rule = {"id": rule_id, "priority": priority, "enabled": enabled,
                "fallbackOnly": fallback,
                "channels": [channel(item, normalize) for item in value.get("channels", [])],
                "channelGroups": [group_id(item) for item in value.get("channelGroups", [])]}
        for field in ("competitionAliases", "homeTeams", "awayTeams", "teamsAny"):
            rule[field] = {normalize(item) for item in names(value.get(field, []), field, normalize)}
        if not isinstance(value.get("fixtureIds", []), list):
            fail("fixtureIds debe ser lista")
        rule["fixtureIds"] = {fixture_id(item) for item in value.get("fixtureIds", [])}
        result.append(rule)
    return sorted(result, key=lambda rule: rule["priority"])


def channels_for_event(event, rules, normalize, respect_specificity=False):
    result = []
    grouped_mode = False
    matched_tiers = set()
    def tier(rule):
        if rule["fixtureIds"]:
            return 0
        if rule["competitionAliases"]:
            return 1
        if rule["homeTeams"] or rule["awayTeams"] or rule["teamsAny"]:
            return 2
        return 3
    if respect_specificity:
        rules = sorted(rules, key=lambda rule: (tier(rule), rule["priority"]))
    for rule in rules:
        if rule.get("channelGroups") and not rule.get("groupsResolved"):
            raise ChannelRulesError("channelGroups sin resolver: cargar la base curada antes de generar.")
        if not rule["enabled"]:
            continue
        if rule.get("fallbackOnly", False) and result:
            continue  # An earlier fixture/competition rule already supplied channels.
        if rule["fixtureIds"] and event["id"] not in rule["fixtureIds"]:
            continue
        filters = (("competitionAliases", (event["competition"],)), ("homeTeams", (event["homeTeam"],)),
                   ("awayTeams", (event["awayTeam"],)), ("teamsAny", (event["homeTeam"], event["awayTeam"])))
        if any(rule[field] and not any(normalize(name) in rule[field] for name in values) for field, values in filters):
            continue
        grouped_mode = grouped_mode or bool(rule.get("channelGroups"))
        for candidate in sorted(rule["channels"], key=lambda item: item["priority"]):
            if not candidate["enabled"]:
                continue
            matched_tiers.add(tier(rule))
            duplicates = [item for item in result if (
                item["streamId"] is not None and candidate["streamId"] is not None and
                item["streamId"] == candidate["streamId"]
            ) or (normalize(item["name"]) == normalize(candidate["name"]) and
                  (item["streamId"] is None or candidate["streamId"] is None))]
            if not duplicates:
                result.append({**candidate, "aliases": list(candidate["aliases"])})
            else:
                # A name-only reference may join one known identity, never collapse two distinct IDs.
                matching_id = candidate["streamId"] or next(
                    (item["streamId"] for item in duplicates if item["streamId"] is not None), None)
                duplicates = [item for item in duplicates if item["streamId"] in (None, matching_id)]
                duplicate = duplicates[0]
                if duplicate["streamId"] is None:
                    duplicate["streamId"] = matching_id
                merged_aliases = duplicate["aliases"] + candidate["aliases"] + [candidate["name"]]
                for extra in duplicates[1:]:
                    merged_aliases += extra["aliases"] + [extra["name"]]
                    result.remove(extra)
                duplicate["aliases"] = names(merged_aliases,
                                             "aliases de canal", normalize)
    if grouped_mode or respect_specificity and len(matched_tiers) > 1:
        # Preserve matched rule precedence and each complete group block, then manual channels.
        # Deduplication keeps the first real ID and merges aliases; the selector gets compact order.
        return [{**item, "priority": index + 1} for index, item in enumerate(result)]
    # Legacy-only output retains its original channel priorities and sorting behavior.
    return sorted(result, key=lambda item: item["priority"])


def validate_references(value, normalize):
    if not isinstance(value, list):
        fail("channels del feed debe ser lista")
    for item in value:
        channel(item, normalize, require_name=False)
