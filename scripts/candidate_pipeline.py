"""Category discovery, independent usage evidence, and a durable review queue."""
from __future__ import annotations

import datetime as dt
import difflib
import html
import json
import re
import tempfile
import unicodedata
import urllib.error
import urllib.parse
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

OFFICIAL_DOMAINS = (
    "prtimes.jp", "sony.jp", "sony.com", "microsoft.com", "apple.com",
    "google.com", "blog.google", "openai.com", "nintendo.com", "nintendo.co.jp",
    "bandainamco.co.jp", "sunrise-world.net", "visualcomponents.com",
)
KINDS = {"common", "proper", "product", "work", "person", "organization", "place"}


def load_categories(path: Path) -> list[dict]:
    categories = json.loads(path.read_text(encoding="utf-8"))
    identities = set()
    for category in categories:
        if not re.fullmatch(r"[a-z_]+", category["id"]) or category["id"] in identities or not category["label"]:
            raise ValueError("Invalid category configuration")
        identities.add(category["id"])
        if not category["queries"] or any(not query["q"] or query["kind"] not in KINDS for query in category["queries"]):
            raise ValueError("Invalid category queries")
    if not categories:
        raise ValueError("No configured categories")
    return categories


def load_pending(path: Path, normalize) -> dict[str, dict]:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("version") not in {1, 2} or not isinstance(document.get("rows"), list):
        raise ValueError("Unsupported pending queue")
    result = {}
    for row in document["rows"]:
        if not isinstance(row.get("word"), str) or not row["word"].strip() or not isinstance(row.get("categories"), list) or not isinstance(row.get("sources"), list):
            raise ValueError("Invalid pending candidate")
        result[normalize(row["word"])] = row
    return result


def save_pending(path: Path, rows: dict[str, dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": 1, "rows": [rows[key] for key in sorted(rows)]}, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publisher_family(item: dict, c) -> str:
    if item.get("publisher_family"):
        return item["publisher_family"]
    text = c.normalize(item.get("description", "") + " " + item.get("source", ""))
    for agency in ("共同通信", "時事通信", "ロイター", "Reuters", "PR TIMES"):
        if c.normalize(agency) in text:
            return c.normalize(agency)
    return c.normalize(item.get("source", ""))


def headline(item: dict, c) -> str:
    return c.normalize(re.sub(r"\s+-\s+[^-]+$", "", item.get("title", "")))


def independent_sources(items: list[dict], c) -> list[dict]:
    ordered = sorted(items, key=lambda item: (-(c.article_date(item).timestamp() if c.article_date(item) else 0), item.get("link", "")))
    def distinct(first, second):
        return (publisher_family(first, c) and publisher_family(second, c)
                and publisher_family(first, c) != publisher_family(second, c)
                and first.get("link") and second.get("link") and first["link"] != second["link"]
                and headline(first, c) != headline(second, c))
    for first in ordered:
        for second in ordered:
            if distinct(first, second):
                selected = [first, second]
                for third in ordered:
                    if all(distinct(third, item) for item in selected):
                        selected.append(third)
                        break
                return selected
    return []


class OfficialLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attributes = dict(attrs)
            self.current = [attributes.get("href", ""), attributes.get("aria-label", "") + attributes.get("title", "")]
    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data
    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            if re.search(r"公式(?:サイト|ページ|発表)|オフィシャル", self.current[1]):
                self.links.append(self.current[0])
            self.current = None


def document(link: str, readings, c):
    if link in readings.documents:
        return readings.documents[link]
    if readings.page_limit is not None and readings.page_count >= readings.page_limit:
        return None
    readings.page_count += 1
    try:
        direct = c.publisher_url(link)
        result = (direct, c.public_reading_document(direct))
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, LookupError):
        result = None
    readings.documents[link] = result
    if result is not None:
        readings.documents[result[0]] = result
    return result


def verified_usage(word: str, sources: list[dict], readings, c) -> list[dict]:
    """Verify the pages and collapse common syndication despite different headlines."""
    selected, urls, hosts, families, bodies = [], set(), set(), set(), []
    for source in sources:
        page = document(source["link"], readings, c)
        if page is None:
            continue
        direct, markup = page
        if (urllib.parse.urlsplit(direct).hostname or "").lower() == "news.google.com":
            continue
        parser = c.ReadingHTML()
        parser.feed(markup)
        text = c.normalize(" ".join(parser.parts))
        if c.normalize(word) not in text or not re.search(r"[ぁ-ゖァ-ヶ一-龠]", text):
            continue
        parsed = urllib.parse.urlsplit(direct)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        family = publisher_family({**source, "description": source.get("description", "") + " " + text}, c)
        query = urllib.parse.urlencode([(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if not key.startswith("utm_") and key not in {"fbclid", "gclid"}])
        identity = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, query, ""))
        # Identical visible articles on different sites remain one usage source.
        if identity in urls or host in hosts or family in families or any(text == body or (len(text) >= 200 and len(body) >= 200 and
                difflib.SequenceMatcher(None, text[:8000], body[:8000]).ratio() >= .94) for body in bodies):
            continue
        selected.append({**source, "link": direct, "publisher_family": family})
        urls.add(identity)
        hosts.add(host)
        families.add(family)
        bodies.append(text)
    return selected


def official_name(word: str, sources: list[dict], readings, c) -> list[dict]:
    reviewed = readings.reviewed.get(c.normalize(word), {})
    context = c.normalize(" ".join(source.get("title", "") for source in sources))
    if reviewed.get("context") and not any(c.normalize(term) in context for term in reviewed["context"]):
        reviewed = {}
    candidates = [(source, True) for source in reviewed.get("official_name_sources", [])]
    for source in sources:
        page = document(source["link"], readings, c)
        if page is None:
            continue
        direct, markup = page
        host = (urllib.parse.urlsplit(direct).hostname or "").lower()
        trusted = host.endswith(".go.jp") or any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)
        if trusted:
            candidates.append(({"source": source["source"], "link": direct}, True))
        parser = OfficialLinks()
        parser.feed(markup)
        for link in parser.links[:2]:
            target = urllib.parse.urljoin(direct, link)
            if urllib.parse.urlsplit(target).scheme == "https":
                candidates.append(({"source": "記事が示す公式サイト", "link": target}, True))
    for source, _ in candidates:
        page = document(source["link"], readings, c)
        if page is not None:
            direct, markup = page
            parser = c.ReadingHTML()
            parser.feed(markup)
            text = c.normalize(" ".join(parser.parts))
            if c.normalize(word) in text:
                return [{"source": source["source"], "link": direct}]
    return []


def kind_for(title: str, word: str, default: str) -> str:
    title = unicodedata.normalize("NFKC", html.unescape(title))
    start = title.find(word)
    before, after = title[max(0, start - 24):start], title[start + len(word):start + len(word) + 30]
    for pattern, kind in (("製品|サービス|ブランド|アプリ|モデル", "product"), ("ゲーム|アニメ|新曲|作品", "work"), ("新駅|地名|施設", "place"), ("組織|研究所|社名", "organization"), ("人名|人物|選手|歌手|俳優", "person")):
        if re.search(r"(?:" + pattern + r")[「『\s]*$", before):
            return kind
    if re.search(r"(?:用語|新語|俗語|略語|言葉)[「『\s]*$", before):
        return "common"
    if re.search(r"(?:名称|名付けた|名づけた|命名)[「『\s]*$", before):
        return "proper"
    if default == "common" and re.match(r"[」』\s]*(?:とは|の意味|という(?:用語|言葉))", after):
        return "common"
    return default


def round_robin(rows: list[dict], categories: list[dict], limit: int) -> list[dict]:
    buckets = {category["id"]: [] for category in categories}
    for row in rows:
        buckets[row["category"]].append(row)
    result = []
    while len(result) < limit:
        progress = False
        for bucket in buckets.values():
            if bucket and len(result) < limit:
                result.append(bucket.pop(0))
                progress = True
        if not progress:
            break
    return result
