import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from release_support import collector, data, candidate

COMMIT = "a" * 40
STAMP = "2026-10-08T23:00:00Z"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "new-words.zip"
        self.rows = data.load_entries()

    def build(self, rows=None, **options):
        return data.build_archive(self.rows if rows is None else rows, self.output,
                                  source_commit=COMMIT, updated_at=STAMP, **options)

    def rewrite(self, updates):
        with zipfile.ZipFile(self.output) as archive:
            payloads = {name: archive.read(name) for name in archive.namelist()}
        payloads.update(updates)
        with zipfile.ZipFile(self.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in payloads.items():
                archive.writestr(name, raw)

    def test_initial_ten_match_reviewed_seed_and_export_only_three_fields(self):
        seed = json.loads((data.ROOT / "data/lists/2026-10-07.json").read_text(encoding="utf-8"))
        self.assertEqual([(row["reading"], row["word"], row["pos"]) for row in self.rows],
                         [(row["reading"], row["word"], row["pos"]) for row in seed["rows"]])
        manifest = self.build()
        self.assertEqual(manifest["word_count"], 10)
        self.assertEqual(len(manifest["category_counts"]), 9)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), {"dictionary-0001.tsv", "metadata-0001.jsonl", "manifest.json"})
            raw = archive.read("dictionary-0001.tsv")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"http", raw)
            self.assertNotIn(b"\r", raw)
            lines = raw.decode("utf-8").splitlines()
            self.assertEqual(len(lines), 10)
            self.assertEqual([line.split("\t") for line in lines], [[row[field] for field in data.COLUMNS] for row in self.rows])
            metadata = [json.loads(line) for line in archive.read("metadata-0001.jsonl").splitlines()]
            self.assertEqual(metadata, self.rows)
        self.assertEqual(data.validate_archive(self.output), manifest)

    def test_identical_builds_are_deterministic(self):
        self.build()
        first = self.output.read_bytes()
        self.build()
        self.assertEqual(first, self.output.read_bytes())

    def test_partition_boundary_pairs_rows_without_splitting_utf8(self):
        rows = [candidate("試験語一"), candidate("試験語二"), candidate("試験語三")]
        limit = max(len(data.json_bytes(row)) for row in rows)
        manifest = self.build(rows, part_bytes=limit)
        self.assertEqual(len(manifest["files"]), 6)
        self.assertTrue(all(item["bytes"] <= limit and item["rows"] == 1 for item in manifest["files"]))
        data.save_entries(rows, self.directory / "canonical", part_bytes=limit)
        self.assertEqual(data.load_entries(self.directory / "canonical"), rows)
        self.assertEqual(data.validate_archive(self.output)["word_count"], 3)

    def test_exact_32_mib_part_rolls_the_next_record_to_new_part(self):
        first = {**candidate("大きな語彙"), "manual_notes": ""}
        overhead = len(data.json_bytes(first))
        first["manual_notes"] = "a" * (data.PART_BYTES - overhead)
        iterator = data.parts([first, candidate("次の語彙")])
        index, _, metadata, count = next(iterator)
        self.assertEqual((index, len(metadata), count), (1, 32 * 1024 * 1024, 1))
        self.assertEqual(next(iterator)[0], 2)

    def test_single_record_cannot_exceed_part_limit(self):
        with self.assertRaisesRegex(ValueError, "single candidate"):
            self.build(part_bytes=20)
        self.assertFalse(self.output.exists())

    def test_zip_size_limit_is_strict_and_invalid_output_is_removed(self):
        with self.assertRaisesRegex(ValueError, "2 GiB"):
            self.build(zip_bytes=1)
        self.assertFalse(self.output.exists())

    def test_metadata_or_tsv_corruption_fails_integrity_verification(self):
        self.build()
        self.rewrite({"dictionary-0001.tsv": b"broken\n"})
        with self.assertRaisesRegex(ValueError, "integrity"):
            data.validate_archive(self.output)

    def test_valid_checksums_cannot_hide_disagreement_with_metadata(self):
        manifest = self.build()
        with zipfile.ZipFile(self.output) as archive:
            raw = archive.read("dictionary-0001.tsv").replace("グリークヨーグルト".encode(), "別の語彙".encode())
        entry = manifest["files"][0]
        entry.update(bytes=len(raw), sha256=data.sha256(raw))
        manifest["data_sha256"] = data.content_digest(manifest)
        self.rewrite({entry["name"]: raw, "manifest.json": data.json_bytes(manifest)})
        with self.assertRaisesRegex(ValueError, "disagrees with metadata"):
            data.validate_archive(self.output)

    def test_missing_members_and_corrupt_manifest_are_rejected(self):
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            manifest = archive.read("manifest.json")
        with zipfile.ZipFile(self.output, "w") as archive:
            archive.writestr("manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "ZIP members"):
            data.validate_archive(self.output)

    def test_conflicting_estimated_or_unchecked_evidence_is_not_exportable(self):
        for update in ({"reading_status": "conflict"}, {"reading_status": "unconfirmed", "reading_method": "estimate"},
                       {"usage_status": "unconfirmed"}, {"dictionary_check": {"status": "missing"}},
                       {"sources": [candidate("試験語彙")["sources"][0]]}, {"reading": "カタカナ"},
                       {"reading": "試験語彙"}, {"word": "語\t彙"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.build([{**candidate("試験語彙"), **update}])

    def test_duplicate_normalized_spellings_and_alternate_readings_are_rejected(self):
        first = candidate("Cloud Nova")
        second = {**candidate("ＣＬＯＵＤ　ＮＯＶＡ"), "reading": "べつのよみ"}
        with self.assertRaisesRegex(ValueError, "Duplicate normalized"):
            self.build([first, second])

    def test_named_entity_requires_official_source_and_correct_pos(self):
        row = candidate("山田音羽", kind="person")
        self.build([row])
        with self.assertRaisesRegex(ValueError, "official"):
            self.build([{**row, "official_name_sources": []}])
        with self.assertRaisesRegex(ValueError, "POS"):
            self.build([{**row, "pos": collector.pos_label(row["word"], "common")}])

    def test_manual_notes_and_all_category_tags_survive_canonical_roundtrip(self):
        row = candidate("試験語彙", categories=["life_food", "technology"])
        row["manual_notes"] = "手動メモ\n次回確認"
        data.save_entries([row], self.directory / "canonical")
        self.assertEqual(data.load_entries(self.directory / "canonical"), [row])
        self.build([row])
        self.assertEqual(data.validate_archive(self.output)["category_counts"], {"life_food": 1, "technology": 1})

    def test_invalid_canonical_update_does_not_destroy_previous_data(self):
        directory = self.directory / "canonical"
        data.save_entries(self.rows, directory)
        before = (directory / "entries-0001.jsonl").read_bytes()
        with self.assertRaises(ValueError):
            data.save_entries(self.rows + [self.rows[0]], directory)
        self.assertEqual((directory / "entries-0001.jsonl").read_bytes(), before)

    def test_reading_annotations_ruby_conflicts_and_estimates(self):
        self.assertEqual(collector.extract_readings("試験語彙", "<ruby>試験語彙<rt>しけんごい</rt></ruby>"), {"しけんごい"})
        self.assertEqual(collector.extract_readings("Name", "Name（ネーム）"), {"ねーむ"})
        self.assertEqual(collector.extract_readings("Name", "SuperName（スーパーネーム）"), set())
        with patch.object(collector, "READINGS_PATH", self.directory / "readings.json"):
            with patch.object(collector, "public_reading_document", side_effect=["Name（ネーム）", "Name（ナメ）"]):
                result = collector.ReadingResolver().resolve("Name", candidate("Name")["sources"])
                self.assertEqual(result["reading_status"], "conflict")
            with patch.object(collector, "public_reading_document", return_value=""):
                result = collector.ReadingResolver().resolve("TDK", candidate("TDK")["sources"])
                self.assertEqual(result["reading_status"], "unconfirmed")
                self.assertEqual(result["reading"], "要確認")
                self.assertEqual(result["reading_estimate"], "てぃーでぃーけー")


if __name__ == "__main__":
    unittest.main()
