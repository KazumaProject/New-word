#!/usr/bin/env python3
from __future__ import annotations

import csv
import datetime as dt
import html
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = os.environ.get("GITHUB_REPOSITORY", "KazumaProject/New-word")
TOKEN = os.environ.get("GH_TOKEN", "")
FORCE_RUN = os.environ.get("FORCE_RUN") == "1"
TZ = ZoneInfo("America/Toronto")

SEEN_PATH = Path("data/seen.tsv")
MOZC_ID_DEF = "https://raw.githubusercontent.com/google/mozc/master/src/data/dictionary_oss/id.def"

SEARCH_QUERIES = [
    '"提供開始" 新サービス',
    '"発表" 新製品',
    '"開発" 新技術',
    '"新機能" 発表',
    '"新ブランド" 発表',
    '"新設" 研究',
]

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


def request(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    h = {"User-Agent": "KazumaProject-New-word/1.0"}
    if headers:
        h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
        return r.read()


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).strip()
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    result = set()
    with SEEN_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row.get("normalized"):
                result.add(row["normalized"])
    return result


def fetch_id_map() -> dict[str, int]:
    text = request(MOZC_ID_DEF).decode("utf-8")
    out: dict[str, int] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        n, label = line.split(" ", 1)
        out[label] = int(n)
    return out


def google_news_rss(query: str) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({
        "q": query,
        "hl": "ja",
        "gl": "JP",
        "ceid": "JP:ja",
    })
    data = request("https://news.google.com/rss/search?" + params)
    root = ET.fromstring(data)
    items = []
    for item in root.findall("./channel/item"):
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        source = (item.findtext("source") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "pubdate": pubdate, "source": source})
    return items


def clean_candidate(raw: str) -> str | None:
    s = unicodedata.normalize("NFKC", html.unescape(raw)).strip(" \t\r\n・:：-—")
    s = re.sub(r"\s+", " ", s)
    if not (2 <= len(s) <= 50):
        return None
    if s in GENERIC_REJECT:
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
    found: set[str] = set()
    for m in QUOTE_RE.finditer(body):
        c = clean_candidate(m.group(1))
        if c:
            found.add(c)
    for m in ASCII_NAME_RE.finditer(body):
        c = clean_candidate(m.group(0))
        if c and len(c) >= 3 and c.lower() not in {"ai", "dx", "iot"}:
            found.add(c)
    return found


def reading_hint(word: str) -> str:
    # Do not fabricate readings for Latin/product names. Katakana can be converted safely.
    if re.fullmatch(r"[ァ-ヶー・\s]+", word):
        out = []
        for ch in word:
            code = ord(ch)
            if 0x30A1 <= code <= 0x30F6:
                out.append(chr(code - 0x60))
            else:
                out.append(ch)
        return "".join(out)
    return "要確認"


def pos_label(word: str) -> str:
    if any(h in word for h in ORG_HINTS):
        return "名詞,固有名詞,組織,*,*,*,*"
    return "名詞,固有名詞,一般,*,*,*,*"


def github_api(method: str, path: str, payload: dict | None = None):
    if not TOKEN:
        raise RuntimeError("GH_TOKEN is not set")
    url = "https://api.github.com" + path
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def past_issue_text() -> str:
    owner, repo = REPO.split("/", 1)
    page = 1
    chunks = []
    while page <= 10:
        issues = github_api("GET", f"/repos/{owner}/{repo}/issues?state=all&per_page=100&page={page}")
        if not issues:
            break
        for i in issues:
            if "pull_request" not in i:
                chunks.append((i.get("title") or "") + "\n" + (i.get("body") or ""))
        if len(issues) < 100:
            break
        page += 1
    return normalize("\n".join(chunks))


def already_in_japanese_keyboard(word: str) -> bool:
    q = urllib.parse.quote(f'"{word}" repo:KazumaProject/JapaneseKeyboard')
    try:
        data = github_api("GET", f"/search/code?q={q}&per_page=1")
        return bool(data.get("total_count"))
    except Exception:
        return False


def create_or_update_issue(title: str, body: str) -> str:
    owner, repo = REPO.split("/", 1)
    q = urllib.parse.quote(f'repo:{REPO} is:issue in:title "{title}"')
    result = github_api("GET", f"/search/issues?q={q}&per_page=10")
    for item in result.get("items", []):
        if item.get("title") == title:
            issue = github_api(
                "PATCH",
                f"/repos/{owner}/{repo}/issues/{item['number']}",
                {"body": body},
            )
            return issue["html_url"]
    issue = github_api("POST", f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})
    return issue["html_url"]


def append_seen(rows: list[dict[str, str]]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    exists = SEEN_PATH.exists()
    with SEEN_PATH.open("a", encoding="utf-8", newline="") as f:
        fieldnames = ["date", "reading", "word", "pos", "id", "normalized"]
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow(row)


def main() -> int:
    now = dt.datetime.now(TZ)
    if not FORCE_RUN and now.hour != 19:
        print(f"Skip: Toronto local time is {now:%Y-%m-%d %H:%M %Z}, not 19:xx")
        return 0

    id_map = fetch_id_map()
    seen = load_seen()
    previous = past_issue_text()

    evidence: dict[str, list[dict[str, str]]] = {}
    for query in SEARCH_QUERIES:
        for item in google_news_rss(query):
            for candidate in extract_candidates(item["title"]):
                evidence.setdefault(candidate, []).append(item)

    accepted = []
    for word, items in evidence.items():
        key = normalize(word)
        if key in seen or key in previous:
            continue

        publishers = {normalize(i["source"]) for i in items if i["source"]}
        if len(publishers) < 2:
            continue

        if already_in_japanese_keyboard(word):
            continue

        label = pos_label(word)
        pos_id = id_map.get(label)
        if pos_id is None:
            continue

        unique_sources = []
        seen_links = set()
        for item in items:
            if item["link"] not in seen_links:
                seen_links.add(item["link"])
                unique_sources.append(item)
            if len(unique_sources) >= 3:
                break

        accepted.append({
            "date": now.date().isoformat(),
            "reading": reading_hint(word),
            "word": word,
            "pos": label,
            "id": str(pos_id),
            "normalized": key,
            "sources": unique_sources,
        })

    accepted.sort(key=lambda x: x["word"])
    if not accepted:
        print("No new candidates passed the filters.")
        return 0

    lines = [
        f"# 新語候補 {now.date().isoformat()}",
        "",
        "| 読み | 表記 | Mozc品詞 | id.def ID | 根拠/出典 | 備考 |",
        "|---|---|---|---:|---|---|",
    ]
    for row in accepted:
        srcs = "<br>".join(
            f"[{s['source'] or 'source'}]({s['link']})" for s in row["sources"]
        )
        note = "読みは自動推測せず要確認" if row["reading"] == "要確認" else ""
        lines.append(
            f"| {row['reading']} | {row['word']} | `{row['pos']}` | {row['id']} | {srcs} | {note} |"
        )

    lines += [
        "",
        "## TSV",
        "",
        "```tsv",
        "読み\t表記\t左ID\t右ID\t品詞",
    ]
    for row in accepted:
        lines.append(
            f"{row['reading']}\t{row['word']}\t{row['id']}\t{row['id']}\t{row['pos']}"
        )
    lines += [
        "```",
        "",
        "## 重複チェック",
        "",
        "- 過去の KazumaProject/New-word Issue",
        "- data/seen.tsv",
        "- KazumaProject/JapaneseKeyboard のGitHubコード検索",
        "- 同一候補はNFKC正規化 + 大文字小文字正規化で比較",
        "",
        "## 品詞",
        "",
        f"Mozc 最新 id.def: {MOZC_ID_DEF}",
        "",
        "> 注: JapaneseKeyboard の一部辞書はバイナリアセットのため、GitHubコード検索だけでは内部語彙を完全走査できません。自動化で採用した語は seen.tsv と過去Issueで永続的に重複防止します。",
        "",
        "_Generated automatically by GitHub Actions without a paid AI API._",
    ]

    title = f"新語候補 {now.date().isoformat()}"
    url = create_or_update_issue(title, "\n".join(lines))
    append_seen([{k: v for k, v in row.items() if k != "sources"} for row in accepted])
    print(f"Created/updated: {url}")
    print(f"Candidates: {len(accepted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
