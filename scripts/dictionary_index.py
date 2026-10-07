"""Read a complete, verified converter export. Never fall back to source search."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import unicodedata
import urllib.parse
import zipfile
from collections import Counter
from pathlib import Path

PACK_PATHS = {
    "system": ("system", ""),
    "single_kanji": ("single_kanji", "_singleKanji"),
    "emoji": ("emoji", "_emoji"),
    "emoticon": ("emoticon", "_emoticon"),
    "symbol": ("symbol", "_symbol"),
    "reading_correction": ("reading_correction", "_reading_correction"),
    "kotowaza": ("kotowaza", "_kotowaza"),
    "person_name": ("person_name", "_person_names"),
    "places": ("places", "_places"),
    "wiki": ("wiki", "_wiki"),
    "neologd": ("neologd", "_neologd"),
    "web": ("web", "_web"),
    "english_reading": ("english_reading", ""),
}
ZIPPED = {"system", "places", "wiki", "neologd", "web", "english_reading"}
ROOT = "app/src/main/assets/"
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024


def normalize(word: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", word).strip()).casefold()


class DictionaryIndexError(RuntimeError):
    pass


def select_release(api, repository: str, tag: str = "") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise DictionaryIndexError("Invalid dictionary repository")
    if tag:
        release = api("GET", f"/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        if release.get("draft") or release.get("prerelease"):
            raise DictionaryIndexError("Dictionary release must be published and stable")
        return release
    for page in range(1, 101):
        releases = api("GET", f"/repos/{repository}/releases?per_page=100&page={page}")
        for release in releases:
            if not release.get("draft") and not release.get("prerelease") and release.get("tag_name", "").startswith("v"):
                # A newer release lacking an index must not silently select stale coverage.
                return release
        if len(releases) < 100:
            break
    raise DictionaryIndexError("No published converter version release found")


class DictionaryIndex:
    def __init__(self, repository: str, release: dict, manifest: bytes, archive: bytes, bundle: bytes):
        self.repository = repository
        self.release = release["tag_name"]
        self.manifest_sha256 = hashlib.sha256(manifest).hexdigest()
        self.url = release.get("html_url", f"https://github.com/{repository}/releases/tag/{self.release}")
        self._temporary = tempfile.TemporaryDirectory(prefix="new-word-dictionary-")
        self.connection = None
        try:
            self.manifest = json.loads(manifest)
            self._verify(archive, bundle)
            self.pos_ids = self.manifest["posIds"]
            self.connection = sqlite3.connect(str(Path(self._temporary.name) / "index.sqlite"))
            self.connection.execute("CREATE TABLE surfaces(word TEXT PRIMARY KEY) WITHOUT ROWID")
            self._load(archive)
        except Exception as error:
            self.close()
            if isinstance(error, DictionaryIndexError):
                raise
            raise DictionaryIndexError(f"Invalid dictionary export: {error}") from error

    @classmethod
    def fetch(cls, api, request, repository: str, tag: str = ""):
        release = select_release(api, repository, tag)
        assets = {asset["name"]: asset for asset in release.get("assets", [])}
        names = ("dictionary-index-manifest.json", "dictionary-index.tsv.gz", "japanese_keyboard_dictionary_assets.zip")
        if any(name not in assets for name in names):
            raise DictionaryIndexError(f"{repository}@{release['tag_name']} has no complete dictionary export; publish the converter index before enabling collection")
        prefix = f"https://github.com/{repository}/releases/download/{urllib.parse.quote(release['tag_name'], safe='')}/"
        downloaded = []
        for name in names:
            asset = assets[name]
            if not 0 < asset.get("size", 0) <= MAX_DOWNLOAD or asset.get("browser_download_url") != prefix + name:
                raise DictionaryIndexError("Dictionary assets must belong to the selected release")
            downloaded.append(request(asset["browser_download_url"], max_bytes=MAX_DOWNLOAD))
        return cls(repository, release, *downloaded)

    def _verify(self, archive: bytes, bundle: bytes):
        manifest = self.manifest
        if manifest.get("schemaVersion") != 1 or manifest.get("dictionaryRelease") != self.release:
            raise DictionaryIndexError("Dictionary manifest version/release mismatch")
        for field in ("converterCommit", "mozcCommit", "idDefSha256"):
            width = 64 if field == "idDefSha256" else 40
            if not re.fullmatch(rf"[0-9a-f]{{{width}}}", manifest.get(field, "")):
                raise DictionaryIndexError(f"Missing build provenance: {field}")
        packs = manifest.get("packs", {})
        if set(packs) != set(PACK_PATHS) or any(type(count) is not int or count <= 0 for count in packs.values()):
            raise DictionaryIndexError("Incomplete dictionary pack coverage")
        for field, name, data in (("index", "dictionary-index.tsv.gz", archive), ("assets", "japanese_keyboard_dictionary_assets.zip", bundle)):
            fact = manifest.get(field, {})
            if fact.get("name") != name or fact.get("sha256") != hashlib.sha256(data).hexdigest():
                raise DictionaryIndexError(f"Dictionary {field} checksum mismatch")
        if manifest["index"].get("rows") != sum(packs.values()):
            raise DictionaryIndexError("Manifest row counts differ")
        with zipfile.ZipFile(io.BytesIO(bundle)) as zip_file:
            expected = set()
            for pack, (directory, suffix) in PACK_PATHS.items():
                extension = ".dat.zip" if pack in ZIPPED else ".dat"
                for part in ("yomi", "tango", "token"):
                    path = ROOT + f"{directory}/{part}{suffix}{extension}"
                    expected.add(path)
                    if zip_file.getinfo(path).file_size <= 0:
                        raise DictionaryIndexError(f"Empty dictionary asset: {path}")
            actual_yomi = {name for name in zip_file.namelist() if name.startswith(ROOT) and name.rsplit('/', 1)[-1].startswith("yomi") and not name.endswith('/')}
            if actual_yomi != {name for name in expected if name.rsplit('/', 1)[-1].startswith("yomi")}:
                raise DictionaryIndexError("Packaged dictionaries differ from indexed coverage")
            if zip_file.getinfo(ROOT + "id.def").file_size > 2 * 1024 * 1024:
                raise DictionaryIndexError("Oversized id.def")
            id_def = zip_file.read(ROOT + "id.def")
        if hashlib.sha256(id_def).hexdigest() != manifest["idDefSha256"]:
            raise DictionaryIndexError("Packaged id.def checksum mismatch")
        ids = {}
        for number, line in enumerate(id_def.decode("utf-8").splitlines()):
            identity, label = line.split(maxsplit=1)
            if int(identity) != number or label in ids:
                raise DictionaryIndexError("Invalid packaged id.def")
            ids[label] = number
        pos_ids = manifest.get("posIds")
        if (not isinstance(pos_ids, dict) or any(type(value) is not int for value in pos_ids.values())
                or not ids or ids.get("BOS/EOS,*,*,*,*,*,*") != 0 or ids != pos_ids):
            raise DictionaryIndexError("POS IDs differ from the packaged dictionary")

    def _load(self, archive: bytes):
        counts = Counter()
        expanded = 0
        with gzip.GzipFile(fileobj=io.BytesIO(archive)) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                reader = csv.reader(text, delimiter="\t", strict=True)
                if next(reader, None) != ["dictionary", "reading", "word"]:
                    raise DictionaryIndexError("Invalid dictionary index header")
                batch = []
                for row in reader:
                    if len(row) != 3 or row[0] not in PACK_PATHS or not row[1].strip() or not normalize(row[2]):
                        raise DictionaryIndexError("Invalid dictionary index row")
                    expanded += sum(len(field.encode("utf-8")) for field in row) + 3
                    if expanded > MAX_EXPANDED:
                        raise DictionaryIndexError("Dictionary index exceeds expanded size limit")
                    counts[row[0]] += 1
                    if counts[row[0]] > self.manifest["packs"][row[0]]:
                        raise DictionaryIndexError("Dictionary index row count mismatch")
                    batch.append((normalize(row[2]),))
                    if len(batch) == 5000:
                        self.connection.executemany("INSERT OR IGNORE INTO surfaces VALUES(?)", batch)
                        batch.clear()
                self.connection.executemany("INSERT OR IGNORE INTO surfaces VALUES(?)", batch)
        if dict(counts) != self.manifest["packs"]:
            raise DictionaryIndexError("Dictionary index is truncated or incomplete")
        self.connection.commit()

    def contains(self, word: str) -> bool:
        if self.connection is None:
            raise DictionaryIndexError("Dictionary index is closed")
        return self.connection.execute("SELECT 1 FROM surfaces WHERE word=?", (normalize(word),)).fetchone() is not None

    def verification(self) -> dict:
        return {"repository": self.repository, "release": self.release, "manifest_sha256": self.manifest_sha256, "url": self.url}

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
