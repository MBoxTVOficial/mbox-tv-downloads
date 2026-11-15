"""Country identity from the official page's profile/crest IDs, not club-name guesses.

The minimal team-directory shape was verified on CONMEBOL Libertadores on
2026-11-15. Test fixture names are deliberately ambiguous; no test is a real
schedule or a country/TV assignment rule.
"""
import copy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from scripts import conmebol_official_source as source
from scripts import official_football_bot as bot
from scripts.broadcaster_sources import ZONE
from scripts.sports_channel_rules import load_clean_groups


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 28)
NOW = datetime(2026, 11, 28, 12, tzinfo=ZONE)
URL = source.SOURCE_URLS['CONMEBOL Libertadores']


def crest(identifier, host='gol-cdn.conmebol.com'):
    return f'<img src="https://{host}/icons/team/light/1x/id/{identifier}.png?version=2026040801">'


def team(identifier, name, country, slug='libertadores', badge=None, host='gol-cdn.conmebol.com'):
    return (f'<article class="m-team-teaser"><a class="m-team-teaser__link" '
            f'href="/{slug}/es/team/{identifier}">View team</a>'
            f'<div class="m-team-teaser__title">{name}</div>'
            f'<div class="m-team-teaser__country">{country}</div>'
            f'{crest(identifier if badge is None else badge, host)}</article>')


def card(identifier=100, home='CA River Plate', away='SE Palmeiras', home_badge='1', away_badge='21',
         slug='libertadores', home_host='gol-cdn.conmebol.com'):
    return (f'<div class="m-fixture-teaser-card"><a class="m-fixture-teaser-card__match-centre-link" '
            f'href="https://gol.conmebol.com/{slug}/es/fixture/view/{identifier}">{home}vs{away}</a>'
            '<div class="m-fixture-teaser-card__date"><span data-timestamp="1795896000">17:00</span></div>'
            f'<div class="m-fixture-teaser-card__team-badge-home">{crest(home_badge, home_host)}</div>'
            f'<div class="m-fixture-teaser-card__team-badge-away">{crest(away_badge)}</div></div>')


def page(cards=None, teams=None):
    cards = card() if cards is None else cards
    teams = team('1', 'River Plate', 'Argentina') + team('21', 'Palmeiras', 'Brasil') if teams is None else teams
    return ('<html><body><div class="m-tournament-strip paragraph--type--tournament-fixtures-results">'
            f'<div class="m-tournament-strip__slider">{cards}</div></div>{teams}</body></html>')


class ConmebolCountryMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / bot.REGISTRY).read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)
        cls.mapping = json.loads((ROOT / 'sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        cls.spec = next(item for item in cls.config['sources'] if item['id'] == 'libertadores')

    def setUp(self):
        guard = patch.object(socket, 'create_connection', side_effect=AssertionError('Offline tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def parse(self, html=None):
        return source.parse_conmebol_fixtures(page() if html is None else html, 'CONMEBOL Libertadores', URL)

    def preview(self, rows):
        return bot.preview(rows, DAY, NOW, self.config, self.groups, self.mapping)

    def event(self, feed):
        return next(event for section in feed['sections'] for event in section['events'])

    def test_ambiguous_names_are_resolved_by_their_distinct_official_crest_ids(self):
        teams = team('1', 'River Plate', 'Argentina') + team('2', 'River Plate', 'Uruguay') + team('21', 'Palmeiras', 'Brasil')
        rows = self.parse(page(card(100, home_badge='1') + card(101, home_badge='2'), teams))
        self.assertEqual(['AR', 'UY'], [row['homeCountry'] for row in rows])
        self.assertEqual(['BR', 'BR'], [row['awayCountry'] for row in rows])
        self.assertEqual(['CA River Plate', 'CA River Plate'], [row['homeTeam'] for row in rows])

    def test_a_country_cannot_be_inferred_from_a_name_or_unregistered_badge(self):
        rows = self.parse(page(card(home='CA Boca Juniors', home_badge='53')))
        self.assertNotIn('homeCountry', rows[0])
        self.assertEqual('BR', rows[0]['awayCountry'])

    def test_legacy_fixture_without_team_directory_retains_old_shape(self):
        row = self.parse(page(teams=''))[0]
        self.assertNotIn('homeCountry', row)
        self.assertNotIn('awayCountry', row)
        fixture = (ROOT / 'tests/fixtures/official_conmebol_libertadores.html').read_text(encoding='utf8')
        self.assertTrue(all('homeCountry' not in item and 'awayCountry' not in item for item in self.parse(fixture)))

    def test_conflicting_directory_country_is_quarantined_without_losing_fixture(self):
        teams = team('1', 'River Plate', 'Argentina') + team('1', 'River Plate', 'Uruguay') + \
                team('1', 'River Plate', 'Argentina') + team('21', 'Palmeiras', 'Brasil')
        row = self.parse(page(teams=teams))[0]
        self.assertNotIn('homeCountry', row)
        self.assertEqual('BR', row['awayCountry'])

    def test_duplicate_fixture_country_conflict_is_sticky_and_does_not_erase_other_country(self):
        teams = team('1', 'River Plate', 'Argentina') + team('2', 'River Plate', 'Uruguay') + team('21', 'Palmeiras', 'Brasil')
        cards = card(home_badge='1') + card(home_badge='2') + card(home_badge='1')
        rows = self.parse(page(cards, teams))
        self.assertEqual(1, len(rows))
        self.assertNotIn('homeCountry', rows[0])
        self.assertEqual('BR', rows[0]['awayCountry'])

    def test_untrusted_badge_host_or_profile_badge_disagreement_cannot_supply_country(self):
        for html in (page(card(home_host='untrusted.invalid')),
                     page(teams=team('1', 'River Plate', 'Argentina', badge='2'))):
            with self.subTest(html=html[:50]):
                self.assertNotIn('homeCountry', self.parse(html)[0])

    def test_same_template_supports_sudamericana_without_cross_competition_profile_lookup(self):
        html = page(card(home='CA Boca Juniors', home_badge='53', slug='sudamericana'),
                    team('53', 'Boca Juniors', 'Argentina', slug='sudamericana'))
        rows = source.parse_conmebol_fixtures(html, 'CONMEBOL Sudamericana', source.SOURCE_URLS['CONMEBOL Sudamericana'])
        self.assertEqual('AR', rows[0]['homeCountry'])
        self.assertNotIn('awayCountry', rows[0])
        wrong = page(card(home='CA Boca Juniors', home_badge='53', slug='sudamericana'),
                     team('53', 'Boca Juniors', 'Argentina'))
        self.assertNotIn('homeCountry', source.parse_conmebol_fixtures(
            wrong, 'CONMEBOL Sudamericana', source.SOURCE_URLS['CONMEBOL Sudamericana'])[0])

    def test_checked_cache_rows_preserve_verified_codes(self):
        row = self.parse()[0]
        checked = bot.checked_rows([row], self.spec, DAY)
        self.assertEqual(('AR', 'BR'), (checked[0]['homeCountry'], checked[0]['awayCountry']))
        cache = {'schemaVersion': 1, 'date': DAY.isoformat(),
                 'registryHash': hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest(),
                 'sources': {'libertadores': {'fetchedAt': NOW.isoformat(), 'rows': checked}}}
        rows, _, report = bot.acquire(self.config, DAY, NOW, cache, offline=True)
        self.assertEqual(('AR', 'BR'), (rows[0]['homeCountry'], rows[0]['awayCountry']))
        self.assertEqual(0, report['sportsApiRequests'])

    def test_checked_duplicate_country_conflicts_stay_omitted(self):
        row = self.parse()[0]
        changed = {**row, 'homeCountry': 'UY'}
        checked = bot.checked_rows([row, changed, row], self.spec, DAY)
        self.assertNotIn('homeCountry', checked[0])
        self.assertEqual('BR', checked[0]['awayCountry'])

    def test_invalid_country_metadata_cannot_replace_valid_fixture_data(self):
        row = self.parse()[0]
        for value in ('ES', 'Argentina', 'ar', [], None):
            with self.subTest(value=value):
                checked = bot.checked_rows([{**row, 'homeCountry': value}], self.spec, DAY)
                self.assertNotIn('homeCountry', checked[0])
                self.assertEqual(row['homeTeam'], checked[0]['homeTeam'])

    def test_country_is_not_accepted_from_other_source_types(self):
        raw = {**self.parse()[0], 'competition': 'Liga Profesional Argentina', 'competitionId': 'lpf-primera',
               'source': 'lpf_official', 'sourceUrl': 'https://www.ligaprofesional.ar/categoria/primera/'}
        spec = next(item for item in self.config['sources'] if item['id'] == 'lpf-primera')
        self.assertNotIn('homeCountry', bot.checked_rows([raw], spec, DAY)[0])

    def test_preview_preserves_optional_country_without_changing_identity_channels_or_schema(self):
        rows = self.parse()
        feed, broadcasts, details, identity = self.preview(rows)
        event = self.event(feed)
        without = [{key: value for key, value in row.items() if key not in ('homeCountry', 'awayCountry')} for row in rows]
        legacy, _, _, _ = bot.preview(without, DAY, NOW, self.config, self.groups, self.mapping, identity=identity)
        self.assertEqual('AR', event['homeCountry'])
        self.assertEqual('BR', event['awayCountry'])
        self.assertEqual(self.event(legacy), {key: value for key, value in event.items()
                                             if key not in ('homeCountry', 'awayCountry')})
        self.assertEqual(1, feed['schemaVersion'])
        self.assertEqual([], broadcasts['broadcasts'])
        self.assertNotIn('channels', event)
        bot.generator.validate_feed(feed)

    def test_feed_validator_accepts_legacy_schema_and_rejects_invalid_country_fields(self):
        feed = self.preview(self.parse())[0]
        for value in ('ES', 'Argentina', 'ar', None, [], True):
            with self.subTest(value=value):
                invalid = copy.deepcopy(feed)
                self.event(invalid)['homeCountry'] = value
                with self.assertRaises(bot.generator.GenerationError):
                    bot.generator.validate_feed(invalid)
        legacy = copy.deepcopy(feed)
        self.event(legacy).pop('homeCountry')
        self.event(legacy).pop('awayCountry')
        bot.generator.validate_feed(legacy)


if __name__ == '__main__':
    unittest.main()
