"""Offline proof-cache, exact identity, shared budget and owned-TV regressions."""
import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from scripts import official_football_bot as bot, prioritized_agendas as guides
from scripts.argentina_tv_sources import TYC_AGENDA_URL
from scripts.broadcaster_sources import Listing, ZONE
from scripts.sports_channel_rules import load_clean_groups
from scripts.tyc_fixture_tv_discovery import MAX_CACHE_BYTES, enrich_fixture_tv


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 14, tzinfo=ZONE)
URL = ('https://www.tycsports.com/amistosos-de-selecciones/'
       'amistosos-internacionales-masc-2026-argentina-vs-costa-rica-amistosos-id771718.html')
ARTICLE = ROOT.joinpath('tests/fixtures/tyc_fixture_tv_argentina_costa_rica_2026_11_17.html').read_text(encoding='utf8')
AGENDA = ROOT.joinpath('tests/fixtures/tyc_agenda_fixture_link_2026_11_17.html').read_text(encoding='utf8')


class TycFixtureTvDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(ROOT.joinpath('sports_agenda_guides.json').read_text(encoding='utf8'))
        cls.mapping = json.loads(ROOT.joinpath('sports_broadcaster_mapping.json').read_text(encoding='utf8'))
        cls.groups = load_clean_groups(ROOT / 'mbox_sports_channels_clean.json', bot.generator.normalize)

    def records(self, home='Argentina', away='Costa Rica', competition='Amistosos de selecciones',
                signals=('Telefe', 'TyC Sports'), provider=1, kickoff=None):
        item = Listing(home, away, kickoff or NOW.replace(hour=20), competition, signals)
        return guides.checked_records([item], guides.PROVIDERS[provider], DAY, self.config)

    def pages(self, *, article=ARTICLE, agenda=AGENDA, existing_agenda=True, spent=3, maximum=6):
        network = Mock()
        def fetch(url):
            value = agenda if url == TYC_AGENDA_URL else article
            if isinstance(value, Exception):
                raise value
            return value
        network.fetch.side_effect = fetch
        pages = bot.PublicPages(bot.RequestBudget(network, maximum))
        pages.budget.requests = spent
        if existing_agenda:
            pages.pages[TYC_AGENDA_URL] = agenda
        return pages, network

    def enrich(self, records=None, pages=None, **kwargs):
        if pages is None:
            pages, _ = self.pages()
        kwargs.setdefault('groups', self.groups)
        kwargs.setdefault('now', NOW)
        return enrich_fixture_tv(self.records() if records is None else records, DAY, pages,
                                  self.config, self.mapping, **kwargs)

    def proof(self, article=ARTICLE):
        _, cache, _ = self.enrich(pages=self.pages(article=article)[0])
        return cache

    def test_one_owned_article_enriches_without_mutating_original_or_inventing_telefe(self):
        records = self.records()
        original = copy.deepcopy(records)
        pages, network = self.pages()
        result, cache, report = self.enrich(records, pages)
        self.assertEqual(original, records)
        self.assertEqual(original, result[:-1])
        self.assertEqual(['TyC Sports'], result[-1]['broadcasters'])
        self.assertEqual('tyc-agenda', result[-1]['guideId'])
        self.assertEqual(TYC_AGENDA_URL, result[-1]['sourceUrl'])
        self.assertEqual(URL, result[-1]['ownedTvUrl'])
        self.assertNotIn('channels', result[-1])
        self.assertEqual(1, network.fetch.call_count)
        self.assertEqual(URL, network.fetch.call_args.args[0])
        self.assertEqual((1, 1, 1, 'OK'), (report['requests'], report['articleRequests'], report['fixtures'], report['state']))
        self.assertEqual(4, pages.budget.requests)
        self.assertLessEqual(len(json.dumps(cache, ensure_ascii=False).encode('utf8')), MAX_CACHE_BYTES)
        self.assertEqual(ARTICLE, cache['entry']['articleHtml'])
        self.assertNotIn('broadcasters', cache['entry'])

    def test_enriched_evidence_keeps_actual_article_url_and_existing_owner(self):
        result, _, _ = self.enrich()
        evidence = guides.evidence_for(result[0], 1, result, self.config)
        own = [item for item in evidence if item.url == URL]
        self.assertEqual(1, len(own))
        self.assertEqual(('TyC Sports', 'ole-tyc-conservative', True),
                         (own[0].broadcaster, own[0].owner, own[0].official))

    def test_agenda_not_refetched_and_article_already_in_pages_costs_zero(self):
        pages, network = self.pages(spent=6)
        pages.pages[URL] = ARTICLE
        result, _, report = self.enrich(pages=pages)
        self.assertEqual('OK', report['state'])
        self.assertEqual(0, report['requests'])
        self.assertEqual(['TyC Sports'], result[-1]['broadcasters'])
        network.fetch.assert_not_called()

    def test_missing_agenda_consumes_existing_budget_for_agenda_then_one_article(self):
        pages, network = self.pages(existing_agenda=False, spent=4)
        _, _, report = self.enrich(pages=pages)
        self.assertEqual([TYC_AGENDA_URL, URL], [call.args[0] for call in network.fetch.call_args_list])
        self.assertEqual((2, 1, 6), (report['requests'], report['articleRequests'], pages.budget.requests))

    def test_exhausted_or_insufficient_shared_budget_never_increased(self):
        for existing, spent in ((True, 6), (False, 5), (False, 6)):
            with self.subTest(existing=existing, spent=spent):
                pages, network = self.pages(existing_agenda=existing, spent=spent)
                result, _, report = self.enrich(pages=pages)
                self.assertEqual(self.records(), result)
                self.assertEqual('DEFERRED_BUDGET', report['state'])
                self.assertEqual(spent, pages.budget.requests)
                self.assertEqual(6, pages.budget.maximum)
                network.fetch.assert_not_called()

    def test_shared_pages_allow_only_one_article_attempt_even_across_calls(self):
        pages, network = self.pages()
        self.enrich(pages=pages)
        _, _, report = self.enrich(pages=pages)
        self.assertEqual('DEFERRED_ARTICLE_LIMIT', report['state'])
        self.assertEqual(1, network.fetch.call_count)

    def test_no_target_no_agenda_request_and_resolved_fixture_is_skipped(self):
        for records in ([], self.records(competition='UEFA Nations League'),
                        self.records(provider=2, signals=('TyC Sports',)),
                        self.records(home='Chile', away='Colombia', signals=('DSports',), provider=0)
                        + self.records(home='Chile', away='Colombia', signals=('DSports',), provider=1)):
            with self.subTest(records=records):
                pages, network = self.pages(existing_agenda=False)
                result, _, report = self.enrich(records, pages)
                self.assertEqual(records, result)
                self.assertIn(report['state'], ('SKIP_NO_TARGETS', 'SKIP_RESOLVED'))
                network.fetch.assert_not_called()

    def test_youth_and_other_fixture_day_do_not_trigger_lookup(self):
        records = self.records()
        for updates in ({'homeTeam': 'Argentina Sub-17'}, {'awayTeam': 'Costa Rica Femenino'},
                        {'startTime': '2026-11-18T20:00:00-03:00'}):
            with self.subTest(updates=updates):
                changed = [{**record, **updates} for record in records]
                pages, network = self.pages(existing_agenda=False)
                result, _, report = self.enrich(changed, pages)
                self.assertEqual(changed, result)
                self.assertEqual('SKIP_NO_TARGETS', report['state'])
                network.fetch.assert_not_called()

    def test_different_target_day_missing_groups_or_larger_budget_fail_closed(self):
        for kwargs, expected in (({'now': NOW + timedelta(days=1)}, 'SKIP_NOT_CURRENT_DAY'),
                                 ({'groups': None}, 'SKIP_MISSING_GROUPS')):
            with self.subTest(kwargs=kwargs):
                pages, network = self.pages()
                self.assertEqual(expected, self.enrich(pages=pages, **kwargs)[2]['state'])
                network.fetch.assert_not_called()
        pages, network = self.pages(maximum=7)
        self.assertEqual('SKIP_INVALID_BUDGET', self.enrich(pages=pages)[2]['state'])
        network.fetch.assert_not_called()

    def test_argentina_prioritized_over_an_earlier_unresolved_friendly(self):
        chile = self.records(home='Chile', away='Colombia', signals=(), kickoff=NOW.replace(hour=19))
        article_url = URL.replace('argentina-vs-costa-rica', 'chile-vs-colombia').replace('id771718', 'id771719')
        chile_agenda = AGENDA.replace('alt="Argentina"', 'alt="Chile"').replace('alt="Costa Rica"', 'alt="Colombia"')
        chile_agenda = chile_agenda.replace('20:00', '19:00').replace(URL[6:], article_url[6:])
        pages, network = self.pages(agenda=chile_agenda + AGENDA)
        result, _, _ = self.enrich(chile + self.records(), pages)
        self.assertEqual(URL, network.fetch.call_args.args[0])
        self.assertEqual('Argentina', result[-1]['homeTeam'])

    def test_missing_exact_article_link_never_falls_back_to_search_or_other_match(self):
        for agenda in (AGENDA.replace('alt="Argentina"', 'alt="Uruguay"'),
                       AGENDA.replace('20:00', '21:00'),
                       AGENDA.replace('17 de Noviembre', '18 de Noviembre')):
            with self.subTest(agenda=agenda):
                pages, network = self.pages(agenda=agenda)
                result, cache, report = self.enrich(pages=pages)
                self.assertEqual(self.records(), result)
                self.assertIsNone(cache['entry'])
                self.assertEqual('NO_EXACT_ARTICLE_LINK', report['state'])
                network.fetch.assert_not_called()

    def test_wrong_article_identity_or_body_never_confirms_anything(self):
        for article in (ARTICLE.replace('Selección Argentina vs. Costa Rica', 'Selección Uruguay vs. Costa Rica'),
                        ARTICLE.replace('Horario: 20.00', 'Horario: 19.00'),
                        ARTICLE.replace('2026-11-17T07:43:53-03:00', '2026-11-16T07:43:53-03:00')):
            with self.subTest(article=article[:100]):
                pages, network = self.pages(article=article)
                result, cache, report = self.enrich(pages=pages)
                self.assertEqual(self.records(), result)
                self.assertIsNone(cache['entry'])
                self.assertEqual('ARTICLE_UNAVAILABLE', report['state'])
                self.assertEqual(1, network.fetch.call_count)

    def test_network_failure_sanitized_and_not_retried(self):
        pages, network = self.pages(article=TimeoutError('secret-response-token'))
        result, cache, report = self.enrich(pages=pages)
        self.assertEqual(self.records(), result)
        self.assertIsNone(cache['entry'])
        self.assertEqual('ARTICLE_UNAVAILABLE', report['state'])
        self.assertEqual('TimeoutError', report['reason'])
        self.assertNotIn('secret-response-token', json.dumps(report))
        self.enrich(pages=pages)
        self.assertEqual(1, network.fetch.call_count)

    def test_fresh_raw_proof_is_revalidated_without_requests_or_ttl_extension(self):
        previous = self.proof()
        original = copy.deepcopy(previous)
        pages, network = self.pages(existing_agenda=False)
        result, cache, report = self.enrich(pages=pages, previous_cache=previous, now=NOW + timedelta(hours=1), offline=True)
        self.assertEqual('CACHE_HIT', report['state'])
        self.assertEqual(URL, result[-1]['ownedTvUrl'])
        self.assertEqual(original, previous)
        self.assertEqual(original['entry']['fetchedAt'], cache['entry']['fetchedAt'])
        self.assertEqual(0, report['requests'])
        network.fetch.assert_not_called()

    def test_expired_future_wrong_day_or_modified_raw_cache_is_not_trusted(self):
        proof = self.proof()
        cases = []
        for field, value in (('fetchedAt', (NOW - timedelta(hours=2)).isoformat()),
                             ('fetchedAt', (NOW + timedelta(minutes=1)).isoformat()),
                             ('sourceUrl', URL + '?token=unsafe'),
                             ('fixtureKey', ['Amistosos de selecciones', 'uruguay', 'costa rica', NOW.replace(hour=20).isoformat()]),
                             ('articleHtml', ARTICLE.replace('Horario: 20.00', 'Horario: 19.00'))):
            modified = copy.deepcopy(proof)
            modified['entry'][field] = value
            cases.append(modified)
        cases.append({**proof, 'date': '2026-11-16'})
        cases.append({**proof, 'unexpected': 'never trusted'})
        for previous in cases:
            with self.subTest(previous=str(previous)[:100]):
                pages, network = self.pages(existing_agenda=False)
                result, cache, report = self.enrich(pages=pages, previous_cache=previous, offline=True)
                self.assertEqual(self.records(), result)
                self.assertIsNone(cache['entry'])
                self.assertEqual('OFFLINE_MISS', report['state'])
                network.fetch.assert_not_called()

    def test_newly_fetched_agenda_supersedes_cached_agenda_even_when_it_invalidates_proof(self):
        previous = self.proof()
        pages, network = self.pages(agenda=AGENDA.replace('20:00', '21:00'))
        result, cache, report = self.enrich(pages=pages, previous_cache=previous, offline=True)
        self.assertEqual(self.records(), result)
        self.assertIsNone(cache['entry'])
        self.assertEqual('TYC_FIXTURE_TV_DATED_AGENDA_MISMATCH', report['cacheRejectedReason'])
        network.fetch.assert_not_called()

    def test_explicit_non_owned_tv_is_cached_but_never_promoted_to_tyc_or_telefe(self):
        pages, network = self.pages(article=ARTICLE.replace('TV: TyC Sports', 'TV: Telefe'))
        result, cache, report = self.enrich(pages=pages)
        self.assertEqual(self.records(), result)
        self.assertEqual('NO_OWNED_TV', report['state'])
        self.assertIsNotNone(cache['entry'])
        pages, network = self.pages(existing_agenda=False)
        result, _, report = self.enrich(pages=pages, previous_cache=cache, offline=True)
        self.assertEqual('CACHE_NO_OWNED_TV', report['state'])
        self.assertEqual(self.records(), result)
        network.fetch.assert_not_called()

    def test_proof_size_is_bounded_and_no_stale_cache_claim_is_used_on_failure(self):
        pages, network = self.pages(article=ARTICLE + '<!--' + 'x' * MAX_CACHE_BYTES + '-->')
        result, cache, report = self.enrich(pages=pages)
        self.assertEqual(self.records(), result)
        self.assertIsNone(cache['entry'])
        self.assertEqual('TYC_FIXTURE_TV_CACHE_SIZE_LIMIT', report['reason'])
        previous = self.proof()
        previous['entry']['articleHtml'] += 'x' * MAX_CACHE_BYTES
        pages, network = self.pages(existing_agenda=False)
        result, cache, report = self.enrich(pages=pages, previous_cache=previous, offline=True)
        self.assertEqual(self.records(), result)
        self.assertIsNone(cache['entry'])
        network.fetch.assert_not_called()

    def test_offline_miss_and_failed_agenda_never_trigger_retry(self):
        pages, network = self.pages(existing_agenda=False)
        self.assertEqual('OFFLINE_MISS', self.enrich(pages=pages, offline=True)[2]['state'])
        pages.failed.add(TYC_AGENDA_URL)
        self.assertEqual('AGENDA_UNAVAILABLE', self.enrich(pages=pages)[2]['state'])
        network.fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
