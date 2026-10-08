import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from release_support import data, collector, candidate
from test_release_archive import COMMIT, STAMP
import publish_word_release as publisher


class FakeRelease:
    def __init__(self):
        self.release = None
        self.raw = None
        self.calls = []
        self.uploads = 0
        self.downloads = 0
        self.immutable_setting = False
        self.setting_denied = False
        self.fail_upload = False
        self.lose_create_response = False
        self.lose_upload_response = False
        self.corrupt_digest = False

    def api(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        base = f"/repos/{publisher.REPOSITORY}/"
        if not path.startswith(base):
            raise AssertionError("Other repository access")
        if method == "GET" and path.endswith("immutable-releases"):
            if self.setting_denied:
                raise publisher.APIError(403, "Forbidden")
            if self.immutable_setting:
                return {"enabled": True}
            raise publisher.APIError(404, "Disabled")
        if method == "GET" and path.endswith(f"releases/tags/{publisher.TAG}"):
            if self.release is None:
                raise publisher.APIError(404, "No Release")
            return copy.deepcopy(self.release)
        if method == "GET" and "/assets?" in path:
            return copy.deepcopy(self.release["assets"])
        if method == "POST" and path.endswith("releases"):
            if self.release is not None:
                raise AssertionError("Duplicate Release")
            self.release = {"id": 1, "immutable": False, "assets": [], **payload,
                            "html_url": f"https://github.com/{publisher.REPOSITORY}/releases/tag/{publisher.TAG}"}
            if self.lose_create_response:
                self.lose_create_response = False
                raise publisher.APIError(None, "Lost create response")
            return copy.deepcopy(self.release)
        if method == "PATCH" and path.endswith("releases/1"):
            self.release.update(payload)
            return copy.deepcopy(self.release)
        raise AssertionError((method, path))

    def upload(self, path):
        self.uploads += 1
        self.release["assets"] = []
        if self.fail_upload:
            raise RuntimeError("Upload failed after old asset deletion")
        self.raw = path.read_bytes()
        self.release["assets"] = [{"name": publisher.ASSET, "state": "uploaded", "size": len(self.raw),
                                   "digest": "sha256:" + ("0" * 64 if self.corrupt_digest else data.sha256(self.raw))}]
        if self.lose_upload_response:
            self.lose_upload_response = False
            raise RuntimeError("Upload succeeded but response was lost")

    def download(self, path):
        self.downloads += 1
        path.write_bytes(self.raw)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / publisher.ASSET
        self.rows = data.load_entries()
        self.client = FakeRelease()
        self.build()
        manager = contextlib.redirect_stdout(io.StringIO())
        manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)

    def build(self):
        data.build_archive(self.rows, self.output, source_commit=COMMIT, updated_at=STAMP)

    def publish(self, **kwargs):
        return publisher.publish(self.output, client=self.client, **kwargs)

    def test_first_publication_creates_one_release_and_one_verified_zip(self):
        self.publish()
        self.assertFalse(self.client.release["draft"])
        self.assertEqual(self.client.release["tag_name"], "new-words")
        self.assertEqual([asset["name"] for asset in self.client.release["assets"]], ["new-words.zip"])
        self.assertEqual(sum(method == "POST" for method, _, _ in self.client.calls), 1)
        self.assertTrue(all("/issues" not in path and "converter" not in path for _, path, _ in self.client.calls))

    def test_unchanged_run_skips_asset_and_release_writes(self):
        self.publish()
        count = len(self.client.calls)
        self.publish()
        self.assertEqual(self.client.uploads, 1)
        self.assertTrue(all(method == "GET" for method, _, _ in self.client.calls[count:]))

    def test_metadata_change_or_new_word_replaces_same_asset_cumulatively(self):
        self.publish()
        self.rows[0]["manual_notes"] = "保存するメモ"
        self.rows += [candidate("追加の語彙")]
        self.build()
        self.publish()
        self.assertEqual(self.client.uploads, 2)
        self.assertEqual(len(self.client.release["assets"]), 1)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.client.calls), 1)
        self.assertEqual(data.validate_archive(self.output)["word_count"], 11)

    def test_upload_failure_recovers_from_git_without_a_second_release(self):
        self.publish()
        canonical = (data.DATA_DIR / "entries-0001.jsonl").read_bytes()
        self.rows += [candidate("復旧する語彙")]
        self.build()
        self.client.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, "Upload failed"):
            self.publish()
        self.assertEqual(self.client.release["assets"], [])
        self.assertEqual((data.DATA_DIR / "entries-0001.jsonl").read_bytes(), canonical)
        self.client.fail_upload = False
        self.publish()
        self.assertEqual(len(self.client.release["assets"]), 1)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.client.calls), 1)

    def test_lost_create_or_upload_response_is_idempotently_recovered(self):
        self.client.lose_create_response = True
        with self.assertRaises(publisher.APIError):
            self.publish()
        self.client.lose_upload_response = True
        with self.assertRaisesRegex(RuntimeError, "response was lost"):
            self.publish()
        self.publish()
        self.assertEqual(self.client.uploads, 1)
        self.assertFalse(self.client.release["draft"])
        self.assertEqual(sum(method == "POST" for method, _, _ in self.client.calls), 1)

    def test_existing_immutable_release_blocks_publication(self):
        self.publish()
        self.client.release["immutable"] = True
        count = len(self.client.calls)
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            self.publish(force=True)
        self.assertEqual(self.client.uploads, 1)
        self.assertTrue(all(method == "GET" for method, _, _ in self.client.calls[count:]))

    def test_enabled_or_unknown_immutability_blocks_first_publication(self):
        for denied in (False, True):
            client = FakeRelease()
            client.immutable_setting = True
            client.setting_denied = denied
            with self.assertRaises(RuntimeError):
                publisher.publish(self.output, client=client)
            self.assertIsNone(client.release)
            self.assertTrue(all(method == "GET" for method, _, _ in client.calls))

    def test_failed_digest_verification_never_reports_publication_success(self):
        self.client.corrupt_digest = True
        with self.assertRaisesRegex(RuntimeError, "整合性"):
            self.publish()
        self.assertTrue(self.client.release["draft"])
        self.client.corrupt_digest = False
        self.publish()
        self.assertFalse(self.client.release["draft"])

    def test_unknown_asset_is_preserved_and_blocks_replacement(self):
        self.publish()
        self.client.release["assets"].append({"name": "manual-notes.txt"})
        with self.assertRaisesRegex(RuntimeError, "別のAsset"):
            self.publish()
        self.assertEqual(self.client.uploads, 1)
        self.assertEqual(len(self.client.release["assets"]), 2)

    def test_legacy_asset_without_server_digest_is_verified_by_download(self):
        self.publish()
        self.client.release["assets"][0]["digest"] = None
        self.publish()
        self.assertEqual(self.client.downloads, 1)
        self.assertEqual(self.client.uploads, 1)

    def test_manual_rebuild_forces_one_asset_replacement(self):
        self.publish()
        self.publish(force=True)
        self.assertEqual(self.client.uploads, 2)
        self.assertEqual(len(self.client.release["assets"]), 1)

    def test_invalid_archive_is_rejected_before_github_access(self):
        self.output.write_bytes(b"corrupt")
        with self.assertRaises(Exception):
            self.publish()
        self.assertFalse(self.client.calls)

    def test_dirty_data_or_failed_remote_commit_check_blocks_publication(self):
        with patch.object(publisher.subprocess, "check_output", return_value=" M data/release/entries-0001.jsonl"):
            with self.assertRaisesRegex(RuntimeError, "push"):
                publisher.ensure_pushed(self.client)
        self.assertFalse(self.client.calls)
        with patch.object(publisher.subprocess, "check_output", return_value=""), patch.object(data, "data_revision", return_value=(COMMIT, STAMP)):
            with self.assertRaises(AssertionError):
                publisher.ensure_pushed(self.client)
        self.assertEqual(self.client.uploads, 0)

    def test_publication_cli_refuses_a_fork(self):
        with patch.dict(publisher.os.environ, {"GITHUB_REPOSITORY": "kazuma-naka/New-word"}), patch.object(publisher.sys, "argv", ["publisher", "--check"]):
            self.assertEqual(publisher.main(), 1)


class LegacyIssueCompatibilityTests(unittest.TestCase):
    def test_v3_issue_six_and_legacy_v1_are_read_without_issue_edits(self):
        from test_collect_new_words import collector as legacy, issue, candidate as legacy_row
        body = (data.ROOT / "lists/2026-10-07.md").read_text(encoding="utf-8")
        with patch.object(legacy, "github_api", side_effect=AssertionError("Read-only parser")):
            rows = legacy.issue_rows({"number": 6, "title": "新語候補 2026-10-07", "body": body})
            old = legacy.issue_rows(issue(1, [legacy_row("旧形式の語彙")]))
        self.assertEqual(len(rows), 10)
        self.assertEqual(len(old), 1)
        self.assertEqual(rows[0]["dictionary_check"], {"status": "not_checked"})


if __name__ == "__main__":
    unittest.main()
