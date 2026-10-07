#!/usr/bin/env python3
from __future__ import annotations

import csv
import datetime as dt
import email.utils
import html
from html.parser import HTMLParser
import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = os.environ.get("GITHUB_REPOSITORY", "KazumaProject/New-word")
TOKEN = os.environ.get("GH_TOKEN", "")
TZ = ZoneInfo("America/Toronto")
DAILY_LIMIT = 10
SEEN_PATH = Path("data/seen.tsv")
READINGS_PATH = Path(__file__).resolve().parents[1] / "data/readings.json"
SEEN_FIELDS = ["date", "reading", "word", "pos", "id", "normalized"]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_pipeline

PENDING_PATH = Path("data/pending.json")
CATEGORIES = candidate_pipeline.load_categories(Path(__file__).resolve().parents[1] / "data/categories.json")

GENERIC_REJECT = {
    "新サービス", "新製品", "新技術", "新機能", "新ブランド",
    "サービス開始", "提供開始", "株式会社",
}
ORG_HINTS = (
    "研究所", "研究センター", "研究室", "ラボ", "Lab", "Research",
    "チーム", "委員会", "協会", "機構", "財団", "連盟",
)
QUOTE_RE = re.compile(r'[「『“"]([^」』”"]{2,60})[」』”"]')
ASCII_NAME_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z][A-Za-z0-9+._-]*)(?:\s+[A-Z][A-Za-z0-9+._-]*){0,4}(?![A-Za-z0-9])")
ENTRIES_RE = re.compile(r"<!-- new-word-entries\s*\n(.*?)\n-->", re.DOTALL)


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(retry_after)
                return max(0.0, when.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    if error.headers.get("X-RateLimit-Remaining") == "0":
        try:
            return max(0.0, float(error.headers["X-RateLimit-Reset"]) - time.time()) + 1
        except (KeyError, TypeError, ValueError):
            return 60.0
    return float(2 ** attempt)


def request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
    timeout: float = 30,
    read_attempts: int = 3,
    max_bytes: int | None = None,
) -> bytes:
    h = {"User-Agent": "KazumaProject-New-word/2.0"}
    if headers:
        h.update(headers)
    # Retrying a write after a lost response can create a duplicate Issue.
    attempts = read_attempts if method == "GET" else 1
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read() if max_bytes is None else response.read(max_bytes + 1)
                if max_bytes is not None and len(raw) > max_bytes:
                    raise ValueError("Response exceeds size limit")
                return raw
        except urllib.error.HTTPError as error:
            retryable = error.code in {408, 429, 500, 502, 503, 504} or (
                error.code == 403 and (
                    error.headers.get("Retry-After") is not None
                    or error.headers.get("X-RateLimit-Remaining") == "0"
                )
            )
            if not retryable:
                raise
            delay = retry_delay(error, attempt)
            if delay > 60:
                raise RuntimeError("Remote service requires a cooldown longer than 60 seconds") from error
            if attempt == attempts:
                raise
            host = urllib.parse.urlsplit(url).netloc
            print(f"Retry GET {host} after HTTP {error.code}: wait {delay:.1f}s", flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Request did not complete")


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).strip()
    return re.sub(r"\s+", " ", s).casefold()


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    with SEEN_PATH.open(encoding="utf-8", newline="") as stream:
        return {
            normalize(row["normalized"])
            for row in csv.DictReader(stream, delimiter="\t")
            if row.get("normalized")
        }


def append_seen(rows: list[dict]) -> int:
    seen = load_seen()
    additions = []
    for row in rows:
        key = normalize(row["word"])
        if key not in seen:
            additions.append({field: row[field] for field in SEEN_FIELDS})
            seen.add(key)
    if not additions:
        return 0

    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    original = SEEN_PATH.read_text(encoding="utf-8") if SEEN_PATH.exists() else ""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=SEEN_PATH.parent,
            prefix=".seen-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            if original:
                stream.write(original)
                if not original.endswith("\n"):
                    stream.write("\n")
            writer = csv.DictWriter(stream, fieldnames=SEEN_FIELDS, delimiter="\t", lineterminator="\n")
            if not original:
                writer.writeheader()
            writer.writerows(additions)
        temporary.replace(SEEN_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(additions)


def google_news_rss(query: str) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
    root = ET.fromstring(request("https://news.google.com/rss/search?" + params))
    if root.tag != "rss" or root.find("channel") is None:
        raise RuntimeError("Google News returned an unexpected RSS document")
    items = []
    for item in root.findall("./channel/item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            items.append({
                "title": title,
                "link": link,
                "pubdate": (item.findtext("pubDate") or "").strip(),
                "source": (item.findtext("source") or "").strip(),
                "description": html.unescape(item.findtext("description") or ""),
            })
    return items


def article_date(item: dict[str, str]) -> dt.datetime | None:
    try:
        date = email.utils.parsedate_to_datetime(item["pubdate"])
        if date.tzinfo is None:
            return None
        return date.astimezone(dt.timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def clean_candidate(raw: str) -> str | None:
    s = unicodedata.normalize("NFKC", html.unescape(raw)).strip(" \t\r\n・:：-—")
    s = re.sub(r"\s+", " ", s)
    if not (2 <= len(s) <= 50) or s in GENERIC_REJECT:
        return None
    if re.search(r"https?://|www\.|(?:\.{3}|…)$", s, re.IGNORECASE):
        return None
    if re.fullmatch(r"[0-9.,%＋+\-]+", s):
        return None
    if re.search(r"(とは|について|に関する|を発表|を提供|を開始|を開発)$", s):
        return None
    # Quotations often contain headlines, slogans and descriptions, not names.
    if re.search(r"[。！？!?、,]|(?:に見える|に決めた|すべは|を実現|できます|しました|している|という|の理由|はない|ではない)", s):
        return None
    if re.search(r"(?:を|が|は|に)[一-龠ぁ-ん]*(?:する|した|なる|できる|創る|変える|届ける|ます|ません)$", s):
        return None
    if len(re.findall(r"[ぁ-んァ-ヶ一-龠A-Za-z]", s)) < 2:
        return None
    return s


def extract_candidates(title: str) -> set[str]:
    body = re.sub(r"\s+-\s+[^-]+$", "", title)
    found = set()
    for match in QUOTE_RE.finditer(body):
        candidate = clean_candidate(match.group(1))
        if candidate and name_context(body, match.start(), match.end()):
            found.add(candidate)
    for match in ASCII_NAME_RE.finditer(body):
        if any(quote.start() <= match.start() < quote.end() for quote in QUOTE_RE.finditer(body)):
            continue
        candidate = clean_candidate(match.group(0))
        if (candidate and len(candidate) >= 3 and candidate.lower() not in {"ai", "dx", "iot"}
                and name_context(body, match.start(), match.end())):
            found.add(candidate)
    # Do not extract a shorter ASCII fragment from a quoted product name.
    found = {word for word in found if not any(word != other and word in other for other in found)}
    return found


def name_context(title: str, start: int, end: int) -> bool:
    """Require a local naming cue, rather than an announcement anywhere in a title."""
    before, after = title[max(0, start - 24):start], title[end:end + 30]
    category = r"(?:新製品|新サービス|新技術|新機能|新ブランド|新モデル|製品|サービス|技術|ブランド|モデル|アプリ|ゲーム|アニメ|新曲|作品|人名|人物|選手|歌手|俳優|組織|施設|新駅|地名|研究所|用語|新語|言葉|俗語|略語|名称|名付けた|名づけた|命名)"
    return bool(re.search(category + r"[『「“\"\s:：]*$", before)
                or re.match(r"[』」”\"\s]*(?:とは|の意味|という|と呼ばれる|と称する|と名付け|と命名|を(?:発表|発売|提供|開発|公開|リリース)|が(?:登場|発売)|の(?:提供|販売|発売))", after))


def kana_reading(value: str) -> str | None:
    value = unicodedata.normalize("NFKC", value).strip()
    if not re.fullmatch(r"[ぁ-ゖァ-ヶー・\s]+", value) or not re.search(r"[ぁ-ゖァ-ヶ]", value):
        return None
    return "".join(
        chr(ord(ch) - 0x60) if 0x30A1 <= ord(ch) <= 0x30F6 else ch
        for ch in value if ch != "・" and not ch.isspace()
    )


def reading_hint(word: str) -> str:
    return kana_reading(word) or "要確認"


class ReadingHTML(HTMLParser):
    """Read visible text and ruby without interpreting scripts or styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.rubies = []
        self.hidden = 0
        self.ruby = None
        self.ruby_part = "base"

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "ruby":
            self.ruby = {"base": [], "reading": []}
        elif tag == "rt":
            self.ruby_part = "reading"
        elif tag == "rp":
            self.ruby_part = "ignore"
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "td"}:
            self.parts.append("\n")
        if tag == "img":
            self.parts.append(dict(attrs).get("alt", ""))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in {"rt", "rp"}:
            self.ruby_part = "base"
        elif tag == "ruby" and self.ruby is not None:
            base = "".join(self.ruby["base"])
            reading = "".join(self.ruby["reading"])
            self.rubies.append((base, reading))
            self.parts.append(base)
            self.ruby = None
            self.ruby_part = "base"
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "td"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.ruby is not None:
            if self.ruby_part != "ignore":
                self.ruby[self.ruby_part].append(data)
        else:
            self.parts.append(data)


def extract_readings(word: str, document: str) -> set[str]:
    parser = ReadingHTML()
    parser.feed(document)
    found = {reading for base, raw in parser.rubies
             if normalize(base) == normalize(word) and (reading := kana_reading(raw))}
    text = unicodedata.normalize("NFKC", "".join(parser.parts))
    # Require the entire name, not a matching substring in another product name.
    name = re.escape(unicodedata.normalize("NFKC", word)).replace(r"\ ", r"\s+")
    kana = r"([ぁ-ゖァ-ヶー・ ]{1,100})"
    boundary = r"(?<![A-Za-z0-9一-龠ぁ-ゖァ-ヶ])"
    patterns = (
        boundary + name + r'[」』”"]?\s*\(\s*(?:(?:読み方|読み|よみ)\s*[:：]\s*)?' + kana + r"\s*\)",
        boundary + kana + r"\s*\(\s*" + name + r"\s*\)",
        boundary + name + r'[」』”"]?\s*(?:の)?(?:読み方|読み|よみ)\s*[:：は]\s*[「『]?'+ kana + r"[」』]*(?=[。\n<]|$)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            reading = kana_reading(match.group(1))
            if reading:
                found.add(reading)
    return found


def public_reading_document(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Invalid source URL")
    raw = request(url, timeout=10, read_attempts=1, max_bytes=2_000_000)
    charset = re.search(br'charset=["\']?([A-Za-z0-9_-]+)', raw[:4096], re.IGNORECASE)
    return raw.decode(charset[1].decode("ascii") if charset else "utf-8", errors="replace")


def publisher_url(url: str) -> str:
    """Resolve the public Google News article wrapper to its publisher URL."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != "news.google.com":
        return url
    if not re.fullmatch(r"/(?:rss/)?(?:articles|read)/[A-Za-z0-9_-]+", parts.path):
        raise ValueError("Unsupported Google News link")
    wrapper = public_reading_document(url)
    signature = re.search(r'data-n-a-sg="([^"<>]+)"', wrapper)
    timestamp = re.search(r'data-n-a-ts="(\d+)"', wrapper)
    if not signature or not timestamp:
        raise ValueError("Google News publisher URL unavailable")
    context = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1],
               1, 1, None, 0, 0, None, 0]
    inner = ["garturlreq", context, parts.path.rsplit("/", 1)[1], int(timestamp[1]), html.unescape(signature[1])]
    body = urllib.parse.urlencode({"f.req": json.dumps([
        [["Fbv4je", json.dumps(inner), None, "generic"]]
    ])}).encode()
    raw = request("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                  method="POST", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"},
                  timeout=10, max_bytes=100_000).decode("utf-8")
    for line in raw.splitlines():
        if line.startswith("[["):
            for item in json.loads(line):
                if isinstance(item, list) and len(item) >= 3 and item[0] == "wrb.fr" and item[1] == "Fbv4je" and isinstance(item[2], str):
                    data = json.loads(item[2])
                    if isinstance(data, list) and len(data) >= 2 and data[0] == "garturlres" and isinstance(data[1], str):
                        return data[1]
    raise ValueError("Google News publisher URL unavailable")


class ReadingResolver:
    def __init__(self, *, page_limit: int | None = 20) -> None:
        self.page_limit = page_limit
        self.page_count = 0
        entries = json.loads(READINGS_PATH.read_text(encoding="utf-8")) if READINGS_PATH.exists() else []
        self.reviewed = {}
        for entry in entries:
            reading = kana_reading(entry["reading"])
            if reading != entry["reading"] or not entry.get("sources"):
                raise ValueError("Reviewed readings require hiragana and source links")
            self.reviewed[normalize(entry["word"])] = entry
        self.documents = {}
        self.results = {}

    def estimate(self, word: str, sources: list[dict]) -> str | None:
        """Suggest a complete reading; never promote a guess to a confirmed reading."""
        text = unicodedata.normalize("NFKC", word)
        context = normalize(" ".join(source.get("title", "") for source in sources))
        letters = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (
            "えー", "びー", "しー", "でぃー", "いー", "えふ", "じー", "えいち", "あい",
            "じぇー", "けー", "える", "えむ", "えぬ", "おー", "ぴー", "きゅー", "あーる",
            "えす", "てぃー", "ゆー", "ぶい", "だぶりゅー", "えっくす", "わい", "ぜっと")))
        # Reuse reviewed components only in the same context. Split at word
        # boundaries so a short name does not rewrite part of another brand.
        parts = re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?|[一-龠々ぁ-ゖァ-ヶー]+|[^\w\s]", text)
        if "".join(parts) != re.sub(r"\s+", "", text):
            return None
        result = []
        for part in parts:
            entry = self.reviewed.get(normalize(part))
            if entry and (not entry.get("context") or any(normalize(term) in context for term in entry["context"])):
                result.append(entry["reading"])
            elif kana := kana_reading(part):
                result.append(kana)
            elif re.fullmatch(r"[A-Z]{2,6}", part):
                result.append("".join(letters[ch] for ch in part))
            elif re.fullmatch(r"[一-龠々ぁ-ゖァ-ヶー]+", part):
                try:
                    from pykakasi import kakasi
                except ImportError:
                    return None
                converted = kana_reading("".join(item["hira"] for item in kakasi().convert(part)))
                if not converted:
                    return None
                result.append(converted)
            elif part == "-":
                continue
            else:
                # Unknown English names and numbers have multiple possible readings.
                return None
        return kana_reading("".join(result))

    def resolve(self, word: str, sources: list[dict]) -> dict:
        key = normalize(word)
        context = normalize(" ".join(source.get("title", "") for source in sources))
        reviewed = self.reviewed.get(key)
        if reviewed and (not reviewed.get("context") or any(normalize(term) in context for term in reviewed["context"])):
            return {"reading": reviewed["reading"], "reading_status": "confirmed",
                    "reading_method": "reviewed", "reading_sources": reviewed["sources"],
                    "reading_note": reviewed.get("note", "")}
        if reading := kana_reading(word):
            return {"reading": reading, "reading_status": "confirmed", "reading_method": "kana", "reading_sources": [], "reading_note": ""}
        cache_key = (key, tuple(source.get("link", "") for source in sources))
        if cache_key in self.results:
            return self.results[cache_key].copy()
        found = {}
        failures = 0
        # At most three publisher pages per word. Reuse shared article pages.
        for source in sources[:3]:
            for reading in extract_readings(word, source.get("title", "") + "\n" + source.get("description", "")):
                found.setdefault(reading, []).append({"source": source["source"], "link": source["link"]})
            link = source.get("link", "")
            if not link:
                continue
            if link not in self.documents:
                if self.page_limit is not None and self.page_count >= self.page_limit:
                    failures += 1
                    continue
                self.page_count += 1
                try:
                    direct = publisher_url(link)
                    self.documents[link] = (direct, public_reading_document(direct))
                except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, LookupError) as error:
                    print(f"Reading source unavailable ({source.get('source', '')}): {type(error).__name__}", flush=True)
                    self.documents[link] = None
            document = self.documents[link]
            if document is None:
                failures += 1
                continue
            direct, markup = document
            for reading in extract_readings(word, markup):
                found.setdefault(reading, []).append({"source": source["source"], "link": direct})
        if len(found) == 1:
            reading, evidence = next(iter(found.items()))
            result = {"reading": reading, "reading_status": "confirmed", "reading_method": "source",
                      "reading_sources": list({item["link"]: item for item in evidence}.values()), "reading_note": ""}
        else:
            status = "conflict" if found else "source_unavailable" if failures else "unconfirmed"
            notes = {"conflict": "出典間で読みが異なるため確認待ち", "source_unavailable": "出典の取得失敗または取得上限により、読みの明記が未確認",
                     "unconfirmed": "出典に読みの明記が見つからないため確認待ち"}
            result = {"reading": "要確認", "reading_status": status, "reading_method": "", "reading_sources": [],
                      "reading_note": notes[status]}
            if not found and (estimate := self.estimate(word, sources)):
                result["reading_estimate"] = estimate
                result["reading_note"] += f" / 推定読み: {estimate}（未確認・TSV対象外）"
        self.results[cache_key] = result
        return result.copy()


def pos_label(word: str, kind: str = "proper") -> str:
    if kind == "common":
        return "名詞,一般,*,*,*,*,*"
    if kind == "person":
        return "名詞,固有名詞,人名,一般,*,*,*"
    if kind == "place":
        return "名詞,固有名詞,地域,一般,*,*,*"
    if kind == "organization" or any(hint in word for hint in ORG_HINTS):
        return "名詞,固有名詞,組織,*,*,*,*"
    return "名詞,固有名詞,一般,*,*,*,*"


def github_api(method: str, path: str, payload: dict | None = None):
    if not TOKEN:
        raise RuntimeError("GH_TOKEN is not set")
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    raw = request("https://api.github.com" + path, headers=headers, method=method, body=body)
    return json.loads(raw) if raw else None


def list_issues() -> list[dict]:
    result = []
    page = 1
    while True:
        issues = github_api("GET", f"/repos/{REPO}/issues?state=all&per_page=100&page={page}")
        result.extend(issue for issue in issues if "pull_request" not in issue)
        if len(issues) < 100:
            return result
        page += 1


def issue_rows(issue: dict) -> list[dict]:
    """Read metadata and the previous collector's TSV without treating prose as words."""
    body = issue.get("body") or ""
    date_match = re.fullmatch(r"新語候補 (\d{4}-\d{2}-\d{2})", issue.get("title") or "")
    date = date_match.group(1) if date_match else ""
    result = {}
    for match in ENTRIES_RE.finditer(body):
        data = json.loads(match.group(1))
        if data.get("version") not in {1, 2, 3} or not isinstance(data.get("rows"), list):
            raise RuntimeError(f"Issue #{issue['number']} has unsupported candidate metadata")
        for row in data["rows"]:
            if not isinstance(row, dict) or any(not isinstance(row.get(field), str) for field in SEEN_FIELDS):
                raise RuntimeError(f"Issue #{issue['number']} has invalid candidate metadata")
            key = normalize(row["word"])
            if not key or normalize(row["normalized"]) != key:
                raise RuntimeError(f"Issue #{issue['number']} has inconsistent candidate metadata")
            if data["version"] in {2, 3}:
                allowed = {category["id"] for category in CATEGORIES}
                if (not isinstance(row.get("categories"), list) or not row["categories"]
                        or any(category not in allowed for category in row["categories"])
                        or row.get("category") != row["categories"][0]):
                    raise RuntimeError(f"Issue #{issue['number']} has invalid category metadata")
                if data["version"] == 2 and (not isinstance(row.get("dictionary"), dict)
                        or not row["dictionary"].get("release")
                        or not re.fullmatch(r"[0-9a-f]{64}", row["dictionary"].get("manifest_sha256", ""))):
                    raise RuntimeError(f"Issue #{issue['number']} has invalid verified candidate metadata")
                if data["version"] == 3 and (row.get("dictionary_check") != {"status": "not_checked"} or row["id"] or row.get("dictionary")):
                    raise RuntimeError(f"Issue #{issue['number']} has invalid collection-only metadata")
            result[key] = row
    for match in re.finditer(r"```tsv\s*\n(.*?)```", body, re.DOTALL):
        for row in csv.DictReader(io.StringIO(match.group(1)), delimiter="\t"):
            word = row.get("表記")
            if not word or not all(row.get(field) for field in ("読み", "品詞")) or (not row.get("左ID") and row.get("辞書照合") != "未確認"):
                continue
            key = normalize(word)
            result.setdefault(key, {
                "date": date, "reading": row["読み"], "word": word,
                "pos": row["品詞"], "id": row["左ID"], "normalized": key,
                "sources": [], "supplement": False,
            })
    # Recognize human readings entered in the visible table or import TSV.
    # Hidden metadata from an earlier run must not overwrite those edits.
    visible = []
    for line in body.splitlines():
        cells = line.split("|")
        if line.startswith("|") and len(cells) in {7, 8}:
            visible.append((html.unescape(cells[2].strip()), html.unescape(cells[1].strip())))
    for match in re.finditer(r"```tsv\s*\n(.*?)```", body, re.DOTALL):
        visible.extend((row.get("表記", ""), row.get("読み", ""))
                       for row in csv.DictReader(io.StringIO(match[1]), delimiter="\t"))
    for word, reading in visible:
        key = normalize(word)
        if key in result and result[key]["reading"] == "要確認" and kana_reading(reading):
            result[key] = {**result[key], "reading": reading, "reading_method": "manual",
                           "reading_status": "confirmed", "reading_sources": [], "reading_note": "手動で確認済み"}
    return list(result.values())


def select_sources(items: list[dict[str, str]]) -> list[dict[str, str]]:
    ordered = sorted(items, key=lambda item: (-article_date(item).timestamp(), item["link"]))
    ordered = [item for item in ordered if item["source"].strip()]
    # A shared link must not count as evidence from two publishers.
    for first in ordered:
        for second in ordered:
            if normalize(first["source"]) != normalize(second["source"]) and first["link"] != second["link"]:
                selected = [first, second]
                publishers = {normalize(item["source"]) for item in selected}
                links = {item["link"] for item in selected}
                for item in ordered:
                    if normalize(item["source"]) not in publishers and item["link"] not in links:
                        selected.append(item)
                        break
                return selected
    return []



def require_official_name(word, sources, readings):
    return candidate_pipeline.official_name(word, sources, readings, SimpleNamespace(**globals()))


def verify_usage_sources(word, sources, readings):
    return candidate_pipeline.verified_usage(word, sources, readings, SimpleNamespace(**globals()))


def collect_candidates(now, excluded, limit, readings, *, pending):
    return candidate_pipeline.collect(now, excluded, limit, readings, pending, CATEGORIES, SimpleNamespace(**globals()))


def markdown_cell(value: str) -> str:
    return html.escape(value, quote=False).replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ")


def render_rows(rows: list[dict]) -> str:
    review = bool(rows) and all(row.get("dictionary_check") == {"status": "not_checked"} for row in rows)
    lines = [
        "| 読み | 表記 | Mozc品詞 | id.def ID | 根拠/出典 | 備考 |",
        "|---|---|---|---:|---|---|",
    ]
    if review:
        lines = ["| 読み | 表記 | 品詞 | 根拠/出典 | 備考 |", "|---|---|---|---|---|"]
    for row in rows:
        sources = "<br>".join(
            f"[{markdown_cell(source['source'])}](<{source['link']}>)" for source in row["sources"]
        )
        notes = []
        if row["reading"] == "要確認":
            notes.append(row.get("reading_note") or "読みの明記が未確認")
        elif row.get("reading_method") == "kana":
            notes.append("かな表記をひらがなに正規化")
        elif row.get("reading_sources"):
            notes.append("読み確認: " + "、".join(
                f"[{markdown_cell(source['source'])}](<{source['link']}>)"
                for source in row["reading_sources"]
            ))
            if row.get("reading_note"):
                notes.append(markdown_cell(row["reading_note"]))
        if row["supplement"]:
            notes.append("補充候補: 過去の記事も対象")
        if row.get("official_name_sources"):
            notes.append("公式名称: " + "、".join(
                f"[{markdown_cell(source['source'])}](<{source['link']}>)"
                for source in row["official_name_sources"]
            ))
        lines.append(
            f"| {markdown_cell(row['reading'])} | {markdown_cell(row['word'])} | "
            f"`{row['pos']}` | " + ("" if review else f"{row['id']} | ") + f"{sources} | {' / '.join(notes)} |"
        )
    verified = bool(rows) and all(row.get("dictionary") and row.get("categories") for row in rows)
    fields = ["読み", "表記", "左ID", "右ID", "品詞"] + (["分類", "辞書照合"] if review else ["分類", "辞書リリース", "辞書リポジトリ", "辞書manifestSHA256"] if verified else [])
    lines += ["", "## TSV", "", "```tsv", "\t".join(fields)]
    for row in rows:
        if kana_reading(row["reading"]):
            output = io.StringIO()
            values = [row["reading"], row["word"], row["id"], row["id"], row["pos"]]
            if review:
                values += [",".join(row["categories"]), "未確認"]
            elif verified:
                values += [",".join(row["categories"]), row["dictionary"]["release"], row["dictionary"]["repository"], row["dictionary"]["manifest_sha256"]]
            csv.writer(output, delimiter="\t", lineterminator="").writerow(values)
            lines.append(output.getvalue())
    if any(not kana_reading(row["reading"]) for row in rows):
        lines.insert(lines.index("```tsv"), "読み未確定の語はTSVから除外しています。\n")
    lines += ["```", ""]
    # Escape comment delimiters so a candidate cannot truncate the metadata.
    metadata = json.dumps({"version": 3 if review else 2 if verified else 1, "rows": rows}, ensure_ascii=False).replace("--", "\\u002d\\u002d")
    lines += ["<!-- new-word-entries", metadata, "-->"]
    return "\n".join(lines)


def render_grouped_rows(rows: list[dict]) -> str:
    if not rows or not all(row.get("category") for row in rows):
        return render_rows(rows)
    separator = "\n\n## TSV\n\n"
    groups = []
    for category in CATEGORIES:
        group = [row for row in rows if row["category"] == category["id"]]
        if group:
            groups.append(f"## {category['label']}\n\n" + render_rows(group).split(separator, 1)[0])
    groups.append("## TSV\n\n" + render_rows(rows).split(separator, 1)[1])
    return "\n\n".join(groups)


def render_issue(title: str, rows: list[dict]) -> str:
    return "\n".join([
        f"# {title}", "", render_grouped_rows(rows), "", "## 確認範囲", "",
        f"- 過去の {REPO} Issue と data/seen.tsv による掲載済み語の除外",
        "- 辞書照合は未確認。掲載語が辞書に未収録であるとは判定していません",
        "- 掲載済み語の重複はNFKC・空白・英字大小を正規化して除外",
        "- 読み確認済みの名詞・固有名詞のみ。推定読みは掲載しません",
        "- 未確定語は data/pending.json に保存。TSVはレビュー候補。品詞IDは辞書接続まで空欄",
        "", "_日本語での使用例と読みを確認したIME用のレビュー候補です。_",
    ])


def publish_candidates(title: str, rows: list[dict], existing: dict | None) -> tuple[str, list[dict]]:
    if existing is None:
        selected = rows[:DAILY_LIMIT]
        issue = github_api("POST", f"/repos/{REPO}/issues", {"title": title, "body": render_issue(title, selected)})
        return issue["html_url"], selected

    path = f"/repos/{REPO}/issues/{existing['number']}"
    # Fetch the current body immediately before appending to retain human edits.
    current = github_api("GET", path)
    previous_rows = issue_rows(current)
    previous_keys = {normalize(row["word"]) for row in previous_rows}
    selected = []
    remaining = max(0, DAILY_LIMIT - len(previous_rows))
    for row in rows:
        key = normalize(row["word"])
        if key not in previous_keys and len(selected) < remaining:
            selected.append(row)
            previous_keys.add(key)
    if selected:
        body = (current.get("body") or "") + "\n\n## 追加候補\n\n" + render_grouped_rows(selected)
        current = github_api("PATCH", path, {"body": body})
    return current["html_url"], previous_rows + selected


def replace_issue_readings(issue: dict, updates: dict[str, dict]) -> str:
    """Change collector fields in place while retaining surrounding human text."""
    body = issue.get("body") or ""
    rows = issue_rows(issue)
    original = {normalize(row["word"]): row for row in rows}
    replacements = {}
    for key, update in updates.items():
        current = original.get(key)
        # A reading manually filled in after discovery must win over the backfill.
        if current and current["reading"] == "要確認":
            replacements[key] = {**current, **update}
    if not replacements:
        return body

    def metadata(match):
        data = json.loads(match[1])
        data["rows"] = [replacements.get(normalize(row["word"]), row) for row in data["rows"]]
        encoded = json.dumps(data, ensure_ascii=False).replace("--", "\\u002d\\u002d")
        return "<!-- new-word-entries\n" + encoded + "\n-->"

    body = ENTRIES_RE.sub(metadata, body)
    # Legacy Issues need metadata before unresolved entries leave the import TSV.
    if not ENTRIES_RE.search(body):
        encoded = json.dumps({"version": 1, "rows": [replacements.get(normalize(row["word"]), row) for row in rows]},
                             ensure_ascii=False).replace("--", "\\u002d\\u002d")
        body += "\n\n<!-- new-word-entries\n" + encoded + "\n-->"
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = line.rstrip("\r\n").split("|")
        if len(cells) not in {7, 8}:
            continue
        key = normalize(html.unescape(cells[2].strip()))
        row = replacements.get(key)
        if row is None or html.unescape(cells[1].strip()) != "要確認":
            continue
        old_notes = render_rows([original[key]]).splitlines()[2].split("|")[-2].strip()
        new_notes = render_rows([row]).splitlines()[2].split("|")[-2].strip()
        preserved = [note for note in cells[-2].strip().split(" / ")
                     if note and note not in old_notes.split(" / ")
                     and note not in {"読みは自動推測せず要確認", "読みの明記が未確認"}]
        cells[1] = " " + markdown_cell(row["reading"]) + " "
        cells[-2] = " " + " / ".join(([new_notes] if new_notes else []) + preserved) + " "
        lines[index] = "|".join(cells) + ("\n" if line.endswith("\n") else "")
    body = "".join(lines)

    def tsv(match):
        output = io.StringIO()
        reader = csv.DictReader(io.StringIO(match[1]), delimiter="\t")
        if (reader.fieldnames or [])[:5] != ["読み", "表記", "左ID", "右ID", "品詞"]:
            return match[0]
        writer = csv.DictWriter(output, fieldnames=reader.fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        present = set()
        for entry in reader:
            key = normalize(entry.get("表記") or "")
            if key in replacements and entry["読み"] == "要確認":
                entry["読み"] = replacements[key]["reading"]
            if key in original and not kana_reading(entry["読み"]):
                continue
            writer.writerow(entry)
            present.add(key)
        # Previously unresolved rows may have been excluded from this TSV.
        marker = ENTRIES_RE.search(body, match.end())
        group = json.loads(marker[1])["rows"] if marker else rows
        for row in group:
            key = normalize(row["word"])
            if key in replacements and key not in present and kana_reading(replacements[key]["reading"]):
                row = replacements[key]
                entry = {"読み": row["reading"], "表記": row["word"], "左ID": row["id"], "右ID": row["id"], "品詞": row["pos"],
                         "分類": ",".join(row.get("categories", [])), "辞書照合": "未確認" if row.get("dictionary_check") == {"status": "not_checked"} else "", "辞書リリース": row.get("dictionary", {}).get("release", ""),
                         "辞書リポジトリ": row.get("dictionary", {}).get("repository", ""),
                         "辞書manifestSHA256": row.get("dictionary", {}).get("manifest_sha256", "")}
                writer.writerow({field: entry.get(field, "") for field in reader.fieldnames})
                present.add(key)
        return "```tsv\n" + output.getvalue() + "```"

    body = re.sub(r"```tsv\s*\n(.*?)```", tsv, body, flags=re.DOTALL)
    notice = "読み未確定の語はTSVから除外しています。"
    if any(row["reading"] == "要確認" for row in issue_rows({**issue, "body": body})):
        if notice not in body:
            body = body.replace("## TSV", "## TSV\n\n" + notice, 1)
    else:
        body = body.replace(notice + "\n\n", "")
    return body


def sync_seen_readings(rows: list[dict]) -> int:
    if not SEEN_PATH.exists():
        return 0
    confirmed = {normalize(row["word"]): row["reading"] for row in rows if kana_reading(row["reading"])}
    with SEEN_PATH.open(encoding="utf-8", newline="") as stream:
        records = list(csv.DictReader(stream, delimiter="\t"))
    changed = 0
    for row in records:
        reading = confirmed.get(normalize(row["word"]))
        if reading and row["reading"] == "要確認":
            row["reading"] = reading
            changed += 1
    if changed:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=SEEN_PATH.parent,
                                             prefix=".seen-", delete=False) as stream:
                temporary = Path(stream.name)
                writer = csv.DictWriter(stream, fieldnames=SEEN_FIELDS, delimiter="\t", lineterminator="\n")
                writer.writeheader()
                writer.writerows(records)
            temporary.replace(SEEN_PATH)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return changed


def refresh_issue_readings(issues: list[dict], *, limit: int | None = 10,
                           resolver: ReadingResolver | None = None, now: dt.datetime | None = None) -> int:
    resolver = resolver or ReadingResolver(page_limit=None if limit is None else 20)
    pending = [(issue["number"], normalize(row["word"]))
               for issue in sorted(issues, key=lambda item: item["number"])
               if re.fullmatch(r"新語候補 \d{4}-\d{2}-\d{2}", issue.get("title", ""))
               for row in issue_rows(issue) if row["reading"] == "要確認"]
    if pending and limit is not None:
        offset = (now or dt.datetime.now(TZ)).date().toordinal() * limit % len(pending)
        pending = (pending[offset:] + pending[:offset])[:limit]
    selected = set(pending)
    attempted = 0
    changed = 0
    confirmed = []
    # Rotate the bounded daily backlog so later Issues also receive retries.
    for issue in sorted(issues, key=lambda item: item["number"]):
        if not re.fullmatch(r"新語候補 \d{4}-\d{2}-\d{2}", issue.get("title", "")):
            continue
        updates = {}
        for row in issue_rows(issue):
            if (issue["number"], normalize(row["word"])) not in selected:
                continue
            attempted += 1
            print(f"Resolve existing reading: #{issue['number']} {row['word']}", flush=True)
            updates[normalize(row["word"])] = resolver.resolve(row["word"], row.get("sources", []))
        path = f"/repos/{REPO}/issues/{issue['number']}"
        if updates:
            current = github_api("GET", path)
            body = replace_issue_readings(current, updates)
            if body != (current.get("body") or ""):
                current = github_api("PATCH", path, {"body": body})
                changed += 1
                print(f"Updated reading Issue: {current['html_url']}", flush=True)
            issue.update(current)
        # Also recover a local history update after a successful Issue PATCH.
        confirmed.extend(issue_rows(issue))
    saved = sync_seen_readings(confirmed)
    print(f"Reading refresh: checked {attempted}, updated {changed} Issues, synced {saved} seen readings", flush=True)
    resolved = sum(bool(kana_reading(row["reading"])) for row in confirmed)
    write_summary(["## 読みの再確認", f"確認処理: {attempted}語 / Issue更新: {changed}件 / 履歴の読み更新: {saved}語",
                   f"既存候補の確定済み: {resolved}語 / 未確定: {len(confirmed) - resolved}語"])
    return changed


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def main(now: dt.datetime | None = None) -> int:
    return candidate_pipeline.run(SimpleNamespace(**globals()), now)


if __name__ == "__main__":
    if sys.argv[1:] == ["--refresh-readings"]:
        try:
            refresh_issue_readings(list_issues(), limit=None)
        except Exception as error:
            print(f"Reading refresh failed: {error}", file=sys.stderr)
            raise SystemExit(1)
    elif sys.argv[1:]:
        raise SystemExit("Usage: collect_new_words.py [--refresh-readings]")
    else:
        raise SystemExit(main())
