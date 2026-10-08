"""Canonical Release data and a deterministic, independently verifiable ZIP."""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

import release_candidates as news
import dictionary_assets as dictionaries

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data/release"
PART_BYTES = 32 * 1024 * 1024
ZIP_BYTES = 2 * 1024 * 1024 * 1024
COLUMNS = ["reading", "word", "pos"]


def json_bytes(value, *, pretty=False):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       indent=2 if pretty else None,
                       separators=None if pretty else (",", ":")) + "\n").encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def source_list(value):
    if not isinstance(value, list):
        raise ValueError("Evidence must be a list")
    for source in value:
        if not isinstance(source, dict) or not isinstance(source.get("source"), str) or not source["source"].strip():
            raise ValueError("Evidence requires a publisher")
        url = news.urllib.parse.urlsplit(source.get("link", ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Evidence requires a public source URL")
    return value


def validate_row(row, categories):
    if not isinstance(row, dict):
        raise ValueError("A candidate must be an object")
    for column in COLUMNS:
        value = row.get(column)
        if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"Invalid dictionary field: {column}")
    key = news.normalize(row["word"])
    if news.clean_candidate(row["word"]) is None:
        raise ValueError("Invalid phrase or incomplete written form")
    if row.get("normalized") != key or news.kana_reading(row["reading"]) != row["reading"]:
        raise ValueError("Invalid normalized spelling or hiragana reading")
    if key == news.normalize(row["reading"]):
        raise ValueError("Written form is identical to the kana input")
    if row.get("kind") not in news.candidate_pipeline.KINDS or row["pos"] != news.pos_label(row["word"], row["kind"]):
        raise ValueError("Invalid noun POS label")
    order = [category["id"] for category in categories]
    tags = row.get("categories")
    if not isinstance(tags, list) or not tags or tags != [identity for identity in order if identity in tags] or row.get("category") != tags[0]:
        raise ValueError("Invalid category tags or primary category")
    if row.get("reading_status") != "confirmed" or row.get("usage_status") != "confirmed":
        raise ValueError("Only confirmed readings and usages may be distributed")
    sources = source_list(row.get("sources"))
    hosts = {news.urllib.parse.urlsplit(source["link"]).hostname.lower().removeprefix("www.") for source in sources}
    if len(hosts) < 2 or len(news.candidate_pipeline.independent_sources(sources, news)) < 2:
        raise ValueError("Two independent Japanese usage sources are required")
    method = row.get("reading_method")
    if method == "kana":
        if news.kana_reading(row["word"]) != row["reading"]:
            raise ValueError("Kana spelling does not agree with the reading")
    elif method in {"source", "reviewed", "manual"}:
        if not source_list(row.get("reading_sources")):
            raise ValueError("Confirmed reading requires evidence")
    else:
        raise ValueError("Estimated or unknown reading method")
    if row["kind"] != "common" and not source_list(row.get("official_name_sources")):
        raise ValueError("Named entities require official naming evidence")
    dictionaries.validate_check(row.get("dictionary_check"), allow_unchecked=True)
    for field in ("accepted_date", "checked_at"):
        if not isinstance(row.get(field), str) or dt.date.fromisoformat(row[field]).isoformat() != row[field]:
            raise ValueError(f"Invalid {field}")
    return key


def validate_rows(rows, categories):
    seen = set()
    for row in rows:
        key = validate_row(row, categories)
        if key in seen:
            raise ValueError(f"Duplicate normalized spelling: {row['word']}")
        seen.add(key)


def parts(rows, *, part_bytes=PART_BYTES):
    """Keep metadata/TSV rows paired; never split UTF-8 bytes or a record."""
    if part_bytes <= 0:
        raise ValueError("Part limit must be positive")
    dictionary, metadata, count, index = bytearray(), bytearray(), 0, 1
    for row in rows:
        tsv = ("\t".join(row[field] for field in COLUMNS) + "\n").encode("utf-8")
        record = json_bytes(row)
        if max(len(tsv), len(record)) > part_bytes:
            raise ValueError("A single candidate exceeds the part size limit")
        if count and (len(dictionary) + len(tsv) > part_bytes or len(metadata) + len(record) > part_bytes):
            yield index, bytes(dictionary), bytes(metadata), count
            dictionary, metadata, count, index = bytearray(), bytearray(), 0, index + 1
        dictionary.extend(tsv)
        metadata.extend(record)
        count += 1
    if count:
        yield index, bytes(dictionary), bytes(metadata), count


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == raw:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".release-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_entries(directory=DATA_DIR, *, categories=None):
    categories = news.CATEGORIES if categories is None else categories
    paths = sorted(directory.glob("entries-*.jsonl"))
    if not paths:
        raise ValueError("Canonical Release data is missing; refusing to reset it")
    rows = []
    for index, path in enumerate(paths, 1):
        if path.name != f"entries-{index:04}.jsonl":
            raise ValueError("Canonical data parts must be consecutively numbered")
        if path.stat().st_size > PART_BYTES:
            raise ValueError("Canonical metadata exceeds the 32 MiB limit")
        with path.open(encoding="utf-8", newline="") as stream:
            for line in stream:
                if not line.endswith("\n") or line.endswith("\r\n"):
                    raise ValueError("Canonical metadata requires LF-terminated records")
                rows.append(json.loads(line))
    validate_rows(rows, categories)
    return rows


def save_entries(rows, directory=DATA_DIR, *, categories=None, part_bytes=PART_BYTES):
    categories = news.CATEGORIES if categories is None else categories
    validate_rows(rows, categories)
    payloads = list(parts(rows, part_bytes=part_bytes))
    if not payloads:
        raise ValueError("Refusing to replace canonical data with an empty list")
    names = set()
    for index, _, metadata, _ in payloads:
        name = f"entries-{index:04}.jsonl"
        names.add(name)
        atomic_write(directory / name, metadata)
    for path in directory.glob("entries-*.jsonl"):
        if path.name not in names and re.fullmatch(r"entries-\d{4,}\.jsonl", path.name):
            path.unlink()


def content_digest(manifest):
    fields = ("schema_version", "encoding", "line_endings", "columns", "categories", "files",
              "word_count", "category_counts", "primary_category_counts", "dictionary_check", "part_bytes")
    return sha256(json_bytes({field: manifest[field] for field in fields}))


def build_archive(rows, output, *, source_commit, updated_at, categories=None, part_bytes=PART_BYTES, zip_bytes=ZIP_BYTES):
    categories = news.CATEGORIES if categories is None else categories
    validate_rows(rows, categories)
    for row in rows:
        dictionaries.validate_check(row.get("dictionary_check"))
        if row["dictionary_check"]["status"] != "missing":
            raise ValueError("Only words absent from all dictionary packs may be distributed")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit):
        raise ValueError("The Git source commit is required")
    timestamp = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() != dt.timedelta(0):
        raise ValueError("Manifest timestamp must be UTC")
    manifest = {
        "schema_version": 1, "source_commit": source_commit,
        "updated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "encoding": "UTF-8", "line_endings": "LF", "columns": COLUMNS,
        "categories": [{"id": item["id"], "label": item["label"]} for item in categories],
        "word_count": len(rows), "category_counts": dict(Counter(tag for row in rows for tag in row["categories"])),
        "primary_category_counts": dict(Counter(row["category"] for row in rows)),
        "dictionary_check": {**dictionaries.provenance(), "status": "verified"}, "part_bytes": part_bytes, "files": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            def member(name, raw):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            payloads = parts(rows, part_bytes=part_bytes) if rows else [(1, b"", b"", 0)]
            for index, dictionary, metadata, count in payloads:
                for kind, extension, raw in (("dictionary", "tsv", dictionary), ("metadata", "jsonl", metadata)):
                    name = f"{kind}-{index:04}.{extension}"
                    member(name, raw)
                    manifest["files"].append({"name": name, "kind": kind, "rows": count,
                                               "bytes": len(raw), "sha256": sha256(raw)})
            manifest["data_sha256"] = content_digest(manifest)
            member("manifest.json", json_bytes(manifest, pretty=True))
        if output.stat().st_size >= zip_bytes:
            raise ValueError("Release ZIP must be smaller than 2 GiB")
        validate_archive(output, zip_bytes=zip_bytes)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return manifest


def validate_archive(path, *, zip_bytes=ZIP_BYTES, dictionary=None):
    if path.stat().st_size >= zip_bytes:
        raise ValueError("Release ZIP must be smaller than 2 GiB")
    with zipfile.ZipFile(path) as archive:
        if archive.getinfo("manifest.json").file_size > 2 * 1024 * 1024:
            raise ValueError("Oversized manifest")
        manifest = json.loads(archive.read("manifest.json"))
        commit = manifest.get("source_commit", "")
        timestamp = dt.datetime.fromisoformat(manifest.get("updated_at", "").replace("Z", "+00:00"))
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) or timestamp.utcoffset() != dt.timedelta(0):
            raise ValueError("Invalid archive provenance")
        if (manifest.get("schema_version") != 1 or manifest.get("columns") != COLUMNS
                or manifest.get("encoding") != "UTF-8" or manifest.get("line_endings") != "LF"
                or not isinstance(manifest.get("part_bytes"), int) or not 0 < manifest["part_bytes"] <= PART_BYTES):
            raise ValueError("Unsupported dictionary archive format")
        files = manifest["files"]
        expected = [item["name"] for item in files] + ["manifest.json"]
        if len(set(expected)) != len(expected) or sorted(archive.namelist()) != sorted(expected):
            raise ValueError("Missing, duplicate, or unexpected ZIP members")
        if len(files) % 2 or not files:
            raise ValueError("Dictionary/metadata parts must be paired")
        rows, seen, counts, primary = 0, set(), Counter(), Counter()
        for offset in range(0, len(files), 2):
            pair = files[offset:offset + 2]
            index = offset // 2 + 1
            for entry, kind, extension in zip(pair, ("dictionary", "metadata"), ("tsv", "jsonl")):
                if entry["name"] != f"{kind}-{index:04}.{extension}" or entry["kind"] != kind:
                    raise ValueError("Invalid part order or filename")
                digest, size = hashlib.sha256(), 0
                with archive.open(entry["name"]) as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                        size += len(chunk)
                if size > manifest["part_bytes"] or size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("Part integrity or size check failed")
            count = 0
            with archive.open(pair[0]["name"]) as dictionary_stream, archive.open(pair[1]["name"]) as metadata:
                for raw in metadata:
                    if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
                        raise ValueError("Metadata requires LF-terminated records")
                    row = json.loads(raw.decode("utf-8"))
                    key = validate_row(row, manifest["categories"])
                    dictionaries.validate_check(row.get("dictionary_check"))
                    if row["dictionary_check"]["status"] != "missing":
                        raise ValueError("Registered or unchecked words cannot be distributed")
                    if dictionary is not None and dictionary.contains(row["word"]):
                        raise ValueError("Distributed word is present in the decoded dictionary outputs")
                    if key in seen:
                        raise ValueError("Duplicate word across parts")
                    seen.add(key)
                    expected_line = ("\t".join(row[field] for field in COLUMNS) + "\n").encode("utf-8")
                    if dictionary_stream.readline() != expected_line:
                        raise ValueError("Dictionary output disagrees with metadata")
                    count += 1
                    counts.update(row["categories"])
                    primary.update([row["category"]])
                if dictionary_stream.read(1) or any(item["rows"] != count for item in pair):
                    raise ValueError("Dictionary and metadata row counts disagree")
            rows += count
        if (rows != manifest["word_count"] or dict(counts) != manifest["category_counts"]
                or dict(primary) != manifest["primary_category_counts"]
                or manifest["dictionary_check"] != {**dictionaries.provenance(), "status": "verified"}
                or manifest["data_sha256"] != content_digest(manifest)):
            raise ValueError("Manifest disagrees with archive data")
    return manifest


def data_revision():
    raw = subprocess.check_output([
        "git", "log", "-1", "--format=%H%x09%cI", "--", "data/release/entries-*.jsonl",
        "data/categories.json", "scripts/release_archive.py",
        "data/dictionary-release.json", "scripts/dictionary_assets.py", "scripts/DecodeDictionary.java",
    ], cwd=ROOT, text=True).strip()
    commit, timestamp = raw.split("\t")
    utc = dt.datetime.fromisoformat(timestamp).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return commit, utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/new-words.zip")
    args = parser.parse_args()
    commit, timestamp = data_revision()
    with dictionaries.DictionaryIndex.from_environment() as index:
        rows = load_entries()
        for row in rows:
            dictionaries.validate_check(row.get("dictionary_check"))
            if index.contains(row["word"]) != (row["dictionary_check"]["status"] == "present"):
                raise ValueError("Saved dictionary verification disagrees with the decoded outputs")
        rows = [row for row in rows if row["dictionary_check"]["status"] == "missing"]
        manifest = build_archive(rows, args.output, source_commit=commit, updated_at=timestamp)
    print(f"Validated {manifest['word_count']} words: {args.output}")


if __name__ == "__main__":
    main()
