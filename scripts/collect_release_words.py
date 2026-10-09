"""Collect confirmed candidates into Git data; never call the Issues API."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys

import release_archive as data
import release_candidates as news
import dictionary_assets as dictionaries
import monthly_sources

DATA_DIR = data.DATA_DIR


def summary(lines):
    print("\n".join(lines), flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def load_queue(directory, today):
    path = directory / "pending.json"
    if not path.exists():
        raise ValueError("Release pending queue is missing; refusing to reset it")
    document = json.loads(path.read_text(encoding="utf-8"))
    pending = news.candidate_pipeline.load_pending(path, news.normalize)
    if len(pending) != len(document["rows"]):
        raise ValueError("Duplicate pending candidates")
    state = document.get("retry_state")
    if not isinstance(state, dict) or not isinstance(state.get("terms"), list):
        raise ValueError("Missing daily pending retry history")
    terms = state["terms"]
    if (not isinstance(state.get("date"), str) or len(set(terms)) != len(terms)
            or any(not isinstance(key, str) or not key or key != news.normalize(key) for key in terms)):
        raise ValueError("Invalid daily pending retry history")
    if state["date"]:
        dt.date.fromisoformat(state["date"])
    return pending, state


def save_queue(directory, pending, state):
    document = {"version": 1, "retry_state": state, "rows": [pending[key] for key in sorted(pending)]}
    data.atomic_write(directory / "pending.json", data.json_bytes(document, pretty=True))


def confirmed_row(row, today):
    result = {**row, "accepted_date": today, "checked_at": today, "usage_status": "confirmed",
              "dictionary_check": row.get("dictionary_check", {"status": "not_checked"})}
    result.setdefault("collection_method", "category_news")
    for field in ("id", "dictionary", "pending_reason", "reading_estimate"):
        result.pop(field, None)
    return result


def verify_saved(rows, dictionary, today):
    result = []
    for row in rows:
        check = dictionary.check(row["word"], today)
        previous = row.get("dictionary_check", {})
        if {**previous, "checked_at": today} == check:
            check = previous
        result.append({**row, "dictionary_check": check, "metadata_version": 2,
                       "evidence_type": row.get("evidence_type", "legacy_discovery")})
    return result


def run(now=None, *, mode="collect", directory=None, budget_seconds=4 * 60 * 60):
    directory = DATA_DIR if directory is None else directory
    now = (now or dt.datetime.now(news.TZ)).astimezone(news.TZ)
    try:
        rows = data.load_entries(directory)
        with dictionaries.DictionaryIndex.from_environment() as dictionary:
            return collect_verified(rows, dictionary, now, mode, directory, budget_seconds=budget_seconds)
    except Exception as error:
        print(f"Release collection failed: {error}", file=sys.stderr, flush=True)
        summary(["## Release収集: 失敗", f"理由: {error}", "ZIPの配布は行いません。保存済みデータから再実行できます。"])
        return 1


def collect_verified(rows, dictionary, now, mode, directory, *, budget_seconds=4 * 60 * 60):
    if mode not in {"collect", "rebuild"}:
        raise ValueError("Unknown collection mode")
    today = now.date().isoformat()
    verified = verify_saved(rows, dictionary, today)
    if verified != rows:
        data.save_entries(verified, directory)
    rows = verified
    missing = sum(row["dictionary_check"]["status"] == "missing" for row in rows)
    if mode == "rebuild":
        summary(["## Release再配布", f"配布対象: {missing}語 / 収録済みのため除外: {len(rows) - missing}語。",
                 "辞書照合: v1.7.256・全13パック確認済み。ニュース収集は行いません。"])
        return 0
    pending, _ = load_queue(directory, today)
    excluded = {news.normalize(row["word"]) for row in rows}
    for key in list(pending):
        if key in excluded:
            del pending[key]
    updated, state = monthly_sources.collect(rows, pending, dictionary, now, directory, budget_seconds=budget_seconds)
    missing = sum(row['dictionary_check']['status'] == 'missing' for row in updated)
    summary([f"## 月次辞書候補 {today}", f"配布対象: {missing} / 保存語: {len(updated)} / 保留: {len(pending)} / 語数上限なし",
             "全ソースの処理済み（保留・取得失敗は下記に記録）。" if state["complete"] else "未完了のソースがあります。次回の月次実行または手動collectで再開します。",
             "辞書照合: v1.7.256・全13パック確認済み。",
             "### ソース別", *[f"- {key}: {value['status']} / {value.get('counts', {})}" +
                               f" / 再確認: {value.get('retry_counts', {})} ({value.get('retry_status', '')})" +
                               (f" / {value['error']}" if value.get('error') else "") for key, value in state["sources"].items()],
             "### カテゴリ別", *[f"- {key}: {value}" for key, value in state["category_counts"].items()]])
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("collect", "rebuild"), default="collect")
    parser.add_argument("--budget-seconds", type=int, default=4 * 60 * 60)
    args = parser.parse_args()
    if args.budget_seconds < 0 or args.budget_seconds > 4 * 60 * 60:
        parser.error("budget-seconds must be between 0 and 14400")
    return run(mode=args.mode, budget_seconds=args.budget_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
