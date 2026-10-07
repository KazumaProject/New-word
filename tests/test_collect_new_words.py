import contextlib
import copy
import csv
import datetime as dt
import email.utils
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "collector", Path(__file__).resolve().parents[1] / "scripts" / "collect_new_words.py"
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
NOW = dt.datetime(2026, 10, 2, 20, 15, tzinfo=collector.TZ)


def article(word, publisher, age=dt.timedelta(hours=1), link=None, now=NOW):
    return {
        "title": f"{publisher}が解説する新語「{word}」の意味",
        "link": link or f"https://example.com/{word}/{publisher}",
        "pubdate": email.utils.format_datetime((now - age).astimezone(dt.timezone.utc)),
        "source": publisher,
    }


def evidence(word, age=dt.timedelta(hours=1), publishers=2, now=NOW):
    return [article(word, f"媒体{index}", age, now=now) for index in range(publishers)]


def candidate(word, date="2026-10-02", supplement=False):
    return {
        "date": date, "reading": collector.reading_hint(word), "word": word,
        "reading_status": "confirmed" if collector.kana_reading(word) else "unconfirmed",
        "reading_method": "kana" if collector.kana_reading(word) else "", "reading_sources": [],
        "reading_note": "" if collector.kana_reading(word) else "出典に読みの明記が見つからないため確認待ち",
        "pos": collector.pos_label(word), "id": "1920", "normalized": collector.normalize(word),
        "sources": evidence(word), "supplement": supplement,
    }


def issue(number, rows, extra=""):
    title = f"新語候補 {rows[0]['date']}"
    return {
        "number": number, "title": title,
        "body": collector.render_issue(title, rows) + extra,
        "html_url": f"https://github.com/{collector.REPO}/issues/{number}",
    }


class FakeGitHub:
    def __init__(self, issues=()):
        self.issues = copy.deepcopy(list(issues))
        self.calls = []
        self.fail_write = False

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and "?state=all" in path:
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0])
            return copy.deepcopy(self.issues[(page - 1) * 100:page * 100])
        if method == "GET":
            number = int(path.rsplit("/", 1)[1])
            return copy.deepcopy(next(row for row in self.issues if row["number"] == number))
        if self.fail_write:
            raise urllib.error.URLError("write response unavailable")
        if method == "POST":
            number = max([row["number"] for row in self.issues], default=0) + 1
            result = {"number": number, **payload, "html_url": f"https://github.com/{collector.REPO}/issues/{number}"}
            self.issues.append(result)
            return copy.deepcopy(result)
        if method == "PATCH":
            number = int(path.rsplit("/", 1)[1])
            result = next(row for row in self.issues if row["number"] == number)
            result.update(payload)
            return copy.deepcopy(result)
        raise AssertionError((method, path))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.seen = Path(directory.name) / "seen.tsv"
        self.seen.write_text("\t".join(collector.SEEN_FIELDS) + "\n", encoding="utf-8")
        self.summary = Path(directory.name) / "summary.md"
        self.github = FakeGitHub()
        self.rss = []
        for patcher in (
            patch.object(collector, "SEEN_PATH", self.seen),
            patch.object(collector, "github_api", side_effect=lambda *args: self.github(*args)),
            patch.object(collector, "PENDING_PATH", Path(directory.name) / "pending.json"),
            patch.object(collector, "verify_usage_sources", side_effect=lambda word, sources, readings: sources),
            patch.object(collector, "require_official_name", return_value=[{"source": "公式", "link": "https://official.example/name"}]),
            patch.object(collector.ReadingResolver, "resolve", side_effect=lambda word, sources: {"reading": collector.kana_reading(word) or "てすとよみ", "reading_status": "confirmed", "reading_method": "source", "reading_sources": sources, "reading_note": ""}),
            patch.object(collector, "public_reading_document", return_value=""),
            patch.object(collector, "READINGS_PATH", Path(directory.name) / "readings.json"),
            patch.object(collector, "google_news_rss", side_effect=lambda query: self.rss),
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)}),
            patch.object(collector.urllib.request, "urlopen", side_effect=AssertionError("unexpected real network request")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        self.errors = io.StringIO()
        for manager in (contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors)):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def run_main(self, now=NOW):
        return collector.main(now)

    def published(self):
        return collector.issue_rows(self.github.issues[-1])

    def test_delayed_start_posts_then_saves_seen(self):
        self.rss = evidence("クラウドノヴァ")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.github.issues[0]["title"], "新語候補 2026-10-02")
        self.assertEqual(collector.load_seen(), {"クラウドノヴァ"})
        self.assertFalse(self.published()[0]["supplement"])
        self.assertIn("掲載: 1語", self.summary.read_text())

    def test_toronto_dates_in_winter_summer_and_transition_days(self):
        for local_date, utc_date in (
            ("2026-01-15", "2026-01-16T00:00:00+00:00"),
            ("2026-07-15", "2026-07-15T23:00:00+00:00"),
            ("2026-03-08", "2026-03-08T23:00:00+00:00"),
            ("2026-11-01", "2026-11-02T00:00:00+00:00"),
        ):
            with self.subTest(date=local_date):
                self.github = FakeGitHub()
                now = dt.datetime.fromisoformat(utc_date)
                self.rss = evidence(f"新名称{local_date}", now=now)
                self.assertEqual(self.run_main(now), 0)
                self.assertEqual(self.github.issues[0]["title"], f"新語候補 {local_date}")

    def test_fallback_expands_window_and_marks_older_words(self):
        for days, expected_stage in ((10, "直近30日"), (100, "直近365日"), (500, "期間制限なし")):
            with self.subTest(age=days):
                self.github = FakeGitHub()
                self.rss = evidence(f"過去の名称{days}", age=dt.timedelta(days=days))
                self.output.seek(0)
                self.output.truncate()
                self.assertEqual(self.run_main(), 0)
                self.assertTrue(self.published()[0]["supplement"])
                self.assertIn(f"Search: 補充: {expected_stage}", self.output.getvalue())
                self.assertIn("過去の記事も対象", self.github.issues[0]["body"])

    def test_recent_window_is_twenty_four_elapsed_hours_across_dst(self):
        now = dt.datetime(2026, 11, 1, 19, tzinfo=collector.TZ)
        published = now.astimezone(dt.timezone.utc) - dt.timedelta(hours=24, minutes=30)
        self.rss = evidence("夏時間切替の名称")
        for item in self.rss:
            item["pubdate"] = email.utils.format_datetime(published)
        self.assertEqual(self.run_main(now), 0)
        self.assertTrue(self.published()[0]["supplement"])

    def test_zero_candidates_succeeds_without_issue_or_seen_changes(self):
        self.rss = evidence("単独媒体の名称", publishers=1)
        original = self.seen.read_bytes()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.github.issues, [])
        self.assertEqual(self.seen.read_bytes(), original)
        self.assertIn("期間制限なし", self.output.getvalue())
        self.assertIn("pending", self.output.getvalue())

    def test_invalid_future_and_old_dates_are_not_used_as_recent_evidence(self):
        self.rss = [
            article("不正日付", "媒体A") | {"pubdate": "invalid"},
            article("未来日付", "媒体A", age=dt.timedelta(hours=-1)),
            article("古い日付", "媒体A", age=dt.timedelta(days=2)),
        ]
        self.assertEqual(self.run_main(), 0)
        self.assertIn("invalid_or_future_date", self.output.getvalue())
        self.assertIn("outside_window", self.output.getvalue())

    def test_normalized_variants_share_evidence_and_source_references(self):
        self.rss = [article("ＣｌｏｕｄＮｏｖａ", "媒体A"), article("cloudnova", "媒体B")]
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.published()), 1)
        self.assertEqual(self.published()[0]["normalized"], "cloudnova")
        self.assertEqual(len(self.published()[0]["sources"]), 2)

    def test_repeated_feed_items_do_not_count_as_two_publishers(self):
        self.rss = [article("単独名称", "媒体A")] * 10
        self.assertEqual(self.run_main(), 0)
        self.assertFalse(any(path.startswith("/search/code?") for _, path, _ in self.github.calls))

    def test_duplicate_link_is_not_two_sources(self):
        self.rss = [article("同一リンク", source, link="https://example.com/shared") for source in ("媒体A", "媒体B")]
        self.assertEqual(self.run_main(), 0)

    def test_sources_include_distinct_publishers_even_when_first_has_many_articles(self):
        items = [article("名称", "媒体A", link=f"https://example.com/a{index}") for index in range(4)]
        items.append(article("名称", "媒体B", age=dt.timedelta(hours=2)))
        sources = collector.select_sources(items)
        self.assertEqual({row["source"] for row in sources}, {"媒体A", "媒体B"})

    def test_shared_link_does_not_hide_an_alternative_valid_pair(self):
        items = [
            article("名称", "媒体A", link="https://example.com/shared"),
            article("名称", "媒体B", link="https://example.com/shared"),
            article("名称", "媒体A", age=dt.timedelta(hours=2), link="https://example.com/other"),
        ]
        self.assertEqual(len(collector.select_sources(items)), 2)

    def test_daily_limit_prioritizes_publishers_then_recency_then_word(self):
        for index in range(12):
            self.rss.extend(evidence(f"新名称{index:02}", age=dt.timedelta(hours=1, minutes=index)))
        self.rss.extend(evidence("多媒体の名称", publishers=3, age=dt.timedelta(hours=2)))
        self.assertEqual(self.run_main(), 0)
        words = [row["word"] for row in self.published()]
        self.assertEqual(words, ["多媒体の名称"] + [f"新名称{index:02}" for index in range(9)])
        self.assertNotIn("新名称09", collector.load_seen())
        self.assertEqual(sum(path.startswith("/search/code?") for _, path, _ in self.github.calls), 0)

    def test_already_seen_and_past_issue_words_are_excluded(self):
        collector.append_seen([candidate("掲載済み")])
        self.github = FakeGitHub([issue(1, [candidate("過去候補", date="2026-10-01")])])
        for word in ("掲載済み", "過去候補", "未登録候補"):
            self.rss.extend(evidence(word))
        self.assertEqual(self.run_main(), 0)
        self.assertEqual([row["word"] for row in self.published()], ["未登録候補"])

    def test_prior_word_containing_candidate_does_not_exclude_shorter_word(self):
        self.github = FakeGitHub([issue(1, [candidate("AlphaExtra", date="2026-10-01")])])
        self.rss = evidence("Alpha")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.published()[0]["word"], "Alpha")




    def test_same_day_append_preserves_human_text_and_respects_total_limit(self):
        old_rows = [candidate(f"掲載済み{index}") for index in range(9)]
        existing = issue(1, old_rows, extra="\n\n手書きの検討メモ。  \n")
        self.github = FakeGitHub([existing])
        self.rss = evidence("追加候補A") + evidence("追加候補B")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.published()), 10)
        self.assertTrue(self.github.issues[0]["body"].startswith(existing["body"]))
        self.assertFalse(any(method == "POST" for method, _, _ in self.github.calls))
        self.github.calls.clear()
        with patch.object(collector, "google_news_rss", side_effect=AssertionError("already full")):
            self.assertEqual(self.run_main(), 0)
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.github.calls))

    def test_existing_daily_issue_survives_when_no_additions_exist(self):
        original = issue(1, [candidate("既存候補")], extra="\n手書きメモ")
        self.github = FakeGitHub([original])
        with patch.object(collector.ReadingResolver, "estimate", return_value=None):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.github.issues, [original])


    def test_legacy_tsv_is_parsed_and_preserved_on_append(self):
        row = candidate("旧形式の候補")
        body = "```tsv\n読み\t表記\t左ID\t右ID\t品詞\n" + f"{row['reading']}\t{row['word']}\t{row['id']}\t{row['id']}\t{row['pos']}\n```\n手書きメモ"
        legacy = {"number": 1, "title": "新語候補 2026-10-02", "body": body, "html_url": "https://example.com/issues/1"}
        self.github = FakeGitHub([legacy])
        self.rss = evidence("新形式の候補")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.published()), 2)
        self.assertIn("手書きメモ", self.github.issues[0]["body"])
        self.assertIn(row["word"], self.github.issues[0]["body"])

    def test_publishing_failure_does_not_save_new_words(self):
        self.github.fail_write = True
        self.rss = evidence("投稿失敗の候補")
        original = self.seen.read_bytes()
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.seen.read_bytes(), original)

    def test_failed_seen_write_is_recovered_from_issue_on_rerun(self):
        for index in range(10):
            self.rss.extend(evidence(f"復元する語{index}"))
        real_append = collector.append_seen
        with patch.object(collector, "append_seen", side_effect=lambda rows: real_append(rows) if not rows else (_ for _ in ()).throw(OSError("disk full"))):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.github.issues), 1)
        self.assertEqual(collector.load_seen(), set())
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(collector.load_seen()), 10)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.github.calls), 1)

    def test_seen_updates_are_atomic_and_deduplicate_normalized_words(self):
        original = self.seen.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                collector.append_seen([candidate("CloudNova")])
        self.assertEqual(self.seen.read_bytes(), original)
        self.assertEqual(list(self.seen.parent.glob(".seen-*")), [])
        self.assertEqual(collector.append_seen([candidate("CloudNova"), candidate("ＣｌｏｕｄＮｏｖａ")]), 1)

    def test_metadata_escapes_comment_delimiters_and_round_trips(self):
        row = candidate("新--名称|試験")
        rendered = issue(1, [row])
        self.assertEqual(collector.issue_rows(rendered), [row])
        self.assertIn("新--名称&#124;試験", rendered["body"])

    def test_invalid_metadata_fails_instead_of_overwriting_issue(self):
        existing = {"number": 1, "title": "新語候補 2026-10-02", "body": '<!-- new-word-entries\n{"version": 99, "rows": []}\n-->', "html_url": "https://example.com/1"}
        self.github = FakeGitHub([existing])
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.github.issues, [existing])

    def test_issue_pagination_has_no_thousand_issue_cutoff_and_ignores_prs(self):
        self.github = FakeGitHub([{"number": index} for index in range(1100)])
        self.github.issues[50]["pull_request"] = {}
        self.assertEqual(len(collector.list_issues()), 1099)
        self.assertEqual(len(self.github.calls), 12)


class NetworkingTests(unittest.TestCase):
    def response(self, body=b"ok"):
        return contextlib.nullcontext(io.BytesIO(body))

    def test_read_retry_respects_retry_after(self):
        error = urllib.error.HTTPError("https://example.com", 429, "limited", {"Retry-After": "17"}, None)
        with patch.object(collector.urllib.request, "urlopen", side_effect=[error, self.response()]) as opener, patch.object(collector.time, "sleep") as sleep:
            self.assertEqual(collector.request("https://example.com"), b"ok")
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(17.0)

    def test_retry_after_http_date_and_rate_limit_reset(self):
        error = urllib.error.HTTPError("https://example.com", 403, "limited", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "120"}, None)
        with patch.object(collector.time, "time", return_value=100):
            self.assertEqual(collector.retry_delay(error, 1), 21.0)
        when = email.utils.format_datetime(dt.datetime.fromtimestamp(125, dt.timezone.utc), usegmt=True)
        error.headers = {"Retry-After": when}
        with patch.object(collector.time, "time", return_value=100):
            self.assertEqual(collector.retry_delay(error, 1), 25.0)

    def test_server_and_network_errors_retry_but_permission_errors_do_not(self):
        for error in (urllib.error.URLError("offline"), urllib.error.HTTPError("https://example.com", 503, "busy", {}, None)):
            with self.subTest(error=error), patch.object(collector.urllib.request, "urlopen", side_effect=[error, self.response()]) as opener, patch.object(collector.time, "sleep"):
                self.assertEqual(collector.request("https://example.com"), b"ok")
                self.assertEqual(opener.call_count, 2)
        denied = urllib.error.HTTPError("https://example.com", 401, "unauthorized", {}, None)
        with patch.object(collector.urllib.request, "urlopen", side_effect=denied) as opener, patch.object(collector.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                collector.request("https://example.com")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_retry_budget_is_three_attempts(self):
        with patch.object(collector.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as opener, patch.object(collector.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                collector.request("https://example.com")
        self.assertEqual(opener.call_count, 3)

    def test_ambiguous_issue_write_is_not_retried(self):
        with patch.object(collector.urllib.request, "urlopen", side_effect=urllib.error.URLError("lost response")) as opener, patch.object(collector.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.URLError):
                collector.request("https://example.com", method="POST", body=b"{}")
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()





if __name__ == "__main__":
    unittest.main()
