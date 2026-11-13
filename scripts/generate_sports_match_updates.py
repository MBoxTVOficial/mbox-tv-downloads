#!/usr/bin/env python3
"""Publish only a small, verified public match-state sidecar (stdlib only)."""
import argparse
from datetime import date, datetime
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.broadcaster_discovery import atomic_json
from scripts.broadcaster_sources import ZONE, norm
from scripts.prioritized_agendas import validate_guides
from scripts.sports_channel_rules import load_clean_groups
from scripts.sports_daily_broadcasts import validate_broadcasts
from scripts.sports_match_updates import (MatchUpdatesError, OleBulkTransport,
    agenda_date, confirm_finished, eligible_events, merge_updates, parse_ole_bulk, pending_events, read_json,
    reserve_request, validate_request_state, validate_updates)

ROOT = Path(__file__).resolve().parents[1]


def refresh(root, output, day, now, source_input=None, transport=None, state_output=None):
    """Offline input is useful for review; network requests are never per fixture."""
    if now.tzinfo is None or now.astimezone(ZONE).date() != day:
        raise MatchUpdatesError('current_art_date_required')
    feed = read_json(root / 'sports_today.json', optional=True)
    guides = validate_guides(read_json(root / 'sports_agenda_guides.json'))
    groups = load_clean_groups(root / 'mbox_sports_channels_clean.json', norm)
    window = read_json(root / 'sports_agenda_window.json', optional=True)
    daily = read_json(root / 'sports_broadcasts_today.json', optional=True)
    if daily is not None:
        validate_broadcasts(daily)
    previous = read_json(output, optional=True)
    if previous is not None:
        validate_updates(previous)
    state_output = state_output or root / 'sports_match_updates_state.json'
    state = read_json(state_output, optional=True) if source_input is None else None
    validate_request_state(state)
    available_date = agenda_date(feed)
    if available_date != day:
        # GENERAL may naturally publish the new day after this workflow starts.
        # Do not query against another day's identity or rewrite last-good files.
        return previous, {'eligible': 0, 'matched': 0, 'sourceFailed': False,
            'changed': False, 'requests': 0, 'pending': 0, 'stateChanged': False,
            'agendaUnavailable': True,
            'availableDate': available_date.isoformat() if available_date is not None else 'missing'}
    # Avoid even a grouped lookup when there is no configured confirmed fixture.
    eligible = eligible_events(feed, day, guides, groups, window, daily)
    pending = pending_events(eligible, day, now, guides, previous, state)
    rows, failed, requests, state_changed = [], False, 0, False
    should_request = bool(pending)
    if source_input is not None:
        should_request = bool(eligible)  # Offline review is independent of network windows/budget.
    elif should_request:
        state = reserve_request(state, day, now)
        should_request = state is not None
        if should_request:
            state_changed = atomic_json(state_output, state)  # Reserve before I/O, including failed attempts.
    if should_request:
        try:
            if source_input is None:
                requests = 1
            payload = read_json(source_input) if source_input is not None else (transport or OleBulkTransport()).fetch(day)
            rows = parse_ole_bulk(payload, day)
        except (MatchUpdatesError, OSError, TimeoutError):
            rows, failed = None, True
        if source_input is None:
            confirmed = confirm_finished(state, previous, rows, eligible, guides, day, now)
            state_changed = atomic_json(state_output, confirmed) or state_changed
    value, diagnostics = merge_updates(feed, day, now, rows, guides, groups,
        previous=previous, window=window, daily=daily)
    # Even a differently formatted last-good file stays byte-for-byte intact on
    # failure or an unchanged observation.
    changed = value is not None and value != previous and atomic_json(output, value)
    diagnostics.update(changed=bool(changed), requests=requests, sourceFailed=failed,
                       pending=len(pending), stateChanged=state_changed)
    return value, diagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path,
        help='Default: <root>/sports_match_updates.json; agenda files are never written.')
    parser.add_argument('--date', type=date.fromisoformat,
        help='Must be the current ART date; a past/future day is rejected before a request.')
    parser.add_argument('--input', type=Path, help='Read a saved Olé bulk response without network.')
    parser.add_argument('--state-output', type=Path,
        help='Persistent request budget, default: <root>/sports_match_updates_state.json.')
    args = parser.parse_args(argv)
    now = datetime.now(ZONE)
    day = args.date or now.date()
    output = args.output or args.root / 'sports_match_updates.json'
    protected = {'sports_today.json', 'sports_agenda_window.json', 'sports_broadcasts_today.json',
                 'sports_agenda_guides.json', 'mbox_sports_channels_clean.json'}
    actual_state = args.state_output or args.root / 'sports_match_updates_state.json'
    if output.resolve().name in protected or output.resolve() == actual_state.resolve() or (args.state_output is not None and
            (args.state_output.resolve().name in protected or args.state_output.resolve() == output.resolve())):
        parser.error('Output must be a separate match-updates sidecar.')
    try:
        _, diagnostics = refresh(args.root, output, day, now, source_input=args.input,
                                 state_output=args.state_output)
    except (ValueError, OSError):
        print('SPORTS_MATCH_UPDATES input_invalid output_preserved=true', file=sys.stderr)
        return 1
    # Safe counts/booleans only; never remote error text or configuration fields.
    print('SPORTS_MATCH_UPDATES ' + ' '.join(f'{key}={str(value).lower()}' for key, value in diagnostics.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
