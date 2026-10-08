"""Replace the single New-word ZIP after canonical data has been pushed."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import release_archive as data
from collect_release_words import summary

REPOSITORY = "KazumaProject/New-word"
TAG = "new-words"
ASSET = "new-words.zip"
TITLE = "日本語IME候補（累積）"


class APIError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GitHub:
    def api(self, method, path, payload=None):
        if not path.startswith(f"/repos/{REPOSITORY}/"):
            raise ValueError("Release publisher is restricted to New-word")
        command = ["gh", "api", "--method", method, path, "-H", "Accept: application/vnd.github+json"]
        if payload is not None:
            command += ["--input", "-"]
        result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                text=True, encoding="utf-8", capture_output=True, check=False)
        if result.returncode:
            match = re.search(r"HTTP (\d{3})", result.stderr)
            raise APIError(int(match[1]) if match else None, result.stderr.strip())
        return json.loads(result.stdout) if result.stdout.strip() else None

    def upload(self, path):
        subprocess.run(["gh", "release", "upload", TAG, str(path), "--repo", REPOSITORY, "--clobber"], check=True)

    def download(self, path):
        subprocess.run(["gh", "release", "download", TAG, "--repo", REPOSITORY, "--pattern", ASSET,
                        "--dir", str(path.parent), "--clobber"], check=True)


def release_state(client):
    try:
        release = client.api("GET", f"/repos/{REPOSITORY}/releases/tags/{TAG}")
    except APIError as error:
        if error.status != 404:
            raise
        # This admin-read endpoint can be unavailable to a token. Fail on an
        # explicit permission error rather than changing repository settings.
        try:
            setting = client.api("GET", f"/repos/{REPOSITORY}/immutable-releases")
        except APIError as setting_error:
            if setting_error.status != 404:
                raise RuntimeError("新規Releaseのimmutable設定を確認できません。可変Releaseを確認して作成してください。") from setting_error
        else:
            if setting.get("enabled"):
                raise RuntimeError("immutable Releaseが有効です。同じAssetを更新できないため公開を停止します。")
        return None
    if release.get("immutable") is not False:
        raise RuntimeError("Releaseがimmutable、または更新可否が不明です。公開を停止します。")
    assets = client.api("GET", f"/repos/{REPOSITORY}/releases/{release['id']}/assets?per_page=100")
    if len(assets) > 1 or any(asset["name"] != ASSET for asset in assets):
        raise RuntimeError("new-words Releaseに別のAssetがあります。既存ファイルは削除しません。")
    return {**release, "assets": assets}


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_matches(asset, path, client):
    if asset.get("state") != "uploaded" or asset.get("size") != path.stat().st_size:
        return False
    expected = file_sha256(path)
    if asset.get("digest"):
        return asset["digest"] == f"sha256:{expected}"
    # Older assets may not expose a server digest. Verify their bytes instead.
    with tempfile.TemporaryDirectory(prefix="new-words-verify-") as directory:
        downloaded = Path(directory) / ASSET
        client.download(downloaded)
        return file_sha256(downloaded) == expected


def notes(manifest, archive_sha):
    return "\n".join([
        "確認済みの日本語使用例・読みを持つIME候補の累積データです。",
        "v1.7.256の全13パックに同じ表記の登録がない語を配布します。辞書登録、品詞ID、変換コストの設定は別工程です。", "",
        "カタカナ化や複数語を組み合わせたIME変換の可否は、この照合の対象に含みません。", "",
        f"- 語数: {manifest['word_count']}", f"- 更新日時: {manifest['updated_at']}",
        f"- 元データ: [{manifest['source_commit'][:12]}](https://github.com/{REPOSITORY}/commit/{manifest['source_commit']})",
        f"- ZIP SHA-256: `{archive_sha}`", "",
        "new-words.zipを展開し、manifest.jsonが列挙するdictionary-*.tsvを使用してください。",
        "TSVはヘッダーなしの読み・表記・品詞ラベルの3列です。根拠はmetadata-*.jsonlに保存しています。",
        "GitHubが自動表示するSource codeアーカイブは辞書用配布ファイルではありません。",
    ]) + "\n"


def publish(path, *, client=None, force=False):
    client = GitHub() if client is None else client
    if path.name != ASSET:
        raise ValueError(f"The permanent asset filename must be {ASSET}")
    manifest = data.validate_archive(path)
    release = release_state(client)
    body = notes(manifest, file_sha256(path))
    if release is None:
        release = client.api("POST", f"/repos/{REPOSITORY}/releases", {
            "tag_name": TAG, "target_commitish": manifest["source_commit"], "name": TITLE,
            "body": body, "draft": True, "make_latest": "false",
        })
        release = release_state(client)
        if release is None:
            raise RuntimeError("Created Release could not be read back")
    asset = release["assets"][0] if release["assets"] else None
    unchanged = bool(asset and asset_matches(asset, path, client))
    if force or not unchanged:
        # --clobber deletes first. Git remains authoritative if upload fails.
        client.upload(path)
        release = release_state(client)
        if release is None or len(release["assets"]) != 1 or not asset_matches(release["assets"][0], path, client):
            raise RuntimeError("配布したZIPの整合性を確認できません。Gitから再配布してください。")
    if release.get("body") != body or release.get("draft"):
        release = client.api("PATCH", f"/repos/{REPOSITORY}/releases/{release['id']}",
                             {"name": TITLE, "body": body, "draft": False, "make_latest": "false"})
    if release.get("immutable") is not False:
        raise RuntimeError("Releaseがimmutableになりました。次回更新できないため設定を確認してください。")
    url = release.get("html_url", f"https://github.com/{REPOSITORY}/releases/tag/{TAG}")
    summary(["## 新語候補Release", f"累積: {manifest['word_count']}語 / 辞書照合: v1.7.256・全13パック確認済み",
             "内容変更なし。ZIP置換を省略しました。" if unchanged and not force else "ZIPを検証して配布しました。", url])
    return url


def ensure_pushed(client):
    # Guard local CLI use as well as the workflow's push-before-publish ordering.
    tracked = ["data/release", "data/categories.json", "data/readings.json", "scripts/release_archive.py",
               "scripts/release_candidates.py", "scripts/candidate_pipeline.py", "scripts/collect_release_words.py",
               "scripts/publish_word_release.py", "data/dictionary-release.json",
               "scripts/dictionary_assets.py", "scripts/DecodeDictionary.java"]
    status = subprocess.check_output(["git", "status", "--porcelain", "--", *tracked], cwd=data.ROOT, text=True)
    if status.strip():
        raise RuntimeError("Release用データ・コードをGitへ保存してpushしてから配布してください。")
    commit, _ = data.data_revision()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=data.ROOT, text=True).strip()
    client.api("GET", f"/repos/{REPOSITORY}/commits/{head}")
    if head != commit:
        client.api("GET", f"/repos/{REPOSITORY}/commits/{commit}")
    return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only mutable Release preflight")
    parser.add_argument("--archive", type=Path, default=data.ROOT / "dist" / ASSET)
    parser.add_argument("--force", action="store_true", help="Re-upload even when the ZIP is unchanged")
    args = parser.parse_args()
    try:
        if os.environ.get("GITHUB_REPOSITORY", REPOSITORY) != REPOSITORY:
            raise ValueError("Release publication is restricted to KazumaProject/New-word")
        client = GitHub()
        if args.check:
            release_state(client)
            print("Mutable Release preflight passed")
        else:
            commit = ensure_pushed(client)
            with data.dictionaries.DictionaryIndex.from_environment() as dictionary:
                if data.validate_archive(args.archive, dictionary=dictionary)["source_commit"] != commit:
                    raise ValueError("ZIP is not built from the saved data revision")
            publish(args.archive, client=client, force=args.force)
        return 0
    except Exception as error:
        print(f"Release publication failed: {error}", file=sys.stderr)
        summary(["## Release配布: 失敗", f"理由: {error}", "保存済みのGitデータから再配布できます。"])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
