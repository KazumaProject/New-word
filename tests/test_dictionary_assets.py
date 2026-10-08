import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from release_support import dictionaries, collector


def metadata(lock):
    return {"tag_name": lock["release"], "assets": [{"name": lock["asset"], "size": lock["bytes"],
        "digest": "sha256:" + lock["sha256"],
        "browser_download_url": f"https://github.com/{lock['repository']}/releases/download/{lock['release']}/{lock['asset']}"}]}


class DictionaryAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.index = self.directory / "index.sqlite"
        self.lock = dictionaries.load_lock()
        self.proof = {**dictionaries.provenance(), "packs": {pack: 4 for pack in dictionaries.PACKS},
                      "token_count": 4 * len(dictionaries.PACKS)}
        self.rows = self.directory / "outputs.tsv"
        records = []
        for pack in sorted(dictionaries.PACKS):
            for reading, word in (("とうきょう", "東京"), ("よーぐると", "ヨーグルト"),
                                  ("べつのよみ", "日本橋"), ("くらうど", "Ｃｌｏｕｄ　Ｎｏｖａ")):
                records.append("\t".join([pack, base64.b64encode(reading.encode()).decode(),
                                         base64.b64encode(word.encode()).decode()]) + "\n")
        self.rows.write_text("".join(records), encoding="ascii", newline="\n")
        self.proof["outputs_sha256"] = dictionaries.file_sha256(self.rows)

    def build(self):
        with patch.object(dictionaries, "provenance", return_value=self.proof):
            dictionaries.index_rows(self.rows, {"packs": self.proof["packs"]}, self.index)

    def test_index_matches_optional_packs_any_reading_and_normalized_forms(self):
        self.build()
        with patch.object(dictionaries, "provenance", return_value=self.proof), dictionaries.DictionaryIndex(self.index) as index:
            for word in ("東京", "日本橋", "Cloud Nova", "CLOUD   NOVA", "ＣＬＯＵＤ　ＮＯＶＡ"):
                self.assertTrue(index.contains(word))
            self.assertEqual(index.check("日本橋", "2026-10-08")["status"], "present")
            self.assertFalse(index.contains("未収録の語彙"))
            self.assertEqual(index.check("未収録の語彙", "2026-10-08")["status"], "missing")
        with self.assertRaisesRegex(ValueError, "closed"):
            index.contains("何でも")

    def test_missing_asset_digest_pack_or_changed_release_is_rejected(self):
        for change in ("missing", "digest", "size", "tag", "duplicate"):
            value = metadata(self.lock)
            if change == "missing": value["assets"] = []
            elif change == "digest": value["assets"][0]["digest"] = None
            elif change == "size": value["assets"][0]["size"] += 1
            elif change == "tag": value["tag_name"] = "latest"
            else: value["assets"] *= 2
            with self.subTest(change=change), self.assertRaises(ValueError):
                dictionaries.verified_asset(value, self.lock)

    def test_corrupt_zip_never_runs_decoder_or_installs_index(self):
        asset = self.directory / "asset.zip"
        asset.write_bytes(b"corrupt")
        with patch.object(dictionaries, "decode") as decoder, self.assertRaisesRegex(ValueError, "checksum or size"):
            dictionaries.prepare(self.index, asset_path=asset, api=lambda lock: metadata(lock))
        decoder.assert_not_called()
        self.assertFalse(self.index.exists())

    def test_incomplete_decoded_rows_or_report_never_install_index(self):
        report = {"packs": {**self.proof["packs"], "wiki": 3}}
        with self.assertRaisesRegex(ValueError, "coverage"):
            dictionaries.index_rows(self.rows, report, self.index, expected=self.proof)
        self.rows.write_text(self.rows.read_text().split("\n", 1)[1], encoding="ascii", newline="\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            dictionaries.index_rows(self.rows, {"packs": self.proof["packs"]}, self.index, expected=self.proof)
        self.assertFalse(self.index.exists())

    def test_matching_counts_cannot_hide_incorrect_conversion_outputs(self):
        raw = self.rows.read_text(encoding="ascii")
        raw = raw.replace(base64.b64encode("日本橋".encode()).decode(), base64.b64encode("間違った表記".encode()).decode())
        self.rows.write_text(raw, encoding="ascii", newline="\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            dictionaries.index_rows(self.rows, {"packs": self.proof["packs"]}, self.index, expected=self.proof)

    def test_missing_corrupt_or_wrong_release_index_never_means_missing_word(self):
        with self.assertRaisesRegex(ValueError, "missing or corrupt"):
            dictionaries.DictionaryIndex(self.index)
        self.build()
        self.index.write_bytes(self.index.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "missing or corrupt"):
            dictionaries.DictionaryIndex(self.index)
        self.build()
        with self.assertRaisesRegex(ValueError, "coverage"):
            dictionaries.DictionaryIndex(self.index)

    def test_prepare_downloads_once_and_checks_bytes_before_decoding(self):
        asset = b"fixture bytes"
        lock = {**self.lock, "bytes": len(asset), "sha256": dictionaries.hashlib.sha256(asset).hexdigest()}
        def decode(path, rows, report):
            self.assertEqual(path.read_bytes(), asset)
            rows.write_bytes(self.rows.read_bytes())
            report.write_text(json.dumps({"packs": self.proof["packs"]}), encoding="utf-8")
        with patch.object(dictionaries, "load_lock", return_value=lock), patch.object(dictionaries, "provenance", return_value=self.proof), \
                patch.object(dictionaries, "decode", side_effect=decode), patch.object(collector, "request", return_value=asset) as download:
            dictionaries.prepare(self.index, api=lambda lock: metadata(lock))
        self.assertEqual(download.call_count, 1)
        self.assertTrue(self.index.is_file())


class SerializedDictionaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.java = os.environ.get("DICTIONARY_JAVA") or shutil.which("java")
        if not cls.java:
            raise RuntimeError("Java 17 is required for dictionary decoder tests")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.asset, self.rows, self.report = [self.directory / name for name in ("asset.zip", "rows.tsv", "report.json")]

    def fixture(self, mode="valid"):
        subprocess.run([self.java, "-Dfile.encoding=UTF-8", str(Path(__file__).with_name("DictionaryFixture.java")),
                        str(self.asset), mode], check=True, capture_output=True, timeout=30)

    def decode(self):
        return subprocess.run([self.java, "-Xmx512m", "-Dfile.encoding=UTF-8", str(dictionaries.DECODER),
                               str(self.asset), str(self.rows), str(self.report)], capture_output=True, timeout=30)

    def test_all_pack_types_and_kana_sentinels_agree_with_conversion_outputs(self):
        self.fixture()
        result = self.decode()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(self.report.read_text())["packs"], {pack: 5 for pack in dictionaries.PACKS})
        values = [(pack, base64.b64decode(reading).decode(), base64.b64decode(word).decode())
                  for pack, reading, word in (line.split("\t") for line in self.rows.read_text().splitlines())]
        self.assertIn(("english_reading", "かな", "カナ"), values)
        self.assertIn(("system", "かな", "かな"), values)
        self.assertIn(("places", "かな", "東京"), values)
        self.assertIn(("reading_correction", "かな", "補正語"), values)
        self.assertIn(("emoji", "かな", "🇦🇨"), values)

    def test_legacy_postings_at_64_bit_boundary_are_fully_decoded(self):
        self.fixture("boundary")
        result = self.decode()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(len(self.rows.read_text().splitlines()), 63 * 13)

    def test_missing_packs_corrupt_postings_and_unknown_sentinels_fail_closed(self):
        for mode in ("missing", "postings", "sentinel"):
            self.report.unlink(missing_ok=True)
            self.fixture(mode)
            result = self.decode()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
