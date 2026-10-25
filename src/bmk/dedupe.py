"""Duplicate proposals (PLAN05 §7, tiers 3–4), clusters for review, bulk apply and recanonicalise.

Tiers 1–2 merge automatically at ingest and after resolving (service.Library). Tiers 3–4 only propose:
a scan rebuilds `duplicate_candidates`, the user reviews clusters and merges or dismisses them; dismissed
pairs go to `not_duplicates` and are never proposed again.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from importlib import resources
from itertools import combinations
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import db as dbmod
from . import urls
from .timeutil import now_utc


@dataclass(frozen=True)
class Rules:
    confidence: dict[str, float]
    max_simhash_distance: int
    min_text_length: int
    max_title_group: int
    min_title_length: int
    generic_titles: frozenset[str]


@cache
def rules() -> Rules:
    data = yaml.safe_load(resources.files("bmk.rules").joinpath("dedupe.yaml").read_text(encoding="utf-8")) or {}
    enrich = yaml.safe_load(resources.files("bmk.rules").joinpath("enrich.yaml").read_text(encoding="utf-8")) or {}
    generic = {t.lower() for t in data.get("generic_titles", []) + enrich.get("generic_titles", [])}
    return Rules(
        confidence=dict(data["confidence"]),
        max_simhash_distance=int(data.get("max_simhash_distance", 3)),
        min_text_length=int(data.get("min_text_length", 500)),
        max_title_group=int(data.get("max_title_group", 3)),
        min_title_length=int(data.get("min_title_length", 8)),
        generic_titles=frozenset(generic),
    )


def norm_title(title: str | None, domain: str = "") -> str | None:
    """Comparable title, or None when it says nothing about the page (generic, too short, the site's name)."""
    if not title:
        return None
    t = unicodedata.normalize("NFKC", title).casefold()
    t = re.sub(r"[\s​-‏⁦-⁩﻿]+", " ", t).strip(" -|·—–:")
    r = rules()
    if len(t) < r.min_title_length or t in r.generic_titles:
        return None
    site = domain.casefold()
    if t in (site, "www." + site, site.split(".")[0]):
        return None
    return t


def _distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _query_differs_only(a: str, b: str) -> bool:
    """Same host and path but a different query: the query is significant (else url_loose would match),
    so these are different pages, e.g. two YouTube videos."""
    pa, pb = urlsplit(a), urlsplit(b)
    return pa.netloc == pb.netloc and pa.path.rstrip("/") == pb.path.rstrip("/") and pa.query != pb.query


def find_candidates(rows: list[dict[str, Any]], dismissed: set[tuple[int, int]]) -> dict[tuple[int, int], tuple[int, float, str]]:
    """Pairs (a < b) → (tier, confidence, rationale), the strongest signal per pair."""
    r = rules()
    found: dict[tuple[int, int], tuple[int, float, str]] = {}

    def propose(x, y, tier, key, rationale):
        pair = (min(x["id"], y["id"]), max(x["id"], y["id"]))
        if pair in dismissed:
            return
        conf = r.confidence[key]
        if pair not in found or found[pair][1] < conf:
            found[pair] = (tier, conf, rationale)

    by_loose: dict[str, list] = {}
    by_domain: dict[str, list] = {}
    for row in rows:
        by_loose.setdefault(row["url_loose"], []).append(row)
        by_domain.setdefault(row["domain"] or "", []).append(row)

    for group in by_loose.values():
        for x, y in combinations(group, 2):
            propose(x, y, 3, "url_variant", "Same address apart from http/https, www, a trailing slash or tracking parameters")

    for domain, group in by_domain.items():
        if len(group) < 2:
            continue
        by_canonical = {row["url_canonical"]: row for row in group}
        titles: dict[str, list] = {}
        for row in group:
            hint = row["canonical_hint"]
            if hint:
                try:
                    hint = urls.canonicalize(hint)
                except ValueError:
                    hint = None
            if hint and (other := by_canonical.get(hint)) and other["id"] != row["id"]:
                propose(row, other, 4, "canonical_hint", "The page says its canonical address is the other bookmark")
            row["_hint"] = hint
            row["_title"] = norm_title(row["title"], domain)
            if row["_title"]:
                titles.setdefault(row["_title"], []).append(row)

        for x, y in combinations(group, 2):
            if _query_differs_only(x["url_canonical"], y["url_canonical"]):
                continue
            if x["_hint"] and x["_hint"] == y["_hint"]:
                propose(x, y, 4, "canonical_hint", "Both pages give the same canonical address")
            if (
                x["content_simhash"]
                and y["content_simhash"]
                and min(x["text_len"] or 0, y["text_len"] or 0) >= r.min_text_length
                and _distance(x["content_simhash"], y["content_simhash"]) <= r.max_simhash_distance
                # Different real titles with the same text is a shared cookie wall or template, not one page.
                and (x["_title"] == y["_title"] or not x["_title"] or not y["_title"])
            ):
                d = _distance(x["content_simhash"], y["content_simhash"])
                propose(x, y, 4, "similar_text", "Page text is identical" if d == 0 else f"Page text is nearly identical ({d} of 64 bits differ)")

        for group_t in titles.values():
            if 2 <= len(group_t) <= r.max_title_group:
                for x, y in combinations(group_t, 2):
                    if not _query_differs_only(x["url_canonical"], y["url_canonical"]):
                        propose(x, y, 4, "same_title", "Same site and same title (check the addresses)")
    return found


def scan(lib) -> dict[str, int]:
    """Rebuild duplicate_candidates over live bookmarks (not deleted, not merged). Cheap: blocked by
    url_loose and domain, so there are no all-pairs comparisons across the library."""
    rows = [
        dict(r)
        for r in lib.conn.execute(
            "SELECT id, url_canonical, url_loose, domain, title, canonical_hint, content_simhash, length(content_text) AS text_len"
            " FROM bookmarks WHERE deleted_at IS NULL AND merged_into_id IS NULL"
        )
    ]
    dismissed = {(r[0], r[1]) for r in lib.conn.execute("SELECT a_id, b_id FROM not_duplicates")}
    found = find_candidates(rows, dismissed)
    now = now_utc()
    with dbmod.transaction(lib.conn):
        lib.conn.execute("DELETE FROM duplicate_candidates")
        lib.conn.executemany(
            "INSERT INTO duplicate_candidates (a_id, b_id, tier, confidence, rationale, found_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(a, b, t, c, why, now) for (a, b), (t, c, why) in found.items()],
        )
    tiers = [t for t, _, _ in found.values()]
    return {"pairs": len(found), "tier3": tiers.count(3), "tier4": tiers.count(4), "clusters": len(_components(found))}


def _components(pairs) -> list[set[int]]:
    parent: dict[int, int] = {}

    def root(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[root(a)] = root(b)
    groups: dict[int, set[int]] = {}
    for x in list(parent):
        groups.setdefault(root(x), set()).add(x)
    return list(groups.values())


def cluster_count(lib) -> int:
    return len(_components([(r[0], r[1]) for r in lib.conn.execute("SELECT a_id, b_id FROM duplicate_candidates")]))


def clusters(lib) -> list[dict[str, Any]]:
    """Proposed groups for review, most confident first, each with its pairs and a suggested survivor."""
    pairs = {(r["a_id"], r["b_id"]): dict(r) for r in lib.conn.execute("SELECT * FROM duplicate_candidates")}
    out = []
    for ids in _components(pairs):
        members = [p for k, p in pairs.items() if k[0] in ids]
        rows = [lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (i,)).fetchone() for i in sorted(ids)]
        items = []
        for row in rows:
            item = {k: row[k] for k in ("id", "title", "url_canonical", "domain", "saved_at", "saved_at_source", "status",
                                         "starred", "remarks", "description", "image_url", "fetch_status", "deleted_at")}  # fmt: skip
            item["url"] = row["url_resolved"] or row["url_original"]
            item["locked_fields"] = json.loads(row["locked_fields"])
            item["sources"] = [
                dict(s) for s in lib.conn.execute("SELECT source_type, source_ref, locator, first_seen_at FROM sources WHERE bookmark_id=?", (row["id"],))
            ]
            item["tags"] = [
                t[0] for t in lib.conn.execute("SELECT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id=t.id WHERE bt.bookmark_id=?", (row["id"],))
            ]
            items.append(item)
        out.append(
            {
                "ids": sorted(ids),
                "tier": min(p["tier"] for p in members),
                "confidence": max(p["confidence"] for p in members),
                "min_confidence": min(p["confidence"] for p in members),
                "pairs": sorted(members, key=lambda p: -p["confidence"]),
                "suggested": lib.pick_survivor(rows)["id"],
                "bookmarks": items,
            }
        )
    out.sort(key=lambda c: (-c["confidence"], c["ids"][0]))
    return out


def apply(lib, min_confidence: float = 0.95, tier: int | None = None) -> list[dict[str, Any]]:
    """Merge every cluster whose pairs are all at least `min_confidence` (and in `tier`), into the suggested survivor."""
    done = []
    for cluster in clusters(lib):
        if cluster["min_confidence"] < min_confidence or (tier is not None and any(p["tier"] != tier for p in cluster["pairs"])):
            continue
        others = [i for i in cluster["ids"] if i != cluster["suggested"]]
        reason = cluster["pairs"][0]["rationale"]
        done.append(lib.merge_bookmarks(cluster["suggested"], others, reason=reason, tier=cluster["tier"]))
    return done


def recanonicalise(lib, dry_run: bool = False) -> dict[str, Any]:
    """Recompute canonical and loose keys with the current rules (after editing rules/canonicalize.yaml).
    A bookmark whose new canonical URL belongs to another bookmark is merged into it (tier 1); every source
    URL is added as an alias so it keeps matching. `dry_run` reports without changing anything."""
    now = now_utc()
    changes: list[dict[str, Any]] = []
    counts = {"canonical": 0, "loose": 0, "merged": 0, "aliases": 0}
    claimed: dict[str, int] = {}  # new canonical → bookmark, so a dry run sees its own planned changes
    with dbmod.transaction(lib.conn):
        for row in lib.conn.execute("SELECT * FROM bookmarks WHERE merged_into_id IS NULL ORDER BY id").fetchall():
            current = lib.conn.execute("SELECT * FROM bookmarks WHERE id=?", (row["id"],)).fetchone()
            if current["merged_into_id"] is not None:  # merged earlier in this run
                continue
            try:
                canonical = urls.canonicalize(current["url_resolved"] or current["url_original"])
            except ValueError:
                continue
            loose = urls.loose_key(canonical)
            if canonical != current["url_canonical"]:
                owner, _ = lib._find_bookmark(canonical)
                owner_id = claimed.get(canonical) or (owner["id"] if owner is not None else None)
                change = {"id": current["id"], "from": current["url_canonical"], "to": canonical}
                if owner_id is not None and owner_id != current["id"]:
                    change["merge_with"] = owner_id
                    counts["merged"] += 1
                    if not dry_run:
                        lib.merge(current["id"], owner_id, reason="same address under the current rules", now=now, tier=1)
                else:
                    counts["canonical"] += 1
                    claimed[canonical] = current["id"]
                    if not dry_run:
                        lib.conn.execute("UPDATE url_aliases SET kind='original' WHERE url_canonical=? AND bookmark_id=?", (current["url_canonical"], current["id"]))
                        lib.conn.execute("INSERT OR REPLACE INTO url_aliases (url_canonical, bookmark_id, kind) VALUES (?, ?, 'canonical')", (canonical, current["id"]))
                        lib._update_columns(current["id"], {"url_canonical": canonical, "url_loose": loose, "domain": urls.domain_of(canonical), "updated_at": now})
                        lib._event(current["id"], "recanonicalise", {"from": current["url_canonical"], "to": canonical}, now)
                changes.append(change)
            elif loose != current["url_loose"]:
                counts["loose"] += 1
                if not dry_run:
                    lib._update_columns(current["id"], {"url_loose": loose})
        for src in lib.conn.execute(
            "SELECT s.raw_url, COALESCE(b.merged_into_id, b.id) AS bookmark_id FROM sources s JOIN bookmarks b ON b.id = s.bookmark_id"
        ).fetchall():
            try:
                key = urls.canonicalize(src["raw_url"])
            except ValueError:
                continue
            if lib.conn.execute("SELECT 1 FROM url_aliases WHERE url_canonical=?", (key,)).fetchone() is None:
                counts["aliases"] += 1
                if not dry_run:
                    lib.conn.execute("INSERT INTO url_aliases (url_canonical, bookmark_id, kind) VALUES (?, ?, 'original')", (key, src["bookmark_id"]))
    return {**counts, "changes": changes}
