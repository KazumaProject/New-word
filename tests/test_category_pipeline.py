import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

from release_support import NewsFixture, collector, data, release, candidate, evidence, article, NOW

pipeline = collector.candidate_pipeline


class CategoryPipelineTests(NewsFixture):
    def test_release_collection_has_no_issue_api(self):
        import test_collect_new_words as legacy
        self.rss = evidence("新語テスト")
        with patch.object(legacy.collector, "github_api", side_effect=AssertionError("Issue/dictionary API")):
            self.assertEqual(self.run_main(), 0)
        row = self.published()[0]
        self.assertEqual(row["dictionary_check"], self.dictionary.check(row["word"], NOW.date().isoformat()))
        self.assertNotIn("id", row)
        self.assertNotIn("dictionary", row)

    def test_registered_spelling_is_excluded_before_article_fetching(self):
        self.dictionary.words.add(collector.normalize("Cloud Nova"))
        self.rss = evidence("ＣＬＯＵＤ　ＮＯＶＡ")
        with patch.object(collector, "verify_usage_sources", side_effect=AssertionError("Already registered")):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.published())
        self.assertFalse(self.pending())

    def test_registered_pending_term_preserves_notes_without_consuming_retry_budget(self):
        row = {**candidate("既存の名称"), "manual_notes": "保存しておくメモ"}
        self.queue({row["normalized"]: row})
        self.dictionary.words.add(row["normalized"])
        with patch.object(collector, "verify_usage_sources", side_effect=AssertionError("Already registered")):
            self.assertEqual(self.run_main(), 0)
        saved = self.pending()[row["normalized"]]
        self.assertEqual(saved["manual_notes"], row["manual_notes"])
        self.assertEqual(saved["dictionary_check"]["status"], "present")
        self.assertEqual(json.loads(self.pending_path.read_text())["retry_state"]["terms"], [])

    def test_rebuild_migrates_unchecked_rows_and_preserves_dates_notes_and_categories(self):
        row = {**self.seed[0], "dictionary_check": {"status": "not_checked"}, "manual_notes": "手動メモ"}
        data.save_entries([row], self.directory)
        with patch.object(collector, "google_news_rss", side_effect=AssertionError("No news in rebuild")):
            self.assertEqual(self.run_main(mode="rebuild"), 0)
        saved = data.load_entries(self.directory)[0]
        self.assertEqual(saved, {**row, "dictionary_check": self.dictionary.check(row["word"], NOW.date().isoformat())})

    def test_unavailable_dictionary_stops_even_rebuild_without_writing_data(self):
        before = {path.name: path.read_bytes() for path in self.directory.glob("*.json*")}
        with patch.object(release.dictionaries.DictionaryIndex, "from_environment", side_effect=ValueError("corrupt index")):
            self.assertEqual(self.run_main(mode="rebuild"), 1)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.glob("*.json*")})

    def test_saved_registered_words_keep_manual_notes_and_are_marked_for_exclusion(self):
        row = {**self.seed[0], "manual_notes": "確認した情報"}
        data.save_entries([row], self.directory)
        self.dictionary.words.add(row["normalized"])
        self.assertEqual(self.run_main(mode="rebuild"), 0)
        saved = data.load_entries(self.directory)[0]
        self.assertEqual(saved["dictionary_check"]["status"], "present")
        self.assertEqual(saved["manual_notes"], row["manual_notes"])

    def test_missing_index_stops_collection_before_news_or_data_changes(self):
        before = {path.name: path.read_bytes() for path in self.directory.glob("*.json*")}
        with patch.object(release.dictionaries.DictionaryIndex, "from_environment", side_effect=ValueError("missing index")), \
                patch.object(collector, "google_news_rss", side_effect=AssertionError("No search without verification")):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.glob("*.json*")})

    def test_uncertain_and_estimated_readings_remain_pending(self):
        self.rss = evidence("UnknownTerm")
        with patch.object(collector.ReadingResolver, "resolve", return_value={
                "reading": "要確認", "reading_status": "unconfirmed", "reading_estimate": "あんのうんたーむ"}):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.published())
        self.assertIn("unknownterm", self.pending())

    def test_pure_hiragana_identical_to_input_is_excluded(self):
        self.rss = evidence("あたらしい")
        self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.published())

    def test_overlap_retains_all_tags_and_has_one_primary_category(self):
        self.rss = evidence("クラウド語彙")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.published()), 1)
        self.assertEqual(self.published()[0]["category"], "life_food")
        self.assertEqual(len(self.published()[0]["categories"]), 9)

    def test_round_robin_selects_across_categories(self):
        categories = [{"id": "one"}, {"id": "two"}, {"id": "three"}]
        rows = [{"category": "one", "word": str(i)} for i in range(10)] + [{"category": "two", "word": "B"}, {"category": "three", "word": "C"}]
        self.assertEqual([row["word"] for row in pipeline.round_robin(rows, categories, 4)], ["0", "B", "C", "1"])

    def test_pending_is_retried_without_feed_articles_and_keeps_notes(self):
        row = {**candidate("再確認の名称"), "manual_notes": "語義を確認済み"}
        self.queue({row["normalized"]: row})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.published()[0]["manual_notes"], row["manual_notes"])
        self.assertFalse(self.pending())

    def test_pending_daily_budget_survives_reruns_and_feed_discovery(self):
        rows = {f"name{i}": candidate(f"Name{i}") for i in range(25)}
        self.queue(rows)
        with patch.object(collector.ReadingResolver, "resolve", return_value={"reading": "要確認", "reading_status": "unconfirmed"}) as resolver:
            self.assertEqual(self.run_main(), 0)
            first = {call.args[0] for call in resolver.call_args_list}
            self.assertEqual(len(first), 10)
            resolver.reset_mock()
            self.rss = [source for row in rows.values() for source in row["sources"]]
            self.assertEqual(self.run_main(), 0)
            self.assertEqual(resolver.call_count, 0)
            self.rss = []
            self.assertEqual(self.run_main(NOW + dt.timedelta(days=1)), 0)
            second = {call.args[0] for call in resolver.call_args_list}
        self.assertEqual(len(second), 10)
        self.assertTrue(first.isdisjoint(second))

    def test_daily_adoption_limit_survives_reruns_and_rotates_next_day(self):
        self.rss = [source for i in range(14) for source in evidence(f"新語テスト{i:02}")]
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.published()), 10)
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.published()), 10)
        self.rss = []
        self.assertEqual(self.run_main(NOW + dt.timedelta(days=1)), 0)
        self.assertEqual(len(self.published()), 14)

    def test_normalized_variants_do_not_overwrite_confirmed_data(self):
        saved = candidate("Cloud Nova")
        saved["manual_notes"] = "人手で確認したメモ"
        data.save_entries(self.seed + [saved], self.directory)
        self.rss = evidence("Ｃｌｏｕｄ　Ｎｏｖａ") + evidence("CLOUD NOVA")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(data.load_entries(self.directory), self.seed + [saved])

    def test_rebuild_does_not_search_or_retry_or_change_any_data(self):
        before = {path.name: path.read_bytes() for path in self.directory.glob("*.json*")}
        with patch.object(collector, "google_news_rss", side_effect=AssertionError("No search in rebuild")):
            self.assertEqual(self.run_main(mode="rebuild"), 0)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.glob("*.json*")})

    def test_missing_or_corrupt_data_never_resets_to_seed(self):
        path = self.directory / "entries-0001.jsonl"
        path.write_text("corrupt", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(path.read_text(), "corrupt")
        path.unlink()
        self.assertEqual(self.run_main(), 1)
        self.assertFalse(path.exists())

    def test_corrupt_daily_retry_history_is_not_overwritten(self):
        self.pending_path.write_text('{"version":1,"rows":[]}', encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(json.loads(self.pending_path.read_text()), {"version": 1, "rows": []})

    def test_named_products_without_official_evidence_stay_pending(self):
        self.rss = evidence("クラウドノヴァ")
        for source in self.rss:
            source["title"] = source["source"] + '：新製品「クラウドノヴァ」を発表'
        with patch.object(collector, "require_official_name", return_value=[]):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.published())
        self.assertIn("名称の公式根拠", next(iter(self.pending().values()))["pending_reason"])

    def test_named_definition_cues_cannot_bypass_official_evidence(self):
        for title, word, kind in (
            ('新製品「クラウドノヴァ」とは', "クラウドノヴァ", "product"),
            ('人名「山田音羽」の意味', "山田音羽", "person"),
            ('施設「緑泉ホール」とは', "緑泉ホール", "place"),
            ('名称「Ｃｌｏｕｄ　Ｎｏｖａ」とは', "Cloud Nova", "proper"),
            ('用語「ローカル推論」の意味', "ローカル推論", "common"),
        ):
            self.assertEqual(pipeline.kind_for(title, word, "common"), kind)

    def test_pending_preserves_partial_evidence_and_manual_notes(self):
        row = {**candidate("再確認する語彙"), "manual_notes": "語義を確認する"}
        self.queue({row["normalized"]: row})
        self.rss = evidence(row["word"])
        with patch.object(collector, "verify_usage_sources", side_effect=lambda word, sources, readings: sources[:1]):
            self.assertEqual(self.run_main(), 0)
        queued = self.pending()[row["normalized"]]
        self.assertEqual(len(queued["sources"]), 2)
        self.assertEqual(queued["manual_notes"], row["manual_notes"])
        self.assertIn("technology", queued["categories"])

    def test_backlog_uses_reserved_budget_before_new_articles(self):
        row = candidate("保留の名称")
        self.queue({row["normalized"]: row})
        self.rss = evidence("本日の名称")
        attempts = []
        def verify(word, sources, readings):
            attempts.append((word, readings.page_limit))
            return sources
        with patch.object(collector, "verify_usage_sources", side_effect=verify):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(attempts[0], (row["word"], 10))
        self.assertIn(("本日の名称", 20), attempts)

    def test_invalid_phrases_urls_and_incomplete_names_are_rejected(self):
        for word in ("https://example.com", "www.example.com", "未来の名称…", "Cloud Nova...",
                     "普通のメガネに見えるスマートグラス", "未来を創る", "回避のすべはない"):
            self.assertIsNone(collector.clean_candidate(word))

    def test_hidden_mentions_are_not_official_evidence(self):
        word = "クラウドノヴァ"
        sources = [{"source": "Sony", "link": "https://sony.jp/name", "title": "発表"}]
        with patch.object(collector, "public_reading_document", return_value='<script>クラウドノヴァ</script><p>別の製品</p>'):
            self.assertEqual(pipeline.official_name(word, sources, collector.ReadingResolver(), collector), [])

    def test_later_source_conflict_removes_previously_ready_candidate(self):
        word = "読み競合の語彙"
        recent = evidence(word)
        older = article(word, "media3", age=dt.timedelta(days=10))
        def reading(word, sources):
            if len(sources) >= 3:
                return {"reading": "要確認", "reading_status": "conflict"}
            return {"reading": "よみきょうごうのごい", "reading_status": "confirmed", "reading_method": "source", "reading_sources": sources}
        with patch.object(collector, "google_news_rss", side_effect=lambda query: recent if "when:1d" in query else recent + [older]), patch.object(collector.ReadingResolver, "resolve", side_effect=reading):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(self.published())
        self.assertEqual(self.pending()[word]["reading_status"], "conflict")

    def test_search_failure_persists_attempts_and_pending_without_adoption(self):
        row = candidate("未確定の語彙")
        self.queue({row["normalized"]: row})
        def feed(query):
            if "when:30d" in query:
                raise RuntimeError("RSS unavailable")
            return evidence(row["word"])
        with patch.object(collector, "google_news_rss", side_effect=feed), patch.object(collector.ReadingResolver, "resolve", return_value={"reading": "要確認", "reading_status": "unconfirmed"}):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.published())
        self.assertIn(row["normalized"], self.pending())
        self.assertEqual(json.loads(self.pending_path.read_text())["retry_state"]["terms"], [row["normalized"]])

    def test_syndicated_headlines_and_agency_reposts_count_once(self):
        first, second = evidence("ニュース語")
        second["title"] = first["title"]
        self.assertEqual(pipeline.independent_sources([first, second], collector), [])
        second["title"] += "続報"
        first["description"] = second["description"] = "共同通信配信"
        self.assertEqual(pipeline.independent_sources([first, second], collector), [])

    def test_identical_bodies_unavailable_pages_and_agency_credits_are_not_independent(self):
        sources = evidence("語彙テスト")
        for page in (lambda link: '<p>語彙テスト（ゴイテスト）を解説。</p>',
                     lambda link: '<p>語彙テストを紹介。共同通信配信。</p><footer>' + link + '</footer>'):
            with patch.object(collector, "public_reading_document", side_effect=page):
                self.assertEqual(len(pipeline.verified_usage("語彙テスト", sources, collector.ReadingResolver(), collector)), 1)
        with patch.object(collector, "public_reading_document", side_effect=ValueError("unavailable")):
            self.assertEqual(pipeline.verified_usage("語彙テスト", sources, collector.ReadingResolver(), collector), [])

    def test_page_budget_is_shared_by_usage_and_reading_checks(self):
        resolver = collector.ReadingResolver(page_limit=1)
        with patch.object(collector, "public_reading_document", return_value='<p>語彙テスト（ゴイテスト）を解説。</p>') as fetch:
            self.assertEqual(len(pipeline.verified_usage("語彙テスト", evidence("語彙テスト"), resolver, collector)), 1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(resolver.page_count, 1)

    def test_toronto_dates_are_used_across_dst(self):
        for instant, date in (("2026-03-09T00:00:00+00:00", "2026-03-08"), ("2026-11-02T00:00:00+00:00", "2026-11-01")):
            now = dt.datetime.fromisoformat(instant)
            self.rss = evidence("時差の語" + date, now=now)
            self.assertEqual(self.run_main(now), 0)
            self.assertEqual(self.published()[-1]["accepted_date"], date)
