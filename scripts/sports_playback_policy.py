"""Playback safety after assignment expansion; never edits results or infers a broadcaster."""
from datetime import date, datetime, timedelta
import re
import unicodedata

TIMEZONE = "America/Argentina/Buenos_Aires"
LIVE_LIMIT = timedelta(hours=3)  # Inclusive; stale only strictly after kickoff + 3h.


def kickoff(feed, event, zone):
    try:
        day = date.fromisoformat(feed["date"])
        time = event["startTime"]
        if feed["timezone"] != TIMEZONE or not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
            return None
        return datetime.fromisoformat(f"{day.isoformat()}T{time}:00").replace(tzinfo=zone)
    except (KeyError, TypeError, ValueError):
        return None


def football(event):
    value = unicodedata.normalize("NFKD", event.get("sport", "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c)).strip()
    return value in {"football", "futbol", "soccer"}


def apply_playback_policy(feed, now, zone, groups=None, log=print):
    """Group identity is obtained only from the curated base; legacy fallback uses exact stream IDs.

    A later LIVE/SCHEDULED/FINISHED fixture at or after kickoff supersedes the shared
    signal. POSTPONED/CANCELLED/unknown fixtures do not assert that a broadcast started.
    Only the superseded group's options are removed; unrelated options remain.
    """
    if now.tzinfo is None:
        raise ValueError("Playback policy requires an aware clock")
    now = now.astimezone(zone)
    events = [event for section in feed["sections"] for event in section["events"]]
    membership = {}
    for identifier, group in (groups or {}).items():
        for stream in group["streams"]:
            if stream["enabled"]:
                membership.setdefault(stream["streamId"], set()).add(("group", identifier))

    def keys(channel):
        sid = channel.get("streamId")
        if not channel.get("enabled", True) or type(sid) is not int or sid <= 0:
            return set()
        return membership.get(sid, set()) | {("stream", sid)}

    # Snapshot before removal: a completed later match still proves the signal moved on.
    starts = {event["id"]: kickoff(feed, event, zone) for event in events}
    signals = {event["id"]: set().union(*(keys(c) for c in event.get("channels", []))) for event in events}
    for event in events:
        start = starts[event["id"]]
        status = event["status"]
        reason = None
        if status not in {"LIVE", "SCHEDULED"}:
            reason = status  # Explicit terminal/special and unknown states are non-playable.
        elif start is None or start.date() != now.date():
            reason = "INVALID_PLAYBACK_TIME"
        elif status == "LIVE" and football(event) and now > start + LIVE_LIMIT:
            reason = "STALE_LIVE"
        if reason is not None:
            if event.pop("channels", None):
                log(f"SPORTS_PLAYBACK {reason} fixtureId={event['id']}")
            continue
        superseded = set()
        for later in events:
            later_start = starts[later["id"]]
            if later["status"] in {"LIVE", "SCHEDULED", "FINISHED"} and later_start is not None and \
                    start < later_start <= now and later_start.date() == now.date():
                superseded |= signals[event["id"]] & signals[later["id"]]
        if superseded:
            remaining = [channel for channel in event.get("channels", []) if not (keys(channel) & superseded)]
            if len(remaining) != len(event.get("channels", [])):
                log(f"SPORTS_PLAYBACK SUPERSEDED_BROADCAST fixtureId={event['id']}")
                if remaining:
                    event["channels"] = [{**channel, "priority": index + 1} for index, channel in enumerate(remaining)]
                else:
                    event.pop("channels", None)
    return feed
