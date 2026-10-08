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
    if (not isinstance(state.get("date"), str) or len(terms) > 10 or len(set(terms)) != len(terms)
            or any(not isinstance(key, str) or not key or key != news.normalize(key) for key in terms)):
        raise ValueError("Invalid daily pending retry history")
    if state["date"]:
        dt.date.fromisoformat(state["date"])
    if state["date"] != today:
        state = {"date": today, "terms": []}
    return pending, state


def save_queue(directory, pending, state):
    document = {"version": 1, "retry_state": state, "rows": [pending[key] for key in sorted(pending)]}
    data.atomic_write(directory / "pending.json", data.json_bytes(document, pretty=True))


def confirmed_row(row, today):
    result = {**row, "accepted_date": today, "checked_at": today, "usage_status": "confirmed",
              "dictionary_check": {"status": "not_checked"}}
    result.setdefault("collection_method", "category_news")
    for field in ("id", "dictionary", "pending_reason", "reading_estimate"):
        result.pop(field, None)
    return result


def run(now=None, *, mode="collect", directory=None):
    directory = DATA_DIR if directory is None else directory
    now = (now or dt.datetime.now(news.TZ)).astimezone(news.TZ)
    today = now.date().isoformat()
    try:
        rows = data.load_entries(directory)
        if mode == "rebuild":
            summary(["## Release再配布", f"保存済み: {len(rows)}語。ニュース収集は行いません。"])
            return 0
        if mode != "collect":
            raise ValueError("Unknown collection mode")
        adopted = sum(row["accepted_date"] == today for row in rows)
        if adopted > news.DAILY_LIMIT:
            raise ValueError("Saved data exceeds the daily adoption limit")
        pending, retry_state = load_queue(directory, today)
        excluded = {news.normalize(row["word"]) for row in rows}
        for key in list(pending):
            if key in excluded:
                del pending[key]
        accepted = []
        try:
            remaining = news.DAILY_LIMIT - adopted
            if remaining:
                accepted = news.collect_candidates(now, excluded, remaining, news.ReadingResolver(),
                                                   pending=pending, retry_state=retry_state)
                if len(accepted) > remaining:
                    raise ValueError("Collector exceeded the remaining daily limit")
                additions = [confirmed_row(row, today) for row in accepted]
                if additions:
                    data.save_entries(rows + additions, directory)
                    rows += additions
                    for row in additions:
                        pending.pop(news.normalize(row["word"]), None)
        finally:
            # Persist attempts/evidence even after an RSS or reading failure.
            save_queue(directory, pending, retry_state)
        summary([f"## Release候補 {today}", f"追加: {len(accepted)}語 / 累積: {len(rows)}語 / 保留: {len(pending)}語",
                 f"本日の保留再確認: {len(retry_state['terms'])}/10語。辞書照合: 未確認。"])
        return 0
    except Exception as error:
        print(f"Release collection failed: {error}", file=sys.stderr, flush=True)
        summary(["## Release収集: 失敗", f"理由: {error}", "ZIPの配布は行いません。保存済みデータから再実行できます。"])
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("collect", "rebuild"), default="collect")
    return run(mode=parser.parse_args().mode)


if __name__ == "__main__":
    raise SystemExit(main())
