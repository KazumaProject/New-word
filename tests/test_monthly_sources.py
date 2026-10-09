import contextlib
import copy
import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from release_support import data, monthly, FakeDictionary, NOW, candidate, release, collector, evidence


def entry(number, spelling=None, reading="てすとご", extra="", pos="noun (common) (futsuumeishi)"):
    spelling = spelling or f"検証語{number}"
    return f"<entry><ent_seq>{number}</ent_seq><k_ele><keb>{spelling}</keb></k_ele><r_ele><reb>{reading}</reb></r_ele><sense><pos>{pos}</pos>{extra}</sense></entry>"


class FixtureNetwork:
    def __init__(self, paths=None, pages=None):
        self.paths, self.pages = paths or {}, pages or {}
        self.expired = False
    def check(self):
        if self.expired:
            raise monthly.BudgetExpired()
    def throttle(self, url):
        self.check()
    def snapshot(self, source):
        self.check()
        path = self.paths[source["id"]]
        return path, hashlib.sha256(path.read_bytes()).hexdigest()
    def page(self, url):
        self.check()
        return self.pages[url]


class MonthlyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.sources = {source["id"]: source for source in monthly.load_sources()}
        self.dictionary = FakeDictionary()
        self.pending = {}
        self.seed = [candidate("保存済み語")]
        data.save_entries(self.seed, self.directory)
        release.save_queue(self.directory, {}, {"date": "2026-10-07", "terms": ["旧履歴"]})
        self.network = FixtureNetwork()
        self.patchers = [patch.object(collector, "READINGS_PATH", self.directory / "readings.json"),
                         patch.object(collector.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network"))]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def xml(self, body, source="jmdict"):
        path = self.directory / (source + ".xml.gz")
        root = "JMdict" if source == "jmdict" else "JMnedict"
        with gzip.open(path, "wb") as stream:
            stream.write(f"<{root}>{body}</{root}>".encode("utf-8"))
        self.network.paths[source] = path
        return path

    def run_collect(self, ids=("jmdict",), rows=None, **options):
        return monthly.collect(self.seed if rows is None else rows, self.pending, self.dictionary, NOW,
                               self.directory, sources=[self.sources[identity] for identity in ids], network=self.network, **options)

    def test_complete_snapshot_has_no_ten_or_three_hundred_word_limit(self):
        self.xml("".join(entry(i) for i in range(350)))
        rows, state = self.run_collect()
        self.assertEqual(len(rows), 351)
        self.assertTrue(state["complete"])
        self.assertEqual(state["sources"]["jmdict"]["cursor"], 350)
        self.assertEqual(state["sources"]["jmdict"]["counts"]["accepted"], 350)
        for row in rows[1:]:
            self.assertTrue(monthly.valid_reference_evidence(row))
            self.assertEqual(len(row["sources"]), 1)

    def test_reading_restrictions_non_nouns_and_bad_forms(self):
        body = """<entry><ent_seq>1</ent_seq>
          <k_ele><keb>甲語</keb></k_ele><k_ele><keb>乙語</keb></k_ele>
          <k_ele><keb>誤表記</keb><ke_inf>word containing irregular kanji usage</ke_inf><ke_inf>incorrect spelling</ke_inf></k_ele>
          <r_ele><reb>こうご</reb><re_restr>甲語</re_restr></r_ele>
          <r_ele><reb>おつご</reb><re_restr>乙語</re_restr></r_ele>
          <r_ele><reb>あやまり</reb><re_inf>incorrect kana usage</re_inf></r_ele>
          <r_ele><reb>かなのみ</reb><re_nokanji/></r_ele>
          <sense><pos>noun (common)</pos></sense></entry>"""
        body += entry(2, "動詞形", pos="Godan verb")
        body += entry(3, "形容詞形", pos="adjective (keiyoushi)")
        path = self.xml(body)
        records = list(monthly.xml_records(path, self.sources["jmdict"]))
        self.assertEqual([(row["word"], row["readings"]) for row in records], [("甲語", ["こうご"]), ("乙語", ["おつご"])])

    def test_sense_restrictions_and_internal_dtd_entities(self):
        path = self.directory / "dtd.xml"
        path.write_text('''<!DOCTYPE JMdict [<!ENTITY n "noun (common)">]><JMdict>
          <entry><ent_seq>9</ent_seq><k_ele><keb>名詞形</keb></k_ele><k_ele><keb>動詞形</keb></k_ele>
          <r_ele><reb>めいしけい</reb><re_restr>名詞形</re_restr></r_ele><r_ele><reb>どうしけい</reb><re_restr>動詞形</re_restr></r_ele>
          <sense><pos>&n;</pos><stagk>名詞形</stagk><stagr>めいしけい</stagr></sense>
          <sense><pos>verb</pos><stagk>動詞形</stagk></sense></entry></JMdict>''', encoding="utf-8")
        rows = list(monthly.xml_records(path, self.sources["jmdict"]))
        self.assertEqual([(row["word"], row["readings"]) for row in rows], [("名詞形", ["めいしけい"])])

    def test_irregular_historical_forms_are_valid_but_search_only_forms_are_not(self):
        body = entry(1, "歴史表記").replace("</keb>", "</keb><ke_inf>word containing out-dated kanji usage</ke_inf>")
        body += entry(2, "特殊表記").replace("</reb>", "</reb><re_inf>word containing irregular kana usage</re_inf>")
        body += entry(3, "検索専用").replace("</keb>", "</keb><ke_inf>search-only kanji form</ke_inf>")
        body += entry(4, "検索読み").replace("</reb>", "</reb><re_inf>search-only kana form</re_inf>")
        path = self.xml(body)
        self.assertEqual([row["word"] for row in monthly.xml_records(path, self.sources["jmdict"])], ["歴史表記", "特殊表記"])

    def test_name_types_and_domain_category_mapping(self):
        body = "".join(f"<entry><ent_seq>{i}</ent_seq><k_ele><keb>{name}</keb></k_ele><r_ele><reb>なまえ</reb></r_ele><trans><name_type>{kind}</name_type></trans></entry>"
                       for i, (name, kind) in enumerate((("人名例", "surname"), ("駅名例", "railway station"), ("組織例", "organization"), ("製品例", "product name"), ("作品例", "work of art"))))
        self.xml(body, "jmnedict")
        rows, _ = self.run_collect(("jmnedict",))
        self.assertEqual([row["kind"] for row in rows[1:]], ["person", "place", "organization", "product", "work"])
        self.xml(entry(10, extra="<field>computing</field><misc>Internet slang</misc>"))
        record = next(monthly.xml_records(self.network.paths["jmdict"], self.sources["jmdict"]))
        self.assertEqual(record["categories"], ["technology", "slang", "software_engineering"])

    def test_registered_spelling_never_fetches_evidence_and_is_normalized(self):
        self.xml(entry(1, "Ｃｌｏｕｄ　Ｎｏｖａ", "べつのよみ"))
        self.dictionary.words.add("cloud nova")
        rows, state = self.run_collect()
        self.assertEqual(rows, self.seed)
        self.assertEqual(state["sources"]["jmdict"]["counts"]["already_registered"], 1)
        self.assertFalse(self.pending)

    def test_conflicting_readings_demote_new_term_without_losing_notes(self):
        word = "読み競合語"
        self.pending[word] = {**candidate(word), "manual_notes": "人手メモ"}
        self.xml(entry(1, word, "よみいち") + entry(2, word, "よみに"))
        rows, _ = self.run_collect()
        self.assertEqual(rows, self.seed)
        self.assertEqual(self.pending[word]["reading_status"], "conflict")
        self.assertEqual(self.pending[word]["manual_notes"], "人手メモ")
        self.assertEqual(len(self.pending[word]["source_records"]), 2)

    def test_budget_checkpoints_and_resume_exact_remaining_records(self):
        self.xml("".join(entry(i) for i in range(40)))
        checks = 0
        original = self.network.check
        def limited():
            nonlocal checks
            checks += 1
            if checks > 29:
                raise monthly.BudgetExpired()
            original()
        with patch.object(self.network, "check", side_effect=limited):
            first, state = self.run_collect()
        self.assertFalse(state["complete"])
        self.assertTrue(state["budget_exhausted"])
        self.assertGreater(len(first), 1)
        self.assertLess(len(first), 41)
        loaded = data.load_entries(self.directory)
        second, state = self.run_collect(rows=loaded)
        self.assertTrue(state["complete"])
        self.assertEqual(len(second), 41)
        self.assertEqual(state["sources"]["jmdict"]["counts"]["accepted"], 40)
        third, _ = self.run_collect(rows=second)
        self.assertEqual(third, second)

    def test_changed_snapshot_resets_cursor_without_skipping_additions(self):
        self.xml(entry(1))
        first, _ = self.run_collect()
        self.xml(entry(0) + entry(1) + entry(2))
        rows, state = self.run_collect(rows=first)
        self.assertEqual(len(rows), 4)
        self.assertEqual(state["sources"]["jmdict"]["cursor"], 3)

    def test_invalid_snapshot_is_reported_and_does_not_reset_saved_words(self):
        self.network.paths["jmdict"] = self.directory / "missing.xml.gz"
        rows, state = self.run_collect()
        self.assertFalse(state["complete"])
        self.assertEqual(state["sources"]["jmdict"]["status"], "failed")
        self.assertEqual(rows, self.seed)

    def test_changed_adapter_configuration_reprocesses_unchanged_snapshot(self):
        self.xml(entry(1))
        first, state = self.run_collect()
        before = state["sources"]["jmdict"]["adapter_sha256"]
        self.sources["jmdict"]["attribution"] += "; revised adapter configuration"
        rows, state = self.run_collect(rows=first)
        self.assertNotEqual(state["sources"]["jmdict"]["adapter_sha256"], before)
        self.assertEqual(state["sources"]["jmdict"]["counts"]["scanned"], 1)
        self.assertEqual(state["sources"]["jmdict"]["counts"]["already_accepted"], 1)
        self.assertEqual(len(rows), 2)

    def test_record_identifiers_are_namespaced_by_source(self):
        self.xml(entry(1, "共通名称"))
        self.xml('<entry><ent_seq>1</ent_seq><k_ele><keb>共通名称</keb></k_ele><r_ele><reb>てすとご</reb></r_ele><trans><name_type>company name</name_type></trans></entry>', "jmnedict")
        rows, _ = self.run_collect(("jmdict", "jmnedict"))
        row = next(row for row in rows if row["word"] == "共通名称")
        self.assertEqual({ref["source_id"] for ref in row["source_records"]}, {"jmdict", "jmnedict"})
        self.assertEqual(len(row["source_records"]), 2)

    def test_legacy_queue_history_and_notes_survive_version_migration(self):
        row = {**candidate("保留語"), "manual_notes": "保持"}
        self.pending[row["normalized"]] = row
        self.xml(entry(1))
        self.run_collect()
        queue = json.loads((self.directory / "pending.json").read_text(encoding="utf-8"))
        self.assertEqual(queue["version"], 2)
        self.assertEqual(queue["legacy_retry_history"], {"date": "2026-10-07", "terms": ["旧履歴"]})
        self.assertEqual(queue["rows"][0]["manual_notes"], "保持")
        self.assertEqual(queue["rows"][0]["metadata_version"], 2)

    def test_authoritative_tsumura_names_and_full_readings(self):
        source = self.sources["tsumura"]
        self.network.pages[source["url"]] = json.dumps({"products": [{"product_Id": "001", "name": "ツムラ葛根湯", "nameKana": "ツムラカッコントウ"}]}, ensure_ascii=False)
        rows, state = self.run_collect(("tsumura",))
        self.assertTrue(state["complete"])
        self.assertEqual([(row["word"], row["reading"]) for row in rows[1:]], [("ツムラ葛根湯", "つむらかっこんとう"), ("葛根湯", "かっこんとう")])
        self.assertEqual(rows[-1]["categories"], ["kampo", "medicine"])
        self.assertTrue(all(monthly.valid_reference_evidence(row) for row in rows[1:]))

    def test_catalog_uses_explicit_reading_and_resumes_links(self):
        source = self.sources["mdn"]
        self.network.pages = {source["url"]: '<h1>用語集</h1><a href="/ja/docs/Glossary/Test">詳しい用語</a>',
                              "https://developer.mozilla.org/ja/docs/Glossary/Test": '<h1>検証用語（けんしょうようご）</h1>'}
        self.dictionary.words.add("用語集")
        rows, state = self.run_collect(("mdn",))
        self.assertEqual(rows[-1]["word"], "検証用語")
        self.assertEqual(rows[-1]["reading"], "けんしょうようご")
        self.assertEqual(len(state["sources"]["mdn"]["visited"]), 2)
        self.assertTrue(monthly.valid_reference_evidence(rows[-1]))

    def test_reference_policy_flag_alone_cannot_bypass_evidence_validation(self):
        row = candidate("検証用語")
        row.update(evidence_type="curated_reference", sources=row["sources"][:1])
        with self.assertRaisesRegex(ValueError, "Reference evidence"):
            data.validate_row(row, collector.CATEGORIES)

    def test_archive_has_checked_attribution_and_incomplete_coverage(self):
        self.xml(entry(1))
        rows, state = self.run_collect()
        output = self.directory / "new-words.zip"
        manifest = data.build_archive(rows, output, source_commit="a" * 40, updated_at="2026-10-09T00:00:00Z", collection_status=state)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["columns"], ["reading", "word", "pos"])
        with zipfile.ZipFile(output) as archive:
            self.assertIn(b"CC BY-SA 4.0", archive.read("SOURCES.txt"))
            payloads = {name: archive.read(name) for name in archive.namelist()}
        payloads["SOURCES.txt"] = b"corrupt"
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in payloads.items():
                archive.writestr(name, raw)
        with self.assertRaisesRegex(ValueError, "Attribution integrity"):
            data.validate_archive(output)

    def test_corrupt_checkpoint_is_not_silently_reset(self):
        path = self.directory / "collection-state.json"
        path.write_text('{"version":1,"sources":{"jmdict":{"status":"complete","cursor":-1}}}', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.run_collect()
        self.assertEqual(before, path.read_bytes())

    def test_reviewed_reference_conflict_is_retried_on_same_month_manual_run(self):
        word = "読み競合語"
        self.xml(entry(1, word, "よみいち") + entry(2, word, "よみに"))
        rows, state = self.run_collect()
        self.assertEqual(self.pending[word]["reading_status"], "conflict")
        self.assertTrue(state["complete"])
        reviewed = [{"word": word, "reading": "よみいち", "sources": [{"source": "確認資料", "link": "https://official.example/reading"}]}]
        (self.directory / "readings.json").write_text(json.dumps(reviewed), encoding="utf-8")
        rows, state = self.run_collect(rows=rows)
        row = next(row for row in rows if row["word"] == word)
        self.assertEqual(row["reading_method"], "reviewed")
        self.assertEqual(row["reading"], "よみいち")
        self.assertTrue(monthly.valid_reference_evidence(row))
        self.assertNotIn(word, self.pending)

    def test_tsumura_partial_formula_reading_cannot_attest_branded_name(self):
        source = self.sources["tsumura"]
        self.network.pages[source["url"]] = json.dumps({"products": [{"product_Id": "001", "name": "ツムラ葛根湯", "nameKana": "カッコントウ"}]})
        rows, _ = self.run_collect(("tsumura",))
        self.assertEqual(rows, self.seed)
        self.assertIn("ツムラ葛根湯", self.pending)

    def test_glossary_headings_extract_terms_without_bilingual_parentheses(self):
        self.assertEqual(monthly.heading_terms("Abstraction (抽象化)"), ["抽象化"])
        self.assertEqual(monthly.heading_terms("アジャイル／アジャイル開発"), ["アジャイル", "アジャイル開発"])

    def test_legacy_version_one_archive_is_still_readable(self):
        output = self.directory / "legacy.zip"
        manifest = data.build_archive(self.seed, output, source_commit="a" * 40, updated_at="2026-10-09T00:00:00Z")
        with zipfile.ZipFile(output) as archive:
            payloads = {name: archive.read(name) for name in archive.namelist() if name != "SOURCES.txt"}
        manifest["schema_version"] = 1
        manifest.pop("attribution")
        manifest.pop("collection_status")
        manifest["data_sha256"] = data.content_digest(manifest)
        payloads["manifest.json"] = data.json_bytes(manifest)
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in payloads.items():
                archive.writestr(name, raw)
        self.assertEqual(data.validate_archive(output)["schema_version"], 1)

    def test_complete_catalog_refreshes_in_next_month(self):
        source = self.sources["mdn"]
        self.network.pages[source["url"]] = '<h1>検証用語（けんしょうようご）</h1>'
        rows, _ = self.run_collect(("mdn",))
        self.network.pages[source["url"]] = '<h1>翌月用語（よくげつようご）</h1>'
        second, state = monthly.collect(rows, self.pending, self.dictionary, NOW.replace(month=11), self.directory,
                                        sources=[source], network=self.network)
        self.assertEqual(len(second), 3)
        self.assertEqual(state["sources"]["mdn"]["month"], "2026-11")


class WorkflowTests(unittest.TestCase):
    def test_schedules_publication_order_and_recovery_artifact(self):
        root = data.ROOT / ".github/workflows"
        monthly_workflow = (root / "daily-release-words.yml").read_text()
        weekly = (root / "daily-new-words.yml").read_text()
        self.assertIn('cron: "0 19 1 * *"', monthly_workflow)
        self.assertIn('cron: "0 19 * * 1"', weekly)
        self.assertNotIn('cron: "0 19 * * *"', monthly_workflow + weekly)
        self.assertIn("timeout-minutes: 300", monthly_workflow)
        self.assertNotIn("publish_word_release.py --check", monthly_workflow)
        self.assertLess(monthly_workflow.index("git push"), monthly_workflow.index("Build and validate one ZIP"))
        self.assertLess(monthly_workflow.index("actions/upload-artifact"), monthly_workflow.index("Update the single Release asset"))


if __name__ == "__main__":
    unittest.main()
