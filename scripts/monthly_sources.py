"""Streaming reference sources and resumable, uncapped dictionary-gap collection."""
from __future__ import annotations

from collections import Counter, OrderedDict
from functools import lru_cache
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import release_candidates as words

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data/sources.json"
ADAPTERS = {"jmdict", "jmnedict", "tsumura", "catalog", "discovery", "category_discovery"}
POLICIES = {"curated_reference", "authoritative", "discovery"}
OUTCOMES = ("scanned", "already_registered", "accepted", "already_accepted", "pending", "failed", "rejected", "unfinished")
ATTRIBUTION = """Japanese IME dictionary gaps — source acknowledgements

Entries identify their sources in source_records and sources in metadata-*.jsonl.
JMdict and JMnedict: Copyright the Electronic Dictionary Research and Development
Group and Jim Breen. Derived entries are distributed under CC BY-SA 4.0.
Changes: noun/name selection, category mapping, kana normalization, deduplication,
and removal of spellings already in converter v1.7.256. No endorsement is implied.
https://www.edrdg.org/jmdict/j_jmdict.html
https://www.edrdg.org/enamdict/enamdict_doc.html
https://www.edrdg.org/edrdg/licence.html
https://creativecommons.org/licenses/by-sa/4.0/

MDN contributors: derived terminology under CC BY-SA 2.5.
https://developer.mozilla.org/en-US/docs/MDN/Writing_guidelines/Attrib_copyright_license
https://creativecommons.org/licenses/by-sa/2.5/

Other source names and URLs are preserved per entry. Only terms and explicit
readings are extracted; explanatory articles and English glosses are not copied.
"""


class BudgetExpired(Exception):
    pass


def load_sources(path=REGISTRY):
    sources = json.loads(path.read_text(encoding="utf-8"))
    seen = set()
    categories = {item["id"] for item in words.CATEGORIES}
    for source in sources:
        parsed = urllib.parse.urlsplit(source["url"])
        if (not re.fullmatch(r"[a-z_]+", source["id"]) or source["id"] in seen
                or source["adapter"] not in ADAPTERS or source["evidence_policy"] not in POLICIES
                or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or not set(source["categories"]) <= categories
                or not source.get("publisher") or not source.get("attribution")):
            raise ValueError("Invalid source registry")
        if source["adapter"] == "catalog":
            re.compile(source["path_pattern"])
        seen.add(source["id"])
    return sources


def ordered_categories(tags):
    return [item["id"] for item in words.CATEGORIES if item["id"] in tags]


@lru_cache(maxsize=1)
def evidence_registry():
    return {source["id"]: source for source in load_sources()}


def valid_reference_evidence(row):
    """A policy flag alone cannot bypass the discovery evidence requirements."""
    policy = row.get("evidence_type")
    refs = row.get("source_records")
    if policy not in {"curated_reference", "authoritative"} or not isinstance(refs, list) or not refs:
        return False
    registry = evidence_registry()
    for ref in refs:
        source = registry.get(ref.get("source_id"))
        if source is None or source["evidence_policy"] != policy:
            continue
        host = urllib.parse.urlsplit(ref.get("url", "")).hostname
        expected = urllib.parse.urlsplit(source["url"]).hostname
        if (host != expected or not ref.get("record_id") or not re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", ""))
                or row["reading"] not in ref.get("readings", [])):
            continue
        if any(item.get("link") == ref["url"] for item in row.get("reading_sources", [])):
            return True
    return False


def noun_sense(sense):
    return any(re.search(r"(?:^noun|^n(?:-|$)|pronoun|numeric|counter)", node.text or "", re.I)
               for node in sense.findall("pos"))


def lexical_categories(senses):
    text = " ".join(node.text or "" for sense in senses for node in list(sense.findall("field")) + list(sense.findall("misc"))).lower()
    tags = set()
    for identity, pattern in (
        ("technology", r"comput|telecommun|internet"),
        ("software_engineering", r"comput|software"),
        ("medicine", r"medicin|pharmacol|pharmacy"),
        ("science_health", r"medicin|anatom|biolog|botan|chemistr|physic|astronom|mathematic|zoolog|geolog"),
        ("life_food", r"food|cook|cuisine|agricultur|cloth|sport"),
        ("business", r"business|econom|financ|law|politic"),
        ("entertainment", r"music|video game|manga|anime|art,|film"),
        ("slang", r"slang|colloquial|internet"),
    ):
        if re.search(pattern, text):
            tags.add(identity)
    return ordered_categories(tags or {"life_food"})


def xml_records(path, source):
    """Yield spellings without inventing reading/spelling combinations."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)
        expected = "JMdict" if source["adapter"] == "jmdict" else "JMnedict"
        if root.tag != expected:
            raise ValueError(f"Expected {expected} snapshot")
        for event, entry in context:
            if event != "end" or entry.tag != "entry":
                continue
            identity = entry.findtext("ent_seq")
            if not identity:
                raise ValueError("Reference record lacks ent_seq")
            senses, inherited = entry.findall("sense"), []
            for sense in senses:
                if sense.findall("pos"):
                    inherited = [node.text or "" for node in sense.findall("pos")]
                else:
                    for tag in inherited:
                        ET.SubElement(sense, "pos").text = tag
            nouns = [sense for sense in senses if noun_sense(sense)]
            named = source["adapter"] == "jmnedict"
            if not named and not nouns:
                entry.clear()
                root.clear()
                continue
            kind, tags = "common", lexical_categories(nouns)
            if named:
                text = " ".join(node.text or "" for node in entry.findall("trans/name_type")).lower()
                if re.search(r"place|station", text):
                    kind, tags = "place", ["places"]
                elif re.search(r"company|organization", text):
                    kind, tags = "organization", ["people_organizations"]
                elif re.search(r"product", text):
                    kind, tags = "product", ["products"]
                elif re.search(r"work|fiction|character", text):
                    kind, tags = "work", ["entertainment"]
                elif re.search(r"surname|given|person|male|female|full name", text):
                    kind, tags = "person", ["people_organizations"]
                else:
                    kind, tags = "proper", ["people_organizations"]
            for written in entry.findall("k_ele") or [None]:
                word = written.findtext("keb") if written is not None else None
                info = " ".join(node.text or "" for node in written.findall("ke_inf")) if written is not None else ""
                if re.search(r"incorrect|mis.?spell|search.only|^sK$", info, re.I):
                    continue
                readings = []
                for reading in entry.findall("r_ele"):
                    raw = reading.findtext("reb") or ""
                    restrictions = [node.text for node in reading.findall("re_restr")]
                    info = " ".join(node.text or "" for node in reading.findall("re_inf"))
                    if (word and (reading.find("re_nokanji") is not None or (restrictions and word not in restrictions))
                            or re.search(r"incorrect|mis.?spell|search.only|^sk$", info, re.I)):
                        continue
                    if not named and not any(
                        (not sense.findall("stagk") or word in [node.text for node in sense.findall("stagk")])
                        and (not sense.findall("stagr") or raw in [node.text for node in sense.findall("stagr")])
                        for sense in nouns
                    ):
                        continue
                    kana = words.kana_reading(raw)
                    if kana:
                        readings.append(kana)
                    if word is None:
                        yield {"record_id": identity + ":" + raw, "word": raw, "readings": [kana] if kana else [],
                               "categories": tags, "kind": kind, "url": source["url"] + "#" + identity}
                if word and readings:
                    yield {"record_id": identity + ":" + word, "word": word, "readings": sorted(set(readings)),
                           "categories": tags, "kind": kind, "url": source["url"] + "#" + identity}
            entry.clear()
            root.clear()


class Links(HTMLParser):
    def __init__(self, heading_tags=("h1", "dt")):
        super().__init__(convert_charrefs=True)
        self.links, self.headings = [], []
        self.active = self.heading = None
        self.hidden = 0
        self.heading_tags = heading_tags
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "a":
            self.active = [dict(attrs).get("href", ""), ""]
        if tag in self.heading_tags:
            self.heading = [tag, ""]
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if tag == "a" and self.active is not None:
            self.links.append(self.active)
            self.active = None
        if self.heading is not None and tag == self.heading[0]:
            self.headings.append(self.heading[1].strip())
            self.heading = None
    def handle_data(self, text):
        if not self.hidden:
            if self.active is not None:
                self.active[1] += text
            if self.heading is not None:
                self.heading[1] += text


class Network:
    """Bounded shared cache, per-host throttling, and a common deadline."""
    def __init__(self, deadline, cache_dir):
        self.deadline, self.cache_dir = deadline, cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pages, self.last = OrderedDict(), {}
    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExpired()
    def throttle(self, url):
        self.check()
        host = urllib.parse.urlsplit(url).hostname
        wait = max(0, self.last.get(host, 0) + 1 - time.monotonic())
        if wait:
            time.sleep(wait)
        self.check()
        self.last[host] = time.monotonic()
    def page(self, url):
        self.check()
        if url in self.pages:
            self.pages.move_to_end(url)
            return self.pages[url]
        self.throttle(url)
        result = words.public_reading_document(url)
        self.pages[url] = result
        if len(self.pages) > 64:
            self.pages.popitem(last=False)
        return result
    def request(self, url, **options):
        self.throttle(url)
        result = words.request(url, **options)
        self.check()
        return result
    def publisher(self, url):
        return words.publisher_url(url, fetch=self.page, api_request=self.request)
    def snapshot(self, source):
        self.throttle(source["url"])
        target = self.cache_dir / (source["id"] + ".xml.gz")
        temporary = target.with_suffix(".download")
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(source["url"], headers={"User-Agent": "KazumaProject-New-word/3.0"})
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    self.check()
                    digest.update(chunk)
                    output.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target, digest.hexdigest()


def catalog_records(source, state, network):
    queue = state.setdefault("urls", [source["url"]])
    visited = set(state.setdefault("visited", []))
    host = urllib.parse.urlsplit(source["url"]).hostname
    while queue:
        url = queue[0]
        markup = network.page(url)
        digest = hashlib.sha256(markup.encode("utf-8")).hexdigest()
        state.setdefault("page_revisions", {})[url] = digest
        parser = Links(source.get("heading_tags", ["h1", "dt"]))
        parser.feed(markup)
        for href, _ in parser.links:
            parsed = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
            target = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                             urllib.parse.quote(parsed.path, safe="/%:@"),
                                             urllib.parse.quote(parsed.query, safe="=&%+/:@"), ""))
            if (parsed.scheme == "https" and parsed.hostname == host and re.search(source["path_pattern"], parsed.path)
                    and target not in visited and target not in queue):
                queue.append(target)
        for heading in parser.headings:
            for word in heading_terms(heading):
                readings = sorted(words.extract_readings(word, markup))
                if not readings and (kana := words.kana_reading(word)):
                    readings = [kana]
                yield {"record_id": url + "#" + words.normalize(word), "word": word, "readings": readings,
                       "categories": source["categories"], "kind": "common", "url": url, "sha256": digest}
        visited.add(url)
        state["visited"] = sorted(visited)
        queue.pop(0)


def heading_terms(heading):
    match = re.fullmatch(r"(.+?)\s*[（(]([^（）()]+)[）)]", heading)
    if match:
        first, second = match[1].strip(), match[2].strip()
        if words.kana_reading(second) or re.search(r"[一-龠ぁ-んァ-ヶ]", first):
            heading = first
        elif re.search(r"[一-龠ぁ-んァ-ヶ]", second):
            heading = second
    terms = re.split(r"[／]|(?<=[一-龠ぁ-んァ-ヶ])/(?=[一-龠ぁ-んァ-ヶ])", heading)
    return [word for term in terms if (word := words.clean_candidate(term))]


def tsumura_records(source, state, network):
    markup = network.page(source["url"])
    digest = hashlib.sha256(markup.encode("utf-8")).hexdigest()
    products = json.loads(markup).get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("Tsumura product list is unavailable or changed format")
    if state.get("sha256") != digest:
        state.update(cursor=0, counts={})
    state["sha256"] = digest
    skip, position = state.get("cursor", 0), 0
    for product in products:
        network.check()
        word, reading = product["name"], words.kana_reading(product["nameKana"])
        if word.startswith("ツムラ") and reading and not reading.startswith("つむら"):
            reading = None  # A formula-only reading cannot attest the branded name.
        record = {"record_id": product["product_Id"], "word": word, "readings": [reading] if reading else [],
                  "categories": ordered_categories(source["categories"] + ["products"]), "kind": "product",
                  "url": source["url"] + "#" + product["product_Id"], "sha256": digest}
        # Both the manufacturer prefix and its reading are explicit in this list.
        if position >= skip:
            yield record
        position += 1
        if (word.startswith("ツムラ") and reading and reading.startswith("つむら")
                and not word.removeprefix("ツムラ").startswith("の")):
            if position >= skip:
                yield {**record, "record_id": record["record_id"] + ":formula", "word": word.removeprefix("ツムラ"),
                       "readings": [reading.removeprefix("つむら")], "kind": "common", "categories": source["categories"]}
            position += 1


def discovery_records(source, state, network):
    queries = ([{"q": source["query"], "categories": source["categories"], "kind": "common"}]
               if source["adapter"] == "discovery" else
               [{**query, "categories": [category["id"]]} for category in words.CATEGORIES for query in category["queries"]])
    for index in range(state.setdefault("query_cursor", 0), len(queries)):
        network.throttle("https://news.google.com/rss/search")
        query = queries[index]
        items = words.google_news_rss(query["q"])
        state.setdefault("query_revisions", {})[query["q"]] = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for item in items:
            for word in sorted(words.extract_candidates(item["title"])):
                yield {"record_id": item["link"] + "#" + words.normalize(word), "word": word, "readings": [],
                       "categories": query["categories"], "kind": words.candidate_pipeline.kind_for(item["title"], word, query["kind"]),
                       "url": item["link"], "sources": [item]}
        state["query_cursor"] = index + 1


def load_state(directory):
    path = directory / "collection-state.json"
    if not path.exists():
        return {"version": 1, "sources": {}, "category_counts": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("sources"), dict):
        raise ValueError("Invalid collection checkpoint; refusing to reset progress")
    for source in state["sources"].values():
        if (not isinstance(source, dict) or not isinstance(source.get("cursor", 0), int)
                or source.get("cursor", 0) < 0 or source.get("status") not in {"running", "complete", "failed", "unfinished"}):
            raise ValueError("Invalid source checkpoint")
    return state


def merge_unique(first, second, field):
    result = {item[field]: item for item in first}
    for item in second:
        result.setdefault(item[field], item)
    return list(result.values())


def verify_usage(word, sources, resolver, context):
    return words.candidate_pipeline.verified_usage(word, sources, resolver, context)


def official_name(word, sources, resolver, context):
    return words.candidate_pipeline.official_name(word, sources, resolver, context)


def collect(rows, pending, dictionary, now, directory, *, budget_seconds=4 * 60 * 60,
            sources=None, network=None, checkpoint=None):
    """Drain all snapshots subject only to the resumable runtime budget."""
    import release_archive as archive
    sources = load_sources() if sources is None else sources
    state = load_state(directory)
    network = network or Network(time.monotonic() + budget_seconds, ROOT / ".cache/sources")
    accepted = {words.normalize(row["word"]): row for row in rows}
    initial_keys = set(accepted)
    day, month = now.date().isoformat(), now.strftime("%Y-%m")
    adapter_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    state["budget_exhausted"] = False
    resolver = words.ReadingResolver(page_limit=None)
    resolver.documents = OrderedDict()
    context = SimpleNamespace(**vars(words))
    context.public_reading_document = network.page
    context.publisher_url = network.publisher if hasattr(network, "publisher") else words.publisher_url
    category_counts = {category["id"]: Counter({**dict.fromkeys(OUTCOMES, 0), **state.get("category_counts", {}).get(category["id"], {})}) for category in words.CATEGORIES}

    def save():
        for category in words.CATEGORIES:
            category_counts[category["id"]]["unfinished"] = sum(
                state["sources"][source["id"]]["status"] != "complete"
                for source in sources if category["id"] in source["categories"] or source["adapter"] == "category_discovery")
        for source in sources:
            source_state = state["sources"][source["id"]]
            source_state["counts"] = {**dict.fromkeys(OUTCOMES, 0), **source_state.get("counts", {})}
            source_state["counts"]["unfinished"] = int(source_state["status"] != "complete")
        state["category_counts"] = {key: dict(value) for key, value in category_counts.items()}
        state["complete"] = (not state["budget_exhausted"] and
                             all(state["sources"].get(source["id"], {}).get("status") == "complete" for source in sources))
        state["checked_at"] = day
        archive.save_entries(list(accepted.values()), directory, allow_empty=True)
        archive.atomic_write(directory / "pending.json", archive.json_bytes({"version": 2, "retry_state": {"date": "", "terms": []},
                                 "legacy_retry_history": state.get("legacy_retry_history", {}),
                                 "rows": [{**pending[key], "metadata_version": 2,
                                           "evidence_type": pending[key].get("evidence_type", "legacy_discovery")}
                                          for key in sorted(pending)]}, pretty=True))
        # Cursor last: a crash can replay records, but cannot skip unsaved rows.
        archive.atomic_write(directory / "collection-state.json", archive.json_bytes(state, pretty=True))
        if checkpoint:
            checkpoint(state)

    def account(source_state, tags, outcome):
        counts = source_state.setdefault("counts", {})
        counts[outcome] = counts.get(outcome, 0) + 1
        for tag in tags:
            category_counts[tag][outcome] += 1

    def consider(record, source, source_state):
        word = words.clean_candidate(record["word"])
        tags = ordered_categories(record["categories"])
        account(source_state, tags, "scanned")
        if not word or not tags:
            account(source_state, tags, "rejected")
            return
        key = words.normalize(word)
        if dictionary.contains(word):
            if key in pending:
                pending[key]["dictionary_check"] = dictionary.check(word, day)
                pending[key]["pending_reason"] = "既存辞書に収録済み"
            account(source_state, tags, "already_registered")
            return
        previous = accepted.get(key) or pending.get(key, {})
        tags = ordered_categories(set(tags) | set(previous.get("categories", [])))
        evidence = record.get("sources") or [{"source": source["publisher"], "link": record["url"], "title": word}]
        all_sources = merge_unique(previous.get("sources", []), evidence, "link")
        row = {**previous, "metadata_version": 2, "date": previous.get("date", day), "word": previous.get("word", word),
               "normalized": key, "categories": tags, "category": tags[0], "kind": record["kind"],
               "sources": all_sources, "checked_at": day, "last_attempt": day, "collection_method": "monthly_sources",
               "dictionary_check": dictionary.check(word, day)}
        policy, readings = source["evidence_policy"], list(record.get("readings", []))
        if policy == "discovery" and previous.get("evidence_type") in {"curated_reference", "authoritative"} and previous.get("reading_status") == "conflict":
            pending[key] = {**previous, "categories": tags, "category": tags[0], "sources": all_sources}
            account(source_state, tags, "pending")
            return
        if key in accepted and policy == "discovery" and (key in initial_keys or previous.get("evidence_type") in {"curated_reference", "authoritative"}):
            accepted[key] = {**previous, "categories": tags, "category": tags[0], "sources": all_sources}
            account(source_state, tags, "already_accepted")
            return
        if key in accepted and policy in {"curated_reference", "authoritative"} and not readings and source["adapter"] == "catalog":
            # A glossary mention adds classification, not an unsupported new reading.
            accepted[key] = {**previous, "categories": tags, "category": tags[0], "sources": all_sources}
            account(source_state, tags, "already_accepted")
            return
        if policy == "authoritative" and not readings:
            direct = context.publisher_url(record["url"])
            allowed = source.get("official_hosts", [urllib.parse.urlsplit(source["url"]).hostname])
            if urllib.parse.urlsplit(direct).hostname not in allowed:
                policy = "discovery"
            else:
                markup = network.page(direct)
                readings = sorted(words.extract_readings(word, markup))
                record = {**record, "url": direct, "sha256": hashlib.sha256(markup.encode()).hexdigest()}
                evidence = [{"source": source["publisher"], "link": direct, "title": word}]
                row["sources"] = merge_unique(row["sources"], evidence, "link")
        if policy in {"curated_reference", "authoritative"}:
            ref = {"source_id": source["id"], "record_id": record["record_id"], "url": record["url"],
                   "sha256": record.get("sha256", source_state.get("sha256", "")), "readings": readings}
            # Replace a changed record, retaining all other source attestations.
            refs = {(item["source_id"], item["record_id"]): item for item in previous.get("source_records", [])}
            refs[(ref["source_id"], ref["record_id"])] = ref
            row.update(source_records=list(refs.values()), evidence_type=policy)
            attested = sorted({reading for item in refs.values() for reading in item["readings"]})
            reviewed = resolver.reviewed.get(key, {})
            context_text = words.normalize(" ".join(item.get("title", "") for item in row["sources"]))
            if (len(attested) > 1 and reviewed.get("reading") in attested
                    and (not reviewed.get("context") or any(words.normalize(term) in context_text for term in reviewed["context"]))):
                selected = reviewed["reading"]
                supporting = [item for item in refs.values() if selected in item["readings"]]
                proof = supporting[0]
                registry = evidence_registry()
                row.update(reading=selected, reading_status="confirmed", reading_method="reviewed", reading_note="",
                           evidence_type=registry[proof["source_id"]]["evidence_policy"], usage_status="confirmed",
                           reading_sources=merge_unique(reviewed["sources"], [{"source": registry[item["source_id"]]["publisher"], "link": item["url"]} for item in supporting], "link"),
                           official_name_sources=[] if row["kind"] == "common" else evidence)
            elif len(attested) == 1:
                row.update(reading=attested[0], reading_status="confirmed", reading_method="source",
                           reading_note="",
                           reading_sources=[{"source": source["publisher"], "link": record["url"]}], usage_status="confirmed",
                           official_name_sources=[] if row["kind"] == "common" else evidence)
                # A source that did not supply the reading cannot attest it.
                if not readings:
                    row.update(reading="要確認", reading_status="unconfirmed", pending_reason="読みの明記が未確認")
            else:
                row.update(reading="要確認", reading_status="conflict" if attested else "unconfirmed",
                           pending_reason="読みの競合" if attested else "読みの明記が未確認")
        else:
            row["evidence_type"] = "discovery"
            usages = verify_usage(word, all_sources, resolver, context)
            if len(usages) < 2:
                row.update(reading="要確認", reading_status="unconfirmed", pending_reason="独立した日本語使用例が2件未満")
            else:
                row["sources"] = usages
                row.update(resolver.resolve(word, usages))
                row["official_name_sources"] = official_name(word, usages, resolver, context) if row["kind"] != "common" else []
                row["usage_status"] = "confirmed"
                if row["kind"] != "common" and not row["official_name_sources"]:
                    row["pending_reason"] = "名称の公式根拠が未確認"
                else:
                    row.pop("pending_reason", None)
        if row.get("reading_status") == "confirmed" and words.kana_reading(row.get("reading", "")) == row.get("reading"):
            if key == words.normalize(row["reading"]):
                account(source_state, tags, "rejected")
                return
            if policy in {"curated_reference", "authoritative"}:
                row.pop("pending_reason", None)
            if not row.get("pending_reason"):
                row.update(pos=words.pos_label(word, row["kind"]), accepted_date=previous.get("accepted_date", day), usage_status="confirmed")
                for field in ("id", "dictionary", "reading_estimate"):
                    row.pop(field, None)
                archive.validate_row(row, words.CATEGORIES)
                accepted[key] = row
                pending.pop(key, None)
                account(source_state, tags, "accepted" if key not in initial_keys else "already_accepted")
                return
        accepted.pop(key, None)
        row.setdefault("pending_reason", row.get("reading_note") or "読み未確認")
        pending[key] = row
        account(source_state, tags, "pending")

    generators, retry_jobs = [], []
    queue_path = directory / "pending.json"
    if queue_path.exists():
        old_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        state.setdefault("legacy_retry_history", old_queue.get("legacy_retry_history", old_queue.get("retry_state", {})))
    for source in sources:
        state["sources"].setdefault(source["id"], {"status": "unfinished", "cursor": 0})
    try:
        for source in sources:
            source_state = state["sources"][source["id"]]
            revision = hashlib.sha256(json.dumps([adapter_digest, source, words.CATEGORIES],
                                      ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if (source_state.get("adapter_sha256") != revision or
                    source_state.get("month") != month and source_state.get("status") == "complete"):
                source_state.clear()
                source_state.update(status="unfinished", cursor=0)
            source_state["adapter_sha256"] = revision
            source_state["month"] = month
            source_state.pop("error", None)
            try:
                if source["adapter"] in {"jmdict", "jmnedict"}:
                    path, digest = network.snapshot(source)
                    if source_state.get("sha256") != digest:
                        source_state.update(cursor=0, counts={})
                    source_state["sha256"] = digest
                    def resumed(iterator=xml_records(path, source), count=source_state["cursor"]):
                        for index, record in enumerate(iterator):
                            network.check()
                            if index >= count:
                                yield record
                    records = resumed()
                elif source["adapter"] == "tsumura":
                    records = tsumura_records(source, source_state, network)
                elif source["adapter"] == "catalog":
                    records = catalog_records(source, source_state, network)
                else:
                    records = discovery_records(source, source_state, network)
                source_state["status"] = "running"
                generators.append((source, source_state, iter(records)))
            except BudgetExpired:
                raise
            except Exception as error:
                source_state.update(status="failed", error=f"{type(error).__name__}: {error}")
        pending_keys = sorted(list(pending))
        def retries(source):
            for key in pending_keys:
                row = pending.get(key)
                if row is None:
                    continue
                refs = [ref for ref in row.get("source_records", []) if ref["source_id"] == source["id"]]
                for ref in refs:
                    yield {**ref, "word": row["word"], "categories": row["categories"], "kind": row.get("kind", "common")}
                if not refs and row.get("source_id") == source["id"]:
                    yield {**row, "record_id": row.get("record_id", key)}
                elif not refs and source["adapter"] == "category_discovery" and row.get("evidence_type", "discovery") in {"discovery", "legacy_discovery"} and row.get("sources"):
                    yield {"record_id": key, "word": row["word"], "categories": row["categories"],
                           "kind": row.get("kind", "common"), "url": row["sources"][0]["link"], "sources": row["sources"]}
        for source in reversed(sources):
            retry_state = {"counts": {}, "status": "running"}
            state["sources"][source["id"]]["retry_counts"] = retry_state["counts"]
            retry_jobs.append((state["sources"][source["id"]], retry_state))
            generators.insert(0, (source, retry_state, iter(retries(source))))
        processed, last_save = 0, time.monotonic()
        while generators:
            for source, source_state, iterator in list(generators):
                network.check()
                try:
                    record = next(iterator)
                except StopIteration:
                    source_state["status"] = "complete"
                    generators.remove((source, source_state, iterator))
                    continue
                except BudgetExpired:
                    raise
                except Exception as error:
                    source_state.update(status="failed", error=f"{type(error).__name__}: {error}")
                    generators.remove((source, source_state, iterator))
                    continue
                try:
                    consider(record, source, source_state)
                except BudgetExpired:
                    raise
                except Exception as error:
                    key = words.normalize(record["word"])
                    if key not in accepted:
                        pending[key] = {**pending.get(key, {}), **record, "sources": record.get("sources") or
                                        [{"source": source["publisher"], "link": record["url"], "title": record["word"]}],
                                        "source_id": source["id"], "pending_reason": f"{type(error).__name__}: {error}"}
                    account(source_state, record["categories"], "failed")
                source_state["cursor"] = source_state.get("cursor", 0) + 1
                processed += 1
                while len(resolver.documents) > 64:
                    resolver.documents.popitem(last=False)
                resolver.results.clear()
                if time.monotonic() - last_save > 300:
                    save()
                    last_save = time.monotonic()
    except BudgetExpired:
        state["budget_exhausted"] = True
    finally:
        for source_state, retry_state in retry_jobs:
            source_state["retry_status"] = "unfinished" if retry_state["status"] == "running" else retry_state["status"]
        for source_state in state["sources"].values():
            if source_state.get("status") == "running":
                source_state["status"] = "unfinished"
        save()
    return list(accepted.values()), state
