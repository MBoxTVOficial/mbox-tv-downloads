"""Editorial cards and channel-ID overrides never query or replace providers."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import patch
import unittest

from scripts import generate_sports_today as generator
from scripts.broadcaster_sources import ZONE
from scripts.sports_agenda_window import validate_window
from scripts.sports_fixture_identity import IdentityStore
from scripts.sports_manual_agenda import (
    MAX_CONFIG_BYTES, MAX_ENTRIES, ManualAgendaError, apply_manual_agenda,
    validate_manual_catalog, validate_manual_config,
)


DAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 12, tzinfo=ZONE)
CATALOG = {'schemaVersion': 1, 'sourceDate': '2026-10-08', 'coverage': 'PARTIAL_HISTORICAL',
    'serverSpecified': False, 'channels': [
        {'streamId': 165660, 'name': 'ARG: TYC SPORTS HD', 'category': 'ARGENTINA'},
        {'streamId': 165659, 'name': 'ARG: TYC SPORTS HD*', 'category': 'ARGENTINA', 'enabled': True},
        {'streamId': 163123, 'name': 'EVENTO 03', 'category': 'EVENTOS DIARIOS', 'temporary': True,
         'sourceDate': '2026-10-08', 'source': 'sports_channel_catalog.json'},
        {'streamId': 99, 'name': 'Disabled channel', 'category': '', 'enabled': False},
        *[{'streamId': identifier, 'name': f'Canal {identifier}', 'category': 'OTHER'}
          for identifier in range(100, 108)],
    ]}


def config(events=None, overrides=None):
    return {'schemaVersion': 1, 'timezone': 'America/Argentina/Buenos_Aires',
            'events': events or [], 'channelOverrides': overrides or []}


def manual(**changes):
    value = {'id': 'argentina-australia-sub17', 'date': DAY.isoformat(),
             'sectionId': 'international', 'competition': 'Mundial Sub-17',
             'homeTeam': 'Argentina Sub-17', 'awayTeam': 'Australia Sub-17',
             'startTime': '13:00', 'channelIds': [165660, 165659]}
    value.update(changes)
    return value


def automatic(identifier=1234567890, **changes):
    event = {'id': f'fixture-{identifier}', 'sport': 'football', 'competition': 'Amistosos de selecciones',
             'homeTeam': 'Argentina', 'awayTeam': 'Australia', 'startTime': '20:00',
             'homeLogo': '', 'awayLogo': '', 'status': 'SCHEDULED',
             'channels': [{'name': 'Existing confirmed', 'streamId': 987654, 'priority': 1, 'enabled': True}]}
    event.update(changes)
    return event


def feed(day=DAY, events=None):
    value = {'schemaVersion': 1, 'date': day.isoformat(), 'timezone': 'America/Argentina/Buenos_Aires',
             'updatedAt': NOW.replace(hour=10).isoformat(), 'demo': False, 'sportsDataMode': 'DEGRADED',
             'sections': [{'id': sid, 'title': title, 'priority': priority, 'events': []}
                          for sid, title, priority, _ in generator.SECTIONS]}
    value['sections'][-1]['events'] = copy.deepcopy([automatic()] if events is None else events)
    return value


def window(days=1, events=None):
    return {'schemaVersion': 1, 'timezone': 'America/Argentina/Buenos_Aires',
            'generatedAt': NOW.replace(hour=10).isoformat(),
            'days': {(DAY+timedelta(days=offset)).isoformat(): feed(DAY+timedelta(days=offset), events)
                     for offset in range(days)}}


def events(value, day=DAY):
    return [event for section in value['days'][day.isoformat()]['sections'] for event in section['events']]


class ManualAgendaTests(unittest.TestCase):
    def apply(self, value=None, settings=None, identity=None, now=NOW, catalog=CATALOG):
        return apply_manual_agenda(window() if value is None else value,
            config([manual()]) if settings is None else settings, catalog, identity, now)

    def test_sub17_card_adds_both_exact_catalog_ids_without_touching_senior_fixture(self):
        original = window()
        result, identity, report = self.apply(original)
        self.assertEqual(2, len(events(result)))
        youth, senior = events(result)
        self.assertEqual('Mundial Sub-17', youth['competition'])
        self.assertEqual('Argentina Sub-17', youth['homeTeam'])
        self.assertEqual([165660, 165659], [row['streamId'] for row in youth['channels']])
        self.assertEqual([1, 2], [row['priority'] for row in youth['channels']])
        self.assertEqual(events(original)[0], senior)
        self.assertEqual(1, report['eventsAdded'])
        self.assertEqual((0, 0), (report['publicRequests'], report['sportsApiRequests']))
        self.assertIn('manual_agenda:2026-11-20:argentina-australia-sub17', identity['entries'])
        validate_window(result)

    def test_the_real_november_19_example_is_date_scoped_not_a_card_for_today(self):
        yesterday = manual(date='2026-11-19')
        result, identity, report = self.apply(settings=config([yesterday]))
        self.assertEqual(window(), result)
        self.assertEqual({}, identity['entries'])
        self.assertEqual(1, report['inactiveEvents'])
        historical = window()
        historical['days'] = {'2026-11-19': feed(date(2026, 11, 19))}
        historical['generatedAt'] = '2026-11-19T10:00:00-03:00'
        historical['days']['2026-11-19']['updatedAt'] = historical['generatedAt']
        result, _, _ = self.apply(historical, config([yesterday]), now=NOW-timedelta(days=1))
        self.assertEqual(2, len(events(result, date(2026, 11, 19))))

    def test_unmapped_event_stream_id_can_be_used_when_present_in_sanitized_catalog(self):
        override = {'date': DAY.isoformat(), 'fixtureId': 1234567890, 'channelIds': [163123]}
        result, _, report = self.apply(settings=config(overrides=[override]))
        self.assertEqual([163123], [row['streamId'] for row in events(result)[0]['channels']])
        self.assertEqual('EVENTO 03', events(result)[0]['channels'][0]['name'])
        self.assertEqual(1, report['channelOverridesApplied'])

    def test_no_five_channel_limit_and_explicit_input_order_is_preserved(self):
        ids = [107, 101, 106, 100, 105, 103, 102]
        result, _, _ = self.apply(settings=config([manual(channelIds=ids)]))
        self.assertEqual(ids, [row['streamId'] for row in events(result)[0]['channels']])
        self.assertEqual(list(range(1, len(ids)+1)), [row['priority'] for row in events(result)[0]['channels']])

    def test_empty_channel_ids_deliberately_makes_an_automatic_card_informative(self):
        settings = config(overrides=[{'date': DAY.isoformat(), 'fixtureId': 1234567890, 'channelIds': []}])
        result, _, _ = self.apply(settings=settings)
        self.assertEqual([], events(result)[0]['channels'])
        self.assertEqual(automatic()['homeTeam'], events(result)[0]['homeTeam'])

    def test_unrelated_automatic_cards_and_their_channel_options_remain_identical(self):
        value = window(events=[automatic(), automatic(1234567891, homeTeam='Other')])
        settings = config(overrides=[{'date': DAY.isoformat(), 'fixtureId': 1234567890, 'channelIds': [165660]}])
        result, _, _ = self.apply(value, settings)
        self.assertEqual(events(value)[1], events(result)[1])
        self.assertEqual([row['id'] for row in events(value)], [row['id'] for row in events(result)])

    def test_override_missing_fixture_or_other_day_remains_pending_without_name_matching(self):
        settings = config(overrides=[{'date': DAY.isoformat(), 'fixtureId': 1234567891, 'channelIds': [165660]},
                                    {'date': '2026-11-21', 'fixtureId': 1234567890, 'channelIds': [165659]}])
        result, _, report = self.apply(settings=settings)
        self.assertEqual(window(), result)
        self.assertEqual(2, report['pendingOverrides'])
        self.assertEqual(['MANUAL_OVERRIDE_TARGET_PENDING'], report['warnings'])

    def test_prior_day_override_never_applies_to_same_numeric_id_today(self):
        settings = config(overrides=[{'date': '2026-11-19', 'fixtureId': 1234567890, 'channelIds': [163123]}])
        self.assertEqual(window(), self.apply(settings=settings)[0])

    def test_stable_identity_repeated_application_and_no_automatic_identity_reuse(self):
        identity = {'schemaVersion': 1, 'entries': {'argentina_agendas:automatic': 1234567890}}
        first, store, _ = self.apply(identity=identity)
        second, again, _ = self.apply(first, identity=store)
        self.assertEqual(first, second)
        self.assertEqual(store, again)
        self.assertNotEqual('fixture-1234567890', events(first)[0]['id'])
        self.assertEqual(1234567890, again['entries']['argentina_agendas:automatic'])

    def test_identity_collision_fails_closed_without_mutating_the_originals(self):
        manual_key = '2026-11-20:argentina-australia-sub17'
        reserved = IdentityStore().resolve('manual_agenda', manual_key)
        identity = {'schemaVersion': 1, 'entries': {'other_provider:occupied': reserved}}
        old = copy.deepcopy(identity)
        value = window()
        with self.assertRaisesRegex(ManualAgendaError, '^MANUAL_IDENTITY_COLLISION$'):
            self.apply(value, identity=identity)
        self.assertEqual(old, identity)
        self.assertEqual(window(), value)

    def test_manual_reconciliation_cannot_transfer_an_automatic_identity(self):
        identity = {'schemaVersion': 1, 'entries': {'argentina_agendas:auto': 1234567890},
                    'reconciliations': {'manual_agenda:2026-11-20:argentina-australia-sub17': 'argentina_agendas:auto'}}
        with self.assertRaisesRegex(ManualAgendaError, 'RECONCILIATION_FORBIDDEN'):
            self.apply(identity=identity)

    def test_removing_a_manual_entry_removes_only_that_card(self):
        first, identity, _ = self.apply()
        result, _, report = self.apply(first, config(), identity=identity)
        self.assertEqual([automatic()], events(result))
        self.assertEqual(1, report['eventsRemoved'])

    def test_active_future_days_are_supported_without_creating_missing_dates(self):
        settings = config([manual(id='tomorrow', date='2026-11-21'), manual(id='day-after', date='2026-11-22')])
        result, _, report = self.apply(window(days=2), settings)
        self.assertEqual({'2026-11-20', '2026-11-21'}, set(result['days']))
        self.assertEqual(1, report['eventsAdded'])
        self.assertEqual(1, report['pendingEvents'])
        self.assertEqual(2, len(events(result, date(2026, 11, 21))))

    def test_clock_uses_art_when_input_is_utc_and_rollover_expires_old_manual_cards(self):
        first, identity, _ = self.apply(window(days=3))
        next_clock = datetime(2026, 11, 21, 3, 30, tzinfo=timezone.utc)
        result, _, report = self.apply(first, config([manual()]), identity, now=next_clock)
        self.assertNotIn('2026-11-20', result['days'])
        self.assertEqual('2026-11-21T00:30:00-03:00', result['generatedAt'])
        self.assertEqual(1, report['inactiveEvents'])
        validate_window(result)

    def test_no_current_day_in_bundle_naive_clock_or_clock_before_generation_fails_closed(self):
        for now in (NOW.replace(tzinfo=None), NOW.replace(hour=8), NOW+timedelta(days=4)):
            with self.subTest(now=now), self.assertRaises(ManualAgendaError):
                self.apply(now=now)

    def test_exact_duplicate_of_automatic_fixture_is_skipped_with_use_override_warning(self):
        value = manual(competition='Amistosos de selecciones', homeTeam='ARGENTINA', awayTeam='Australia', startTime='20:00')
        result, identity, report = self.apply(settings=config([value]))
        self.assertEqual(window(), result)
        self.assertEqual({}, identity['entries'])
        self.assertEqual(['MANUAL_DUPLICATE_USE_OVERRIDE'], report['warnings'])

    def test_automatic_order_is_preserved_when_inserting_manual_cards_by_time(self):
        value = window(events=[automatic(startTime='20:00'), automatic(1234567891, homeTeam='Other', startTime='15:00')])
        settings = config([manual(id='late', startTime='21:00'), manual(id='early', startTime='13:00')])
        result, _, _ = self.apply(value, settings)
        auto_ids = {event['id'] for event in events(value)}
        self.assertEqual([event['id'] for event in events(value)],
                         [event['id'] for event in events(result) if event['id'] in auto_ids])
        self.assertEqual(['13:00', '21:00'], [event['startTime'] for event in events(result) if event['id'] not in auto_ids])

    def test_output_contains_only_existing_app_schema_without_scores_or_provider_status(self):
        result, _, _ = self.apply()
        event = events(result)[0]
        self.assertEqual({'id', 'sport', 'competition', 'homeTeam', 'awayTeam', 'startTime',
                          'homeLogo', 'awayLogo', 'status', 'channels'}, set(event))
        self.assertEqual('SCHEDULED', event['status'])
        self.assertEqual({'name', 'streamId', 'aliases', 'priority', 'enabled'}, set(event['channels'][0]))
        generator.validate_feed(result['days'][DAY.isoformat()])

    def test_inputs_are_not_mutated_and_overlay_never_uses_network(self):
        value, settings, catalog = window(), config([manual()]), copy.deepcopy(CATALOG)
        original = copy.deepcopy((value, settings, catalog))
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network permitted')):
            self.apply(value, settings, catalog=catalog)
        self.assertEqual(original, (value, settings, catalog))

    def test_catalog_metadata_can_describe_partial_historical_coverage_without_claiming_live_validation(self):
        checked = validate_manual_catalog(CATALOG)
        self.assertEqual('PARTIAL_HISTORICAL', checked['coverage'])
        self.assertFalse(checked['serverSpecified'])
        self.assertTrue(next(row for row in checked['channels'] if row['streamId'] == 163123)['temporary'])

    def test_unknown_disabled_duplicate_nonpositive_and_boolean_channel_ids_are_rejected(self):
        for ids in ([888888], [99], [165660, 165660], [0], [-1], [True], [2147483648], ['165660']):
            with self.subTest(ids=ids), self.assertRaises(ManualAgendaError):
                validate_manual_config(config([manual(channelIds=ids)]), CATALOG)

    def test_unrecognized_config_event_override_and_catalog_fields_are_rejected(self):
        cases = [dict(config(), score=1), config([manual(homeScore=1)]), config([manual(status='LIVE')]),
                 config([manual(url='https://example.org/stream.ts')]), config([manual(venue='Stadium')]),
                 config(overrides=[{'date': DAY.isoformat(), 'fixtureId': 1, 'channelIds': [], 'name': 'Extra'}])]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ManualAgendaError):
                validate_manual_config(value, CATALOG)
        for value in (dict(CATALOG, token='Never accept'),
                      {'schemaVersion': 1, 'channels': [{'streamId': 1, 'name': 'One', 'category': 'Other', 'url': 'x'}]}):
            with self.subTest(catalog=value), self.assertRaises(ManualAgendaError):
                validate_manual_catalog(value)

    def test_exact_date_clock_section_id_and_safe_text_rules(self):
        for changes in ({'date': '2026-02-30'}, {'date': '2026-1-20'}, {'startTime': '7:00'},
                        {'startTime': '24:00'}, {'startTime': '13:00:00'}, {'sectionId': 'new-section'}, {'sectionId': []},
                        {'id': '../unsafe'}, {'id': 'with spaces'}, {'homeTeam': 'Team\nUnsafe'},
                        {'competition': 'https://example.com/'}, {'awayTeam': 'Argentina Sub-17'}):
            with self.subTest(changes=changes), self.assertRaises(ManualAgendaError):
                validate_manual_config(config([manual(**changes)]), CATALOG)
        with self.assertRaises(ManualAgendaError):
            validate_manual_config(dict(config(), timezone='UTC'), CATALOG)

    def test_duplicate_event_id_fixture_and_override_targets_are_rejected(self):
        for settings in (config([manual(), manual(startTime='14:00')]),
                         config([manual(), manual(id='same-fixture')]),
                         config(overrides=[{'date': DAY.isoformat(), 'fixtureId': 1, 'channelIds': []}]*2)):
            with self.subTest(settings=settings), self.assertRaises(ManualAgendaError):
                validate_manual_config(settings, CATALOG)

    def test_catalog_ids_and_descriptive_fields_have_strict_safe_types(self):
        for changes in ({'streamId': True}, {'streamId': 0}, {'name': 'https://example.org/stream.ts'},
                        {'enabled': 'true'}, {'temporary': 'yes'}, {'sourceDate': 'yesterday'}):
            value = {'schemaVersion': 1, 'channels': [{'streamId': 1, 'name': 'One', 'category': 'Other', **changes}]}
            with self.subTest(changes=changes), self.assertRaises(ManualAgendaError):
                validate_manual_catalog(value)
        with self.assertRaisesRegex(ManualAgendaError, 'CATALOG_DUPLICATE_ID'):
            validate_manual_catalog({'schemaVersion': 1, 'channels': [CATALOG['channels'][0]]*2})

    def test_optional_logos_are_public_raster_metadata_and_cannot_hide_stream_urls(self):
        checked = validate_manual_config(config([manual(homeLogo='https://static.example.com/argentina.png', awayLogo='')]), CATALOG)
        self.assertEqual('https://static.example.com/argentina.png', checked['events'][0]['homeLogo'])
        for logo in ('https://u:password@example.com/a.png', 'http://example.com/a.png',
                     'https://127.0.0.1/a.png', 'https://example.com/a.png?token=secret',
                     'https://example.com/get.php', 'https://example.com/stream.m3u8'):
            with self.subTest(logo=logo), self.assertRaisesRegex(ManualAgendaError, 'LOGO_INVALID'):
                validate_manual_config(config([manual(homeLogo=logo)]), CATALOG)

    def test_bounded_config_and_diagnostics_never_expose_rejected_values(self):
        with self.assertRaises(ManualAgendaError):
            validate_manual_config(config(events=[manual(id=str(index)) for index in range(MAX_ENTRIES+1)]), CATALOG)
        value = config([manual(homeTeam='SECRET' + 'x'*MAX_CONFIG_BYTES)])
        with self.assertRaises(ManualAgendaError) as error:
            validate_manual_config(value, CATALOG)
        self.assertNotIn('SECRET', str(error.exception))
        value = config([manual(homeTeam='  Argentina Sub-17  ')])
        self.assertEqual('Argentina Sub-17', validate_manual_config(value, CATALOG)['events'][0]['homeTeam'])


if __name__ == '__main__':
    unittest.main()
