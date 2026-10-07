import datetime as dt
import csv
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_collect_new_words as legacy
from test_collect_new_words import collector, candidate, evidence, article, issue, NOW

pipeline = collector.candidate_pipeline


class CategoryPipelineTests(unittest.TestCase):
    setUp = legacy.CollectorTests.setUp
    run_main = legacy.CollectorTests.run_main
    published = legacy.CollectorTests.published

    def test_collection_only_uses_this_project_and_marks_dictionary_unchecked(self):
        self.rss = evidence("新語テスト")
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(all(path.startswith(f"/repos/{collector.REPO}/issues") for _, path, _ in self.github.calls))
        row = self.published()[0]
        self.assertEqual(row["dictionary_check"], {"status": "not_checked"})
        self.assertEqual(row["id"], "")
        self.assertNotIn("dictionary", row)
    def test_uncertain_readings_are_pending_and_not_seen(self):
        self.rss = evidence("UnknownTerm")
        with patch.object(collector.ReadingResolver, "resolve", return_value={"reading": "要確認", "reading_status": "unconfirmed", "reading_estimate": "あんのうんたーむ", "reading_note": "推定"}):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.github.issues, [])
        self.assertEqual(collector.load_seen(), set())
        queue = pipeline.load_pending(collector.PENDING_PATH, collector.normalize)
        self.assertIn("unknownterm", queue)
    def test_pure_hiragana_identical_to_input_is_excluded(self):
        self.rss = evidence("あたらしい")
        self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.github.issues)
    def test_category_overlap_groups_once_and_exports_unchecked_status(self):
        self.rss = evidence("クラウド語彙")
        self.assertEqual(self.run_main(), 0)
        rows = self.published()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "life_food")
        self.assertEqual(len(rows[0]["categories"]), 9)
        body = self.github.issues[0]["body"]
        self.assertIn("## 生活・食", body)
        self.assertIn("分類\t辞書照合", body)
        self.assertIn("辞書照合は未確認", body)
        self.assertIn('"version": 3', body)
    def test_round_robin_uses_multiple_categories_before_second_word(self):
        categories = [{"id": "one"}, {"id": "two"}, {"id": "three"}]
        rows = [{"category": "one", "word": str(i)} for i in range(10)] + [{"category": "two", "word": "B"}, {"category": "three", "word": "C"}]
        self.assertEqual([row["word"] for row in pipeline.round_robin(rows, categories, 4)], ["0", "B", "C", "1"])
    def test_reading_refresh_retains_collection_tsv_columns(self):
        row = {**candidate("読み修正の語彙"), "categories": ["technology"], "category": "technology", "dictionary_check": {"status": "not_checked"}, "id": ""}
        current = issue(1, [row], extra="\n手動メモ: 語義を確認済み")
        body = collector.replace_issue_readings(current, {row["normalized"]: {"reading": "よみしゅうせいのごい", "reading_status": "confirmed", "reading_method": "source"}})
        tsv = body.split("```tsv\n", 1)[1].split("```", 1)[0]
        entry = next(csv.DictReader(io.StringIO(tsv), delimiter="\t"))
        self.assertEqual(entry["辞書照合"], "未確認")
        self.assertEqual(entry["左ID"], "")
        self.assertEqual(entry["分類"], "technology")
        self.assertIn("手動メモ: 語義を確認済み", body)
    def test_pending_is_retried_without_new_feed_articles(self):
        row = {**candidate("再確認の名称"), "categories": ["technology"], "kind": "common"}
        pipeline.save_pending(collector.PENDING_PATH, {collector.normalize(row["word"]): row})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.published()[0]["word"], row["word"])
        self.assertEqual(pipeline.load_pending(collector.PENDING_PATH, collector.normalize), {})
    def test_pending_retry_is_limited_to_ten_terms(self):
        rows = {f"name{i}": {**candidate(f"Name{i}"), "categories": ["technology"], "kind": "common"} for i in range(25)}
        pipeline.save_pending(collector.PENDING_PATH, rows)
        with patch.object(collector.ReadingResolver, "resolve", return_value={"reading": "要確認", "reading_status": "unconfirmed"}) as resolver:
            self.assertEqual(self.run_main(), 0)
            self.assertEqual(resolver.call_count, 10)
    def test_pending_retry_rotates_to_a_new_batch_on_the_next_day(self):
        rows = {f"name{i}": {**candidate(f"Name{i}"), "categories": ["technology"], "kind": "common"} for i in range(25)}
        pipeline.save_pending(collector.PENDING_PATH, rows)
        with patch.object(collector.ReadingResolver, "resolve", return_value={"reading": "要確認", "reading_status": "unconfirmed"}) as resolver:
            self.assertEqual(self.run_main(), 0)
            first = {call.args[0] for call in resolver.call_args_list}
            resolver.reset_mock()
            self.assertEqual(self.run_main(NOW + dt.timedelta(days=1)), 0)
            second = {call.args[0] for call in resolver.call_args_list}
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertTrue(first.isdisjoint(second))
    def test_named_products_without_official_evidence_stay_pending(self):
        self.rss = evidence("クラウドノヴァ")
        for source in self.rss:
            source["title"] = source["source"] + '：新製品「クラウドノヴァ」を発表'
        with patch.object(collector, "require_official_name", return_value=[]):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.github.issues)
        self.assertIn("名称の公式根拠", next(iter(pipeline.load_pending(collector.PENDING_PATH, collector.normalize).values()))["pending_reason"])
    def test_named_definition_cues_cannot_bypass_official_evidence(self):
        for title, word, kind in (
            ('新製品「クラウドノヴァ」とは', "クラウドノヴァ", "product"),
            ('人名「山田音羽」の意味', "山田音羽", "person"),
            ('施設「緑泉ホール」とは', "緑泉ホール", "place"),
            ('名称「Ｃｌｏｕｄ　Ｎｏｖａ」とは', "Cloud Nova", "proper"),
            ('用語「ローカル推論」の意味', "ローカル推論", "common"),
        ):
            with self.subTest(title=title):
                self.assertEqual(pipeline.kind_for(title, word, "common"), kind)
    def test_pending_retains_partial_or_single_source_evidence_and_manual_notes(self):
        word = "再確認する語彙"
        original = evidence(word)
        row = {**candidate(word), "categories": ["technology"], "kind": "common", "manual_notes": "語義を確認する"}
        pipeline.save_pending(collector.PENDING_PATH, {collector.normalize(word): row})
        self.rss = original
        with patch.object(collector, "verify_usage_sources", side_effect=lambda word, sources, readings: sources[:1]):
            self.assertEqual(self.run_main(), 0)
        queued = pipeline.load_pending(collector.PENDING_PATH, collector.normalize)[collector.normalize(word)]
        self.assertEqual(len(queued["sources"]), 2)
        self.assertEqual(queued["manual_notes"], "語義を確認する")
        self.assertIn("technology", queued["categories"])
        self.rss = evidence("単独の語彙", publishers=1)
        self.assertEqual(self.run_main(), 0)
        queued = pipeline.load_pending(collector.PENDING_PATH, collector.normalize)["単独の語彙"]
        self.assertEqual(len(queued["sources"]), 1)
    def test_backlog_uses_reserved_budget_before_new_articles(self):
        word = "保留の名称"
        row = {**candidate(word), "categories": ["technology"], "kind": "common"}
        pipeline.save_pending(collector.PENDING_PATH, {collector.normalize(word): row})
        self.rss = evidence("本日の名称")
        attempts = []
        def verify(word, sources, readings):
            attempts.append((word, readings.page_limit))
            return sources
        with patch.object(collector, "verify_usage_sources", side_effect=verify):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(attempts[0], (word, 10))
        self.assertIn(("本日の名称", 20), attempts)
    def test_urls_and_incomplete_names_are_rejected(self):
        for word in ("https://example.com", "www.example.com", "未来の名称…", "Cloud Nova..."):
            self.assertIsNone(collector.clean_candidate(word))
        self.assertEqual(collector.clean_candidate("Cloud Nova"), "Cloud Nova")
    def test_hidden_mentions_are_not_official_naming_evidence(self):
        word = "クラウドノヴァ"
        sources = [{**evidence(word)[0], "link": "https://sony.jp/product"}]
        with patch.object(collector, "public_reading_document", return_value='<script>クラウドノヴァ</script><p>別の製品の発表</p>'):
            self.assertEqual(pipeline.official_name(word, sources, collector.ReadingResolver(), SimpleNamespace(**vars(collector))), [])
        with patch.object(collector, "public_reading_document", return_value='<h1>クラウドノヴァ</h1><p>公式の製品発表</p>'):
            self.assertTrue(pipeline.official_name(word, sources, collector.ReadingResolver(), SimpleNamespace(**vars(collector))))
    def test_later_reading_conflict_removes_previously_ready_candidate(self):
        word = "読み競合の語彙"
        recent = evidence(word)
        older = article(word, "追加媒体", age=dt.timedelta(days=10))
        def reading(word, sources):
            if len(sources) >= 3:
                return {"reading": "要確認", "reading_status": "conflict", "reading_note": "出典間で読みが異なる"}
            return {"reading": "よみきょうごうのごい", "reading_status": "confirmed", "reading_method": "source"}
        with patch.object(collector, "google_news_rss", side_effect=lambda query: recent if "when:1d" in query else recent + [older]), patch.object(collector.ReadingResolver, "resolve", side_effect=reading):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.github.issues)
        queued = pipeline.load_pending(collector.PENDING_PATH, collector.normalize)[word]
        self.assertEqual(queued["reading_status"], "conflict")
    def test_search_failure_saves_pending_without_publication(self):
        self.rss = evidence("未確定の語彙")
        def feed(query):
            if "when:30d" in query:
                raise RuntimeError("RSS unavailable")
            return self.rss
        with patch.object(collector, "google_news_rss", side_effect=feed), patch.object(collector.ReadingResolver, "resolve", return_value={"reading": "要確認", "reading_status": "unconfirmed"}):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.github.issues)
        self.assertIn("未確定の語彙", pipeline.load_pending(collector.PENDING_PATH, collector.normalize))
    def test_syndicated_headlines_and_agency_reposts_count_once(self):
        context = SimpleNamespace(**vars(collector))
        first, second = evidence("ニュース語")
        second["title"] = first["title"]
        self.assertEqual(pipeline.independent_sources([first, second], context), [])
        second["title"] += "続報"
        first["description"] = second["description"] = "共同通信配信"
        self.assertEqual(pipeline.independent_sources([first, second], context), [])
    def test_identical_body_reposts_and_unavailable_pages_are_not_two_usages(self):
        sources = evidence("語彙テスト")
        for number, source in enumerate(sources):
            source["link"] = f"https://publisher{number}.example/article"
        resolver = collector.ReadingResolver()
        markup = '<p>語彙テスト（ゴイテスト）の日本語使用例です。</p>'
        with patch.object(collector, "public_reading_document", return_value=markup):
            result = pipeline.verified_usage("語彙テスト", sources, resolver, SimpleNamespace(**vars(collector)))
        self.assertEqual(len(result), 1)
        with patch.object(collector, "public_reading_document", side_effect=ValueError("unavailable")):
            self.assertEqual(pipeline.verified_usage("別語", evidence("別語"), collector.ReadingResolver(), SimpleNamespace(**vars(collector))), [])
    def test_agency_credit_in_different_page_bodies_counts_once(self):
        sources = evidence("語彙テスト")
        for number, source in enumerate(sources):
            source["link"] = f"https://publisher{number}.example/article"
        def page(link):
            return '<p>語彙テストを紹介する日本語記事。共同通信配信。</p><footer>' + link + '</footer>'
        with patch.object(collector, "public_reading_document", side_effect=page):
            result = pipeline.verified_usage("語彙テスト", sources, collector.ReadingResolver(), SimpleNamespace(**vars(collector)))
        self.assertEqual(len(result), 1)
    def test_page_budget_is_shared_by_usage_and_reading_verification(self):
        resolver = collector.ReadingResolver(page_limit=1)
        with patch.object(collector, "public_reading_document", return_value='<p>語彙テスト（ゴイテスト）を解説します。</p>') as fetch:
            self.assertEqual(len(pipeline.verified_usage("語彙テスト", evidence("語彙テスト"), resolver, SimpleNamespace(**vars(collector)))), 1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(resolver.page_count, 1)
