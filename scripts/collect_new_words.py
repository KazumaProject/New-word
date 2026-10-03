#!/usr/bin/env python3
from __future__ import annotations

import csv
import datetime as dt
import email.utils
import html
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
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = os.environ.get("GITHUB_REPOSITORY", "KazumaProject/New-word")
TOKEN = os.environ.get("GH_TOKEN", "")
CODE_SEARCH_TOKEN = os.environ.get("CODE_SEARCH_TOKEN", "")
TZ = ZoneInfo("America/Toronto")
DAILY_LIMIT = 10
SEEN_PATH = Path("data/seen.tsv")
SEEN_FIELDS = ["date", "reading", "word", "pos", "id", "normalized"]
MOZC_ID_DEF = "https://raw.githubusercontent.com/google/mozc/master/src/data/dictionary_oss/id.def"

SEARCH_QUERIES = (
    '"提供開始" 新サービス',
    '"発表" 新製品',
    '"開発" 新技術',
    '"新機能" 発表',
    '"新ブランド" 発表',
    '"新設" 研究',
)
FALLBACK_QUERIES = (
    'AI モデル "発表"',
    '医療 技術 "研究"',
    '宇宙 "発表"',
    'ゲーム "発売"',
    'ブランド "名称"',
)
SEARCH_STAGES = (
    ("直近24時間", 1, SEARCH_QUERIES),
    ("補充: 直近30日", 30, SEARCH_QUERIES + FALLBACK_QUERIES),
    ("補充: 直近365日", 365, SEARCH_QUERIES + FALLBACK_QUERIES),
    ("補充: 期間制限なし", None, SEARCH_QUERIES + FALLBACK_QUERIES),
)

GENERIC_REJECT = {
    "サービス", "新サービス", "新製品", "新技術", "新機能", "新ブランド",
    "生成AI", "人工知能", "スマートフォン", "アプリ", "システム", "プラットフォーム",
    "プロジェクト", "サービス開始", "提供開始", "研究開発", "株式会社",
}
ORG_HINTS = (
    "研究所", "研究センター", "研究室", "ラボ", "Lab", "Research",
    "チーム", "委員会", "協会", "機構", "財団", "連盟",
)
QUOTE_RE = re.compile(r'[「『“"]([^」』”"]{2,60})[」』”"]')
ASCII_NAME_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9+._-]*)(?:\s+[A-Z][A-Za-z0-9+._-]*){0,4}\b")
ENTRIES_RE = re.compile(r"<!-- new-word-entries\s*\n(.*?)\n-->", re.DOTALL)


class CodeSearchDeferred(RuntimeError):
    """The provider requires a cooldown before any more code searches."""


class CodeSearchLimiter:
    """Space every attempt, including retries, to stay below 10 requests/minute."""

    def __init__(self) -> None:
        self.last_request: float | None = None

    def wait(self) -> None:
        if self.last_request is not None:
            delay = 6.1 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
        self.last_request = time.monotonic()


CODE_SEARCH_LIMITER = CodeSearchLimiter()


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
    code_search: bool = False,
) -> bytes:
    h = {"User-Agent": "KazumaProject-New-word/2.0"}
    if headers:
        h.update(headers)
    # Retrying a write after a lost response can create a duplicate Issue.
    attempts = 3 if method == "GET" else 1
    for attempt in range(1, attempts + 1):
        if code_search:
            CODE_SEARCH_LIMITER.wait()
        try:
            req = urllib.request.Request(url, data=body, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
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
            rate_limited = error.code == 429 or error.code == 403
            if code_search and rate_limited and (delay > 60 or attempt == attempts):
                raise CodeSearchDeferred(
                    f"GitHub code search requires a {delay:.0f}s cooldown; "
                    "retry later or optionally configure CODE_SEARCH_TOKEN for a dedicated search token"
                ) from error
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


def fetch_id_map() -> dict[str, int]:
    out = {}
    for line in request(MOZC_ID_DEF).decode("utf-8").splitlines():
        if line.strip():
            number, label = line.split(maxsplit=1)
            out[label] = int(number)
    required = {pos_label("新製品"), pos_label("新研究所")}
    if not required.issubset(out):
        raise RuntimeError("Mozc id.def is missing a required POS label")
    return out


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
    if re.fullmatch(r"[0-9.,%＋+\-]+", s):
        return None
    if re.search(r"(とは|について|に関する|を発表|を提供|を開始|を開発)$", s):
        return None
    if len(re.findall(r"[ぁ-んァ-ヶ一-龠A-Za-z]", s)) < 2:
        return None
    return s


def extract_candidates(title: str) -> set[str]:
    body = re.sub(r"\s+-\s+[^-]+$", "", title)
    found = set()
    for match in QUOTE_RE.finditer(body):
        candidate = clean_candidate(match.group(1))
        if candidate:
            found.add(candidate)
    for match in ASCII_NAME_RE.finditer(body):
        candidate = clean_candidate(match.group(0))
        if candidate and len(candidate) >= 3 and candidate.lower() not in {"ai", "dx", "iot"}:
            found.add(candidate)
    return found


def reading_hint(word: str) -> str:
    if re.fullmatch(r"[ァ-ヶー・\s]+", word):
        return "".join(chr(ord(ch) - 0x60) if 0x30A1 <= ord(ch) <= 0x30F6 else ch for ch in word)
    return "要確認"


def pos_label(word: str) -> str:
    if any(hint in word for hint in ORG_HINTS):
        return "名詞,固有名詞,組織,*,*,*,*"
    return "名詞,固有名詞,一般,*,*,*,*"


def github_api(method: str, path: str, payload: dict | None = None):
    code_search = path.startswith("/search/code?")
    token = (CODE_SEARCH_TOKEN or TOKEN) if code_search else TOKEN
    if not token:
        raise RuntimeError("GH_TOKEN is not set")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    raw = request(
        "https://api.github.com" + path, headers=headers, method=method, body=body,
        code_search=code_search,
    )
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
        if data.get("version") != 1 or not isinstance(data.get("rows"), list):
            raise RuntimeError(f"Issue #{issue['number']} has unsupported candidate metadata")
        for row in data["rows"]:
            if not isinstance(row, dict) or any(not isinstance(row.get(field), str) for field in SEEN_FIELDS):
                raise RuntimeError(f"Issue #{issue['number']} has invalid candidate metadata")
            key = normalize(row["word"])
            if not key or normalize(row["normalized"]) != key:
                raise RuntimeError(f"Issue #{issue['number']} has inconsistent candidate metadata")
            result[key] = row
    for match in re.finditer(r"```tsv\s*\n(.*?)```", body, re.DOTALL):
        for row in csv.DictReader(io.StringIO(match.group(1)), delimiter="\t"):
            word = row.get("表記")
            if not word or not all(row.get(field) for field in ("読み", "左ID", "品詞")):
                continue
            key = normalize(word)
            result.setdefault(key, {
                "date": date, "reading": row["読み"], "word": word,
                "pos": row["品詞"], "id": row["左ID"], "normalized": key,
                "sources": [], "supplement": False,
            })
    return list(result.values())


def already_in_japanese_keyboard(word: str) -> bool:
    params = urllib.parse.urlencode({"q": f'"{word}" repo:KazumaProject/JapaneseKeyboard', "per_page": 1})
    data = github_api("GET", "/search/code?" + params)
    if data.get("incomplete_results") or type(data.get("total_count")) is not int:
        raise RuntimeError(f"Code search did not complete for {word!r}")
    return data["total_count"] > 0


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


def collect_candidates(now: dt.datetime, excluded: set[str], id_map: dict[str, int], limit: int) -> list[dict]:
    checked = {}
    reference = now.astimezone(dt.timezone.utc)
    for label, days, queries in SEARCH_STAGES:
        counts = Counter()
        evidence = {}
        print(f"Search: {label}", flush=True)
        try:
            cutoff = reference - dt.timedelta(days=days) if days is not None else None
            for query in queries:
                counts["queries"] += 1
                rss_query = f"{query} when:{days}d" if days is not None else query
                print(f"Fetch RSS ({counts['queries']}/{len(queries)}): {rss_query}", flush=True)
                for item in google_news_rss(rss_query):
                    counts["articles"] += 1
                    published = article_date(item)
                    if published is None or published > reference:
                        counts["invalid_or_future_date"] += 1
                        continue
                    if cutoff is not None and published < cutoff:
                        counts["outside_window"] += 1
                        continue
                    for word in sorted(extract_candidates(item["title"])):
                        key = normalize(word)
                        entry = evidence.setdefault(key, {"variants": {}, "items": {}})
                        entry["variants"][word] = max(published, entry["variants"].get(word, published))
                        entry["items"][(normalize(item["source"]), item["link"])] = item

            counts["candidates"] = len(evidence)
            ranked = []
            for key, entry in evidence.items():
                if key in excluded:
                    counts["already_seen"] += 1
                    continue
                items = list(entry["items"].values())
                publishers = {normalize(item["source"]) for item in items if item["source"].strip()}
                if len(publishers) < 2 or len({item["link"] for item in items}) < 2:
                    counts["fewer_than_two_publishers"] += 1
                    continue
                word = min(entry["variants"], key=lambda value: (-entry["variants"][value].timestamp(), value))
                pos = pos_label(word)
                if pos not in id_map:
                    counts["missing_pos_id"] += 1
                    continue
                newest = max(article_date(item).timestamp() for item in items)
                ranked.append((-len(publishers), -newest, word, key, pos, items))

            accepted = []
            for _, _, word, key, pos, items in sorted(ranked):
                sources = select_sources(items)
                if len(sources) < 2:
                    counts["fewer_than_two_distinct_sources"] += 1
                    continue
                if key not in checked:
                    print(f"Check existing word: {word}", flush=True)
                    try:
                        checked[key] = already_in_japanese_keyboard(word)
                    except CodeSearchDeferred as error:
                        counts["code_search_deferred"] += 1
                        print(f"{error}; confirmed candidates: {len(accepted)}", flush=True)
                        if not accepted:
                            raise
                        break
                if checked[key]:
                    counts["already_registered"] += 1
                    continue
                accepted.append({
                    "date": now.date().isoformat(), "reading": reading_hint(word), "word": word,
                    "pos": pos, "id": str(id_map[pos]), "normalized": key, "sources": sources,
                    "supplement": days != 1,
                })
                if len(accepted) >= limit:
                    break
            counts["accepted"] = len(accepted)
            if accepted:
                return accepted
        finally:
            print(f"Results ({label}): {json.dumps(dict(counts), ensure_ascii=False, sort_keys=True)}", flush=True)
    return []


def markdown_cell(value: str) -> str:
    return html.escape(value, quote=False).replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ")


def render_rows(rows: list[dict]) -> str:
    lines = [
        "| 読み | 表記 | Mozc品詞 | id.def ID | 根拠/出典 | 備考 |",
        "|---|---|---|---:|---|---|",
    ]
    for row in rows:
        sources = "<br>".join(
            f"[{markdown_cell(source['source'])}](<{source['link']}>)" for source in row["sources"]
        )
        notes = []
        if row["reading"] == "要確認":
            notes.append("読みは自動推測せず要確認")
        if row["supplement"]:
            notes.append("補充候補: 過去の未登録語を含む")
        lines.append(
            f"| {markdown_cell(row['reading'])} | {markdown_cell(row['word'])} | "
            f"`{row['pos']}` | {row['id']} | {sources} | {' / '.join(notes)} |"
        )
    lines += ["", "## TSV", "", "```tsv", "読み\t表記\t左ID\t右ID\t品詞"]
    for row in rows:
        lines.append(f"{row['reading']}\t{row['word']}\t{row['id']}\t{row['id']}\t{row['pos']}")
    lines += ["```", ""]
    # Escape comment delimiters so a candidate cannot truncate the metadata.
    metadata = json.dumps({"version": 1, "rows": rows}, ensure_ascii=False).replace("--", "\\u002d\\u002d")
    lines += ["<!-- new-word-entries", metadata, "-->"]
    return "\n".join(lines)


def render_issue(title: str, rows: list[dict]) -> str:
    return "\n".join([
        f"# {title}", "", render_rows(rows), "", "## 重複チェック", "",
        f"- 過去の {REPO} Issue と data/seen.tsv",
        "- KazumaProject/JapaneseKeyboard のGitHubコード検索",
        "- 同一候補はNFKC正規化 + 大文字小文字正規化で比較",
        "", "## 品詞", "", f"Mozc 最新 id.def: {MOZC_ID_DEF}", "",
        "> 注: JapaneseKeyboard のバイナリ辞書内部は未確認です。コード検索で見つからないことは、辞書本体に未登録であることを保証しません。",
        "", "_Generated automatically by GitHub Actions without a paid AI API._",
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
        body = (current.get("body") or "") + "\n\n## 追加候補\n\n" + render_rows(selected)
        current = github_api("PATCH", path, {"body": body})
    return current["html_url"], previous_rows + selected


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def main(now: dt.datetime | None = None) -> int:
    now = (now or dt.datetime.now(TZ)).astimezone(TZ)
    title = f"新語候補 {now.date().isoformat()}"
    print(f"Collect: Toronto {now:%Y-%m-%d %H:%M %Z}; daily limit {DAILY_LIMIT}", flush=True)
    try:
        issues = list_issues()
        matches = [issue for issue in issues if issue.get("title") == title]
        existing = min(matches, key=lambda issue: issue["number"]) if matches else None
        previous_rows = issue_rows(existing) if existing else []
        # An existing Issue also recovers seen.tsv after a failed database push.
        append_seen(previous_rows)
        remaining = max(0, DAILY_LIMIT - len(previous_rows))
        if remaining == 0:
            print(f"Daily Issue already has {len(previous_rows)} candidates: {existing['html_url']}")
            write_summary([f"## {title}", f"掲載済み: {len(previous_rows)}語。上限に達しているため追加なし。", existing["html_url"]])
            return 0

        excluded = load_seen()
        for issue in issues:
            excluded.update(normalize(row["word"]) for row in issue_rows(issue))
        deferred = None
        try:
            accepted = collect_candidates(now, excluded, fetch_id_map(), remaining)
        except CodeSearchDeferred as error:
            if not previous_rows:
                raise
            deferred = str(error)
            accepted = []
        if not accepted:
            if previous_rows:
                print(f"No additional candidates; keeping {len(previous_rows)} published candidates.")
                reason = "コード検索の待機制限により追加なし。" if deferred else "追加候補なし。"
                write_summary([f"## {title}", f"掲載済み: {len(previous_rows)}語。{reason}", existing["html_url"]])
                return 0
            raise RuntimeError("All search stages exhausted: no verified, previously unlisted candidates")

        url, published = publish_candidates(title, accepted, existing)
        saved = append_seen(published)
        print(f"Created/updated: {url}\nDaily candidates: {len(published)}\nNew seen records: {saved}")
        write_summary([f"## {title}", f"掲載: {len(published)}語 / 履歴への追加: {saved}語", url])
        return 0
    except Exception as error:
        print(f"Collection failed: {error}", file=sys.stderr, flush=True)
        write_summary([f"## {title}: 失敗", f"理由: {error}", "探索件数と除外理由は Collect new words のログを確認してください。"])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
