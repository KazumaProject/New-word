"""Read-only verification of every word pack in the pinned converter Release."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import contextlib
import datetime as dt
from functools import cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

import release_candidates as news

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "data/dictionary-release.json"
DECODER = ROOT / "scripts/DecodeDictionary.java"
PACKS = {"emoji", "emoticon", "english_reading", "kotowaza", "neologd", "person_name",
         "places", "reading_correction", "single_kanji", "symbol", "system", "web", "wiki"}


@cache
def load_lock():
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if (lock.get("schema_version") != 1 or lock.get("repository") != "KazumaProject/kotlin-kana-kanji-converter"
            or lock.get("release") != "v1.7.256" or lock.get("asset") != "japanese_keyboard_dictionary_assets.zip"
            or not re.fullmatch(r"[0-9a-f]{64}", lock.get("sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", lock.get("outputs_sha256", ""))
            or not isinstance(lock.get("bytes"), int) or not 0 < lock["bytes"] < 128 * 1024 * 1024
            or set(lock.get("packs", {})) != PACKS
            or any(type(count) is not int or count <= 0 for count in lock["packs"].values())):
        raise ValueError("Invalid pinned dictionary Release or incomplete pack coverage")
    return lock


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@cache
def provenance():
    lock = load_lock()
    # Git checkouts may use CRLF on Windows. Hash the same source on both OSes.
    decoder_sha = hashlib.sha256(DECODER.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return {"repository": lock["repository"], "release": lock["release"], "asset": lock["asset"],
            "asset_sha256": lock["sha256"], "decoder_sha256": decoder_sha,
            "outputs_sha256": lock["outputs_sha256"],
            "packs": lock["packs"], "token_count": sum(lock["packs"].values()),
            "method": "decoded_outputs", "scope": "written_form_any_reading"}


def validate_check(check, *, allow_unchecked=False):
    if allow_unchecked and check == {"status": "not_checked"}:
        return
    if not isinstance(check, dict) or check.get("status") not in {"missing", "present"}:
        raise ValueError("Dictionary membership has not been verified")
    expected = {**provenance(), "status": check["status"], "checked_at": check.get("checked_at")}
    if check != expected:
        raise ValueError("Dictionary Release integrity or coverage is unverified")
    day = check.get("checked_at")
    if not isinstance(day, str) or dt.date.fromisoformat(day).isoformat() != day:
        raise ValueError("Invalid dictionary verification date")


def release_metadata(lock):
    # GET only. This module has no API capable of changing the converter repo.
    result = subprocess.run(["gh", "api", f"repos/{lock['repository']}/releases/tags/{lock['release']}"],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def verified_asset(metadata, lock):
    matches = [asset for asset in metadata.get("assets", []) if asset.get("name") == lock["asset"]]
    url = f"https://github.com/{lock['repository']}/releases/download/{lock['release']}/{lock['asset']}"
    if metadata.get("tag_name") != lock["release"] or len(matches) != 1:
        raise ValueError("Pinned dictionary Release asset is missing or ambiguous")
    asset = matches[0]
    if (asset.get("size") != lock["bytes"] or asset.get("digest") != "sha256:" + lock["sha256"]
            or asset.get("browser_download_url") != url):
        raise ValueError("Dictionary Release asset integrity cannot be verified")
    return asset


def decode(asset, rows, report):
    subprocess.run([os.environ.get("DICTIONARY_JAVA", "java"), "-Xmx2g", "-Dfile.encoding=UTF-8",
                    str(DECODER), str(asset), str(rows), str(report)], check=True, timeout=300)


def index_rows(rows_path, report, output, *, expected=None):
    """Only install an index after all decoded output counts agree with the lock."""
    expected = provenance() if expected is None else expected
    if report != {"packs": expected["packs"]}:
        raise ValueError("Decoded dictionary pack coverage disagrees with the pinned Release")
    if file_sha256(rows_path) != expected["outputs_sha256"]:
        raise ValueError("Decoded outputs disagree with the verified converter output checksum")
    counts = Counter()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="new-word-index-", dir=output.parent) as temporary:
        database = Path(temporary) / "index.sqlite"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; "
                "CREATE TABLE words (normalized TEXT PRIMARY KEY) WITHOUT ROWID; "
                "CREATE TABLE metadata (document TEXT NOT NULL);")
            batch = []
            with rows_path.open(encoding="ascii", newline="") as stream:
                for line in stream:
                    fields = line.rstrip("\n").split("\t")
                    if len(fields) != 3 or not line.endswith("\n") or line.endswith("\r\n"):
                        raise ValueError("Invalid decoded dictionary record")
                    pack, raw_reading, raw_word = fields
                    reading = base64.b64decode(raw_reading, validate=True).decode("utf-8")
                    word = base64.b64decode(raw_word, validate=True).decode("utf-8")
                    key = news.normalize(word)
                    if pack not in expected["packs"] or not reading.strip() or not key:
                        raise ValueError("Invalid decoded dictionary output")
                    counts[pack] += 1
                    batch.append((key,))
                    if len(batch) >= 5000:
                        connection.executemany("INSERT OR IGNORE INTO words VALUES (?)", batch)
                        batch.clear()
            connection.executemany("INSERT OR IGNORE INTO words VALUES (?)", batch)
            if dict(counts) != expected["packs"] or sum(counts.values()) != expected["token_count"]:
                raise ValueError("Incomplete decoded dictionary outputs")
            unique = connection.execute("SELECT COUNT(*) FROM words").fetchone()[0]
            for control in ("東京", "ヨーグルト"):
                if not connection.execute("SELECT 1 FROM words WHERE normalized=?", (news.normalize(control),)).fetchone():
                    raise ValueError("Known dictionary output control failed")
            document = {"schema_version": 1, "provenance": expected, "word_count": unique,
                        "outputs_sha256": expected["outputs_sha256"]}
            connection.execute("INSERT INTO metadata VALUES (?)", (json.dumps(document, ensure_ascii=False),))
            connection.commit()
        database.replace(output)
    output.with_suffix(output.suffix + ".sha256").write_text(file_sha256(output) + "\n", encoding="ascii")


def prepare(output, *, asset_path=None, api=None, download=None):
    lock = load_lock()
    metadata = (release_metadata if api is None else api)(lock)
    asset = verified_asset(metadata, lock)
    with tempfile.TemporaryDirectory(prefix="new-word-dictionary-") as temporary:
        directory = Path(temporary)
        path = directory / lock["asset"]
        if asset_path is None:
            fetch = news.request if download is None else download
            path.write_bytes(fetch(asset["browser_download_url"], max_bytes=lock["bytes"], timeout=60))
        else:
            path = Path(asset_path)
        if path.stat().st_size != lock["bytes"] or file_sha256(path) != lock["sha256"]:
            raise ValueError("Dictionary ZIP checksum or size mismatch")
        rows, report = directory / "outputs.tsv", directory / "coverage.json"
        decode(path, rows, report)
        index_rows(rows, json.loads(report.read_text(encoding="utf-8")), output)
    return output


class DictionaryIndex:
    def __init__(self, path):
        self.connection = None
        path = Path(path)
        digest = path.with_suffix(path.suffix + ".sha256")
        if not path.is_file() or not digest.is_file() or file_sha256(path) != digest.read_text(encoding="ascii").strip():
            raise ValueError("Verified dictionary index is missing or corrupt; refusing a missing-word conclusion")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            documents = connection.execute("SELECT document FROM metadata").fetchall()
            if len(documents) != 1:
                raise ValueError("Unverified dictionary index metadata")
            self.document = json.loads(documents[0][0])
            if (self.document.get("schema_version") != 1 or self.document.get("provenance") != provenance()
                    or self.document.get("outputs_sha256") != self.document["provenance"]["outputs_sha256"]
                    or connection.execute("SELECT COUNT(*) FROM words").fetchone()[0] != self.document.get("word_count")
                    or self.document["word_count"] <= 0 or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
                raise ValueError("Dictionary index integrity or pack coverage is unverified")
            self.connection = connection
        except Exception:
            connection.close()
            raise

    @classmethod
    def from_environment(cls):
        path = os.environ.get("DICTIONARY_INDEX_PATH")
        if not path:
            raise ValueError("Prepare the verified dictionary index and set DICTIONARY_INDEX_PATH first")
        return cls(path)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def contains(self, word):
        if self.connection is None:
            raise ValueError("Dictionary index is closed; refusing a missing-word conclusion")
        return self.connection.execute("SELECT 1 FROM words WHERE normalized=?", (news.normalize(word),)).fetchone() is not None

    def check(self, word, day):
        return {**self.document["provenance"], "status": "present" if self.contains(word) else "missing", "checked_at": day}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset", type=Path, help="Verify a previously downloaded ZIP against the same Release")
    args = parser.parse_args()
    prepare(args.output, asset_path=args.asset)
    with DictionaryIndex(args.output) as index:
        proof = index.document["provenance"]
        print(f"Verified {proof['release']}: {len(proof['packs'])} packs, {proof['token_count']:,} outputs")


if __name__ == "__main__":
    main()
