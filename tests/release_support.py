import contextlib
import copy
import datetime as dt
import email.utils
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_candidates as collector
import release_archive as data
import collect_release_words as release
import dictionary_assets as dictionaries
import monthly_sources as monthly

NOW = dt.datetime(2026, 10, 8, 19, tzinfo=collector.TZ)
REGISTERED_SEED_WORDS = {"グリークヨーグルト", "スポットワーク", "モキュメンタリー", "モフリン", "チームみらい", "ぬい活"}


class FakeDictionary:
    def __init__(self, words=()):
        self.words = {collector.normalize(word) for word in words}
        self.document = {"provenance": dictionaries.provenance()}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def contains(self, word):
        return collector.normalize(word) in self.words

    def check(self, word, day):
        return {**self.document["provenance"], "checked_at": day,
                "status": "present" if self.contains(word) else "missing"}


def reviewed_seed():
    # The checked-in ledger grows daily. Fixtures must remain independent of
    # its current word count so scheduled tests keep passing after collection.
    document = json.loads((data.ROOT / "data/lists/2026-10-07.json").read_text(encoding="utf-8"))
    rows = [release.confirmed_row(row, row["date"]) for row in document["rows"] if row["word"] not in REGISTERED_SEED_WORDS]
    for row in rows:
        row["dictionary_check"] = FakeDictionary().check(row["word"], row["date"])
    return rows


def article(word, publisher, age=dt.timedelta(hours=1), link=None, now=NOW):
    return {"title": f'{publisher}が解説する新語「{word}」の意味',
            "link": link or f"https://{publisher}.example/articles/{quote(word)}",
            "pubdate": email.utils.format_datetime((now - age).astimezone(dt.timezone.utc)), "source": publisher}


def evidence(word, age=dt.timedelta(hours=1), publishers=2, now=NOW):
    return [article(word, f"media{index}", age, now=now) for index in range(publishers)]


def candidate(word, date="2026-10-07", *, kind="common", categories=None):
    tags = categories or ["technology"]
    return {"date": date, "accepted_date": date, "checked_at": date,
            "word": word, "normalized": collector.normalize(word),
            "reading": collector.kana_reading(word) or "てすとよみ", "reading_status": "confirmed",
            "reading_method": "source", "reading_sources": evidence(word), "reading_note": "",
            "usage_status": "confirmed", "sources": evidence(word), "kind": kind,
            "pos": collector.pos_label(word, kind), "categories": tags, "category": tags[0],
            "dictionary_check": FakeDictionary().check(word, date),
            "official_name_sources": [] if kind == "common" else [{"source": "公式", "link": "https://official.example/name"}]}


class NewsFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.pending_path = self.directory / "pending.json"
        self.seed = [candidate("履歴テスト")]
        data.save_entries(self.seed, self.directory)
        self.queue({})
        self.rss = []
        self.dictionary = FakeDictionary()
        self.output = io.StringIO()
        for patcher in (
            patch.object(monthly, "load_sources", return_value=[next(source for source in monthly.load_sources() if source['id'] == 'category_discovery')]),
            patch.object(monthly.Network, "throttle", return_value=None),
            patch.object(monthly, "verify_usage", side_effect=lambda word, sources, readings, context: collector.verify_usage_sources(word, sources, readings)),
            patch.object(monthly, "official_name", side_effect=lambda word, sources, readings, context: collector.require_official_name(word, sources, readings)),
            patch.object(dictionaries.DictionaryIndex, "from_environment", return_value=self.dictionary),
            patch.object(collector, "google_news_rss", side_effect=lambda query: self.rss),
            patch.object(collector, "verify_usage_sources", side_effect=lambda word, sources, readings: sources),
            patch.object(collector, "require_official_name", return_value=[{"source": "公式", "link": "https://official.example/name"}]),
            patch.object(collector.ReadingResolver, "resolve", side_effect=lambda word, sources: {
                "reading": collector.kana_reading(word) or "てすとよみ", "reading_status": "confirmed",
                "reading_method": "source", "reading_sources": sources, "reading_note": ""}),
            patch.object(collector, "READINGS_PATH", self.directory / "readings.json"),
            patch.object(collector.urllib.request, "urlopen", side_effect=AssertionError("Unexpected real network")),
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.directory / "summary.md")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for manager in (contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output)):
            manager.__enter__()
            self.addCleanup(manager.__exit__, None, None, None)

    def queue(self, rows, *, state=None):
        release.save_queue(self.directory, copy.deepcopy(rows), state or {"date": "", "terms": []})

    def pending(self):
        return collector.candidate_pipeline.load_pending(self.pending_path, collector.normalize)

    def run_main(self, now=NOW, *, mode="collect"):
        return release.run(now, mode=mode, directory=self.directory)

    def published(self):
        return data.load_entries(self.directory)[len(self.seed):]
