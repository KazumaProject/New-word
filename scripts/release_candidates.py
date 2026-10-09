"""Japanese usage and reading checks for the independent Release collector."""
from __future__ import annotations

import datetime as dt
import email.utils
import html
from html.parser import HTMLParser
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from pathlib import Path
from zoneinfo import ZoneInfo

import candidate_pipeline

TZ = ZoneInfo("America/Toronto")
READINGS_PATH = Path(__file__).resolve().parents[1] / "data/readings.json"
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


def publisher_url(url: str, *, fetch=None, api_request=None) -> str:
    """Resolve the public Google News article wrapper to its publisher URL."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != "news.google.com":
        return url
    if not re.fullmatch(r"/(?:rss/)?(?:articles|read)/[A-Za-z0-9_-]+", parts.path):
        raise ValueError("Unsupported Google News link")
    wrapper = (fetch or public_reading_document)(url)
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
    raw = (api_request or request)("https://news.google.com/_/DotsSplashUi/data/batchexecute",
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


def require_official_name(word, sources, readings):
    return candidate_pipeline.official_name(word, sources, readings, SimpleNamespace(**globals()))


def verify_usage_sources(word, sources, readings):
    return candidate_pipeline.verified_usage(word, sources, readings, SimpleNamespace(**globals()))
