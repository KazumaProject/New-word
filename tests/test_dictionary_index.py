import copy
import csv
import gzip
import hashlib
import io
import json
import unittest
import zipfile

from test_collect_new_words import collector
from dictionary_index import DictionaryIndex, DictionaryIndexError, PACK_PATHS, ROOT, ZIPPED, select_release

OPTIONAL_WORDS = {"person_name": "音羽ひかり", "places": "緑泉駅", "neologd": "推論語彙", "english_reading": "Cloud Nova"}


def fixture():
    id_def = "0 BOS/EOS,*,*,*,*,*,*\n1 名詞,一般,*,*,*,*,*\n".encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(ROOT + "id.def", id_def)
        for pack, (directory, suffix) in PACK_PATHS.items():
            for part in ("yomi", "tango", "token"):
                extension = ".dat.zip" if pack in ZIPPED else ".dat"
                bundle.writestr(ROOT + f"{directory}/{part}{suffix}{extension}", b"fixture")
    bundle = output.getvalue()
    text = io.StringIO()
    writer = csv.writer(text, delimiter="\t", lineterminator="\n")
    writer.writerow(["dictionary", "reading", "word"])
    for pack in PACK_PATHS:
        writer.writerow([pack, "にっぽんばし" if pack == "wiki" else "にほんばし", OPTIONAL_WORDS.get(pack, "日本橋")])
    writer.writerow(["wiki", "くらうど", "Ｃｌｏｕｄ  Nova"])
    writer.writerow(["reading_correction", "もじ", '語"句'])
    archive = gzip.compress(text.getvalue().encode())
    counts = {pack: 1 + (pack in {"wiki", "reading_correction"}) for pack in PACK_PATHS}
    manifest = {"schemaVersion": 1, "dictionaryRelease": "v-test", "converterCommit": "a" * 40, "mozcCommit": "b" * 40,
                "idDefSha256": hashlib.sha256(id_def).hexdigest(), "posIds": {"BOS/EOS,*,*,*,*,*,*": 0, "名詞,一般,*,*,*,*,*": 1},
                "packs": counts, "index": {"name": "dictionary-index.tsv.gz", "sha256": hashlib.sha256(archive).hexdigest(), "rows": sum(counts.values())},
                "assets": {"name": "japanese_keyboard_dictionary_assets.zip", "sha256": hashlib.sha256(bundle).hexdigest()}}
    return manifest, archive, bundle


class DictionaryIndexTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.archive, self.bundle = fixture()
    def load(self):
        return DictionaryIndex("KazumaProject/kotlin-kana-kanji-converter", {"tag_name": "v-test"}, json.dumps(self.manifest).encode(), self.archive, self.bundle)
    def test_optional_packs_alternate_readings_and_normalized_forms(self):
        with self.load() as index:
            self.assertTrue(index.contains("日本橋"))
            self.assertTrue(index.contains("cloud   nova"))
            self.assertTrue(index.contains('語"句'))
            self.assertFalse(index.contains("日本橋駅"))
            for word in OPTIONAL_WORDS.values():
                self.assertTrue(index.contains(word))
            self.assertEqual(index.pos_ids["名詞,一般,*,*,*,*,*"], 1)
            self.assertEqual(len(index.verification()["manifest_sha256"]), 64)
    def test_checksums_release_provenance_and_pos_mapping_are_required(self):
        for mutate in (
            lambda m: m.update(dictionaryRelease="wrong"),
            lambda m: m.update(schemaVersion=99),
            lambda m: m.update(mozcCommit="master"),
            lambda m: m["index"].update(sha256="0" * 64),
            lambda m: m["assets"].update(sha256="0" * 64),
            lambda m: m["posIds"].update({"名詞,一般,*,*,*,*,*": 123}),
            lambda m: m["posIds"].update({"名詞,一般,*,*,*,*,*": True}),
            lambda m: m["packs"].pop("english_reading"),
        ):
            with self.subTest(mutate=mutate):
                self.manifest = fixture()[0]
                mutate(self.manifest)
                with self.assertRaises(DictionaryIndexError):
                    self.load()
    def test_truncated_index_is_not_valid_coverage_even_with_updated_hash(self):
        self.archive = gzip.compress(b"dictionary\treading\tword\nsystem\ta\tb\n")
        self.manifest["index"]["sha256"] = hashlib.sha256(self.archive).hexdigest()
        with self.assertRaises(DictionaryIndexError):
            self.load()
    def test_unknown_pack_is_rejected(self):
        self.manifest["packs"]["unexpected"] = 1
        with self.assertRaises(DictionaryIndexError):
            self.load()
    def test_corrupt_gzip_and_missing_binary_pack_cannot_support_lookup(self):
        original = self.archive
        for broken in (b"invalid gzip", original[:-8]):
            with self.subTest(index=broken[:12]):
                self.archive = broken
                self.manifest["index"]["sha256"] = hashlib.sha256(broken).hexdigest()
                with self.assertRaises(DictionaryIndexError):
                    self.load()
        self.manifest, self.archive, self.bundle = fixture()
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.bundle)) as source, zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                if "english_reading/" not in name:
                    target.writestr(name, source.read(name))
        self.bundle = output.getvalue()
        self.manifest["assets"]["sha256"] = hashlib.sha256(self.bundle).hexdigest()
        with self.assertRaises(DictionaryIndexError):
            self.load()
    def test_newest_version_without_export_does_not_fall_back_to_older_index(self):
        releases = [{"tag_name": "dictionary-metadata-snapshot"}, {"tag_name": "v-new", "assets": []}, {"tag_name": "v-old", "assets": ["index"]}]
        api = lambda *_: releases
        self.assertEqual(select_release(api, "KazumaProject/kotlin-kana-kanji-converter")["tag_name"], "v-new")
        with self.assertRaises(DictionaryIndexError):
            DictionaryIndex.fetch(api, lambda *_: self.fail("must not download"), "KazumaProject/kotlin-kana-kanji-converter")
    def test_downloads_all_three_files_once_from_the_same_release(self):
        repository = "KazumaProject/kotlin-kana-kanji-converter"
        payloads = {"dictionary-index-manifest.json": json.dumps(self.manifest).encode(), "dictionary-index.tsv.gz": self.archive, "japanese_keyboard_dictionary_assets.zip": self.bundle}
        release = {"tag_name": "v-test", "assets": [{"name": name, "size": len(data), "browser_download_url": f"https://github.com/{repository}/releases/download/v-test/{name}"} for name, data in payloads.items()]}
        calls = []
        def request(url, **kwargs):
            calls.append(url)
            return payloads[url.rsplit('/', 1)[-1]]
        with DictionaryIndex.fetch(lambda *_: [release], request, repository) as index:
            self.assertTrue(index.contains("日本橋"))
        self.assertEqual(len(calls), 3)
        release["assets"][0]["browser_download_url"] = release["assets"][0]["browser_download_url"].replace("v-test/", "other/")
        with self.assertRaises(DictionaryIndexError):
            DictionaryIndex.fetch(lambda *_: [release], request, repository)
