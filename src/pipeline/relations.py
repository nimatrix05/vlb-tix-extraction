"""Links between books: VLB-TIX related products, series, shared preview catalogues, same author.

Everything is computed from saved data (no scraping), so it can be recalculated any time - e.g. after
next week's book list is imported, links to books that were 'not in our list' become active automatically.
Output: outputs/book_relations.xlsx (+ per-book relation features for the prediction model).
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime

from .config import Settings
from .db import now
from .util import log, write_xlsx

# How strongly a link suggests "these could be sold together". Tune after checking against real bundles.
WEIGHTS = {
    "same_series": 3.0,                 # both volumes in our list (series name read from the page)
    "series_link": 3.0,                 # VLB-TIX label 'Produkt derselben Reihe'
    "reference": 2.0,                   # other Referenztitel chosen by the publisher (label may be a series name)
    "same_author": 1.0,                 # from our own Excel list - supporting evidence only (prolific authors!)
    "similar": 1.0,                     # VLB-TIX label 'Ähnliches Produkt'
    "comparison": 1.0,                  # VLB-TIX label 'Vergleichstitel' (a forecasting input, weak bundle signal)
    "shared_preview": 0.5,              # same publisher catalogue/season
}


def relation_kind(relation: str) -> str | None:
    """Map the label shown on VLB-TIX to a link kind. None = a link but never a bundle (other edition)."""
    r = (relation or "").lower()
    if "ausgabe" in r or r.startswith("ersetzt"):
        return None          # other edition / replaces an earlier edition: a link, never a bundle
    if "desselben autors" in r or "derselben autorin" in r:
        return "same_author"
    if "vergleichstitel" in r:
        return "comparison"  # publisher's comparison title: same positioning, often a competitor
    if "derselben reihe" in r or r.startswith("aus der reihe") or "weitere bände" in r:
        return "series_link"
    if r.startswith("ähnlich"):
        return "similar"
    return "reference"


BUNDLE_MIN_SCORE = 2.0


def _norm_series(name: str | None) -> str | None:
    return re.sub(r"\s+", " ", name.strip().lower()) if name else None


def _norm_author(a: str | None) -> str | None:
    if not a:
        return None
    first = a.split(";")[0].split(" / ")[0].strip()
    if "," in first:  # "Henchen, Julia" -> "julia henchen"
        last, given = [x.strip() for x in first.split(",", 1)]
        first = f"{given} {last}"
    return re.sub(r"\s+", " ", first.lower()) or None


VLB_KINDS = {"series_link", "reference", "similar", "same_series", "comparison"}


def fmt_date(d: str | None) -> str | None:
    """Show every date as DD.MM.YYYY (VLB-TIX uses that, the Excel list uses YYYY-MM-DD)."""
    p = _parse_de_date(d)
    return p.strftime("%d.%m.%Y") if p else d


def _parse_de_date(d: str | None):
    if not d:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d[:10], fmt).date()
        except ValueError:
            pass
    return None


class _UnionFind:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def build_relations(conn: sqlite3.Connection) -> dict:
    books = {r[0]: {"isbn13": r[0], "asin": r[1], "author": r[2], "publisher": r[3], "pub_date": r[4]}
             for r in conn.execute("SELECT isbn13, asin, author, publisher, publication_date FROM books")}
    info = {r[0]: {"series": r[1], "volume": r[2], "price": r[3], "et": r[4], "availability": r[5]}
            for r in conn.execute("SELECT isbn13, series_name, volume, price_eur, release_date, availability "
                                  "FROM book_vlbtix_info")}
    rels = [dict(zip(("isbn13", "relation", "related_ean", "title", "author", "publisher", "format", "price",
                      "release_date", "availability", "url", "seen_at"), r)) for r in conn.execute(
        "SELECT isbn13, relation, related_ean, title, author, publisher, format, price_eur, release_date, "
        "availability, related_url, seen_at FROM product_relations ORDER BY isbn13, relation")]
    previews = [dict(zip(("isbn13", "name", "publisher", "date", "count", "url", "kind"), r)) for r in conn.execute(
        "SELECT isbn13, preview_name, preview_publisher, preview_date, product_count, preview_url, "
        "COALESCE(kind, 'preview') FROM preview_memberships ORDER BY isbn13")]

    pairs: dict[tuple[str, str], dict] = {}

    def add(a, b, kind, reason):
        if a == b or a not in books or b not in books:
            return
        key = tuple(sorted((a, b)))
        p = pairs.setdefault(key, {"score": 0.0, "kinds": set(), "reasons": []})
        if kind in p["kinds"]:
            return  # count each kind once per pair (A->B and B->A are the same evidence)
        p["kinds"].add(kind)
        p["score"] += WEIGHTS.get(kind, 0.0)
        p["reasons"].append(reason)

    # 1) VLB-TIX related products where the related book is in our list
    for r in rels:
        kind = relation_kind(r["relation"])
        if kind is None:
            continue
        add(r["isbn13"], r["related_ean"], kind, f'VLB-TIX "{r["relation"]}"')
    # 2) same series name (both in our list)
    by_series = defaultdict(list)
    for isbn, i in info.items():
        if i["series"]:
            by_series[_norm_series(i["series"])].append(isbn)
    for members in by_series.values():
        for a in members:
            for b in members:
                if a < b:
                    add(a, b, "same_series", f'same series "{info[a]["series"]}" '
                                             f'(Band {info[a]["volume"]} & {info[b]["volume"]})')
    # 3) same author (from our own Excel list)
    by_author = defaultdict(list)
    for isbn, b in books.items():
        if _norm_author(b["author"]):
            by_author[_norm_author(b["author"])].append(isbn)
    for name, members in by_author.items():
        if 1 < len(members) <= 30:  # very prolific "authors" (e.g. 'Diverse') would only add noise
            for a in members:
                for b in members:
                    if a < b:
                        add(a, b, "same_author", f"same author ({books[a]['author']})")
    # 4) shared preview catalogue (weak)
    by_preview = defaultdict(list)
    for p in previews:
        if p["kind"] != "special":
            by_preview[(p["name"], p["date"])].append(p["isbn13"])
    for (name, d), members in by_preview.items():
        members = sorted(set(members))
        if 1 < len(members) <= 40:
            for a in members:
                for b in members:
                    if a < b:
                        add(a, b, "shared_preview", f'same preview "{name}" ({d})')

    # clusters over meaningful links
    uf = _UnionFind()
    for (a, b), p in pairs.items():
        if p["score"] >= BUNDLE_MIN_SCORE:
            uf.union(a, b)
    clusters = defaultdict(list)
    for isbn in {x for k in pairs for x in k}:
        if any(p["score"] >= BUNDLE_MIN_SCORE for k, p in pairs.items() if isbn in k):
            clusters[uf.find(isbn)].append(isbn)
    cluster_rows, cluster_of = [], {}
    for n, members in enumerate(sorted(clusters.values(), key=lambda m: (-len(m), m[0])), 1):
        cid = f"C{n:04d}"
        for m in members:
            cluster_of[m] = (cid, len(members))
        cluster_rows.append([cid, len(members), "; ".join(sorted(members)),
                             "; ".join(sorted({books[m]["author"] or "?" for m in members})),
                             "; ".join(sorted({books[m]["publisher"] or "?" for m in members}))])
    return {"books": books, "info": info, "rels": rels, "previews": previews, "pairs": pairs,
            "clusters": cluster_rows, "cluster_of": cluster_of, "by_series": by_series}


def relations_report(conn: sqlite3.Connection, settings: Settings) -> dict:
    d = build_relations(conn)
    books, info, pairs = d["books"], d["info"], d["pairs"]
    today = date.today()

    links = [[r["isbn13"], r["relation"], r["related_ean"], "yes" if r["related_ean"] in books else "no",
              r["title"], r["author"], r["publisher"], r["format"], r["price"], fmt_date(r["release_date"]),
              r["availability"],
              r["url"], (r["seen_at"] or "").replace("T", " ").split("+")[0]] for r in d["rels"]]

    series_rows = []
    for key, members in sorted(d["by_series"].items()):
        for m in sorted(members, key=lambda x: (info[x]["volume"] or 0)):
            earlier = [x for x in members if (info[x]["volume"] or 0) < (info[m]["volume"] or 0)]
            series_rows.append([info[m]["series"], info[m]["volume"], m, books[m]["author"], books[m]["publisher"],
                                fmt_date(info[m]["et"]), len(members), "yes" if earlier else "no"])

    preview_rows = [[p["isbn13"], "Special" if p["kind"] == "special" else "Vorschau", p["name"], p["publisher"],
                     fmt_date(p["date"]), p["count"], p["url"]] for p in d["previews"]]

    def side(x):
        b, i = books[x], info.get(x, {})
        return [x, b["author"], b["publisher"], fmt_date(i.get("et") or b["pub_date"]), i.get("price")]

    bundle_rows = []
    ranked = sorted(pairs.items(), key=lambda kv: (not (kv[1]["kinds"] & VLB_KINDS), -kv[1]["score"], kv[0]))
    for (a, b), p in ranked:
        if p["score"] < BUNDLE_MIN_SCORE:
            continue
        ia, ib = info.get(a, {}), info.get(b, {})
        pa, pb = ia.get("price"), ib.get("price")
        da, db = _parse_de_date(ia.get("et") or books[a]["pub_date"]), _parse_de_date(ib.get("et") or books[b]["pub_date"])
        bundle_rows.append([*side(a), *side(b), round(p["score"], 1),
                            "VLB-TIX" if p["kinds"] & VLB_KINDS else "own list only", "; ".join(p["reasons"]),
                            "yes" if (books[a]["publisher"] or "") == (books[b]["publisher"] or "") else "no",
                            round(abs(pa - pb), 2) if pa is not None and pb is not None else None,
                            "yes" if (da and db and da <= today and db <= today) else ("no" if da and db else "?"),
                            d["cluster_of"].get(a, ("", 0))[0]])

    by_author = defaultdict(list)
    for isbn, b in books.items():
        if _norm_author(b["author"]):
            by_author[_norm_author(b["author"])].append(isbn)
    comp_counts = defaultdict(int)
    replaces = set()
    for r in d["rels"]:
        rl = (r["relation"] or "").lower()
        if "vergleichstitel" in rl:
            comp_counts[r["isbn13"]] += 1
        if rl.startswith("ersetzt"):
            replaces.add(r["isbn13"])
    rel_counts = defaultdict(lambda: [0, 0])
    for r in d["rels"]:
        rel_counts[r["isbn13"]][0] += 1
        rel_counts[r["isbn13"]][1] += r["related_ean"] in books
    prev_counts = defaultdict(int)
    special_counts = defaultdict(int)
    for p in d["previews"]:
        if p["kind"] == "special":
            special_counts[p["isbn13"]] += 1
        else:
            prev_counts[p["isbn13"]] += 1
    feat_rows = []
    for isbn in sorted(books):
        i = info.get(isbn, {})
        members = d["by_series"].get(_norm_series(i.get("series")), []) if i.get("series") else []
        earlier = [m for m in members if (info[m]["volume"] or 0) < (i.get("volume") or 0)]
        strong = [k for k, p in pairs.items() if isbn in k and p["score"] >= BUNDLE_MIN_SCORE]
        cid, csize = d["cluster_of"].get(isbn, ("", 0))
        same_author = len(by_author.get(_norm_author(books[isbn]["author"]), [])) - 1 \
            if _norm_author(books[isbn]["author"]) else 0
        feat_rows.append([isbn, i.get("series"), i.get("volume"), len(members), "yes" if earlier else "no",
                          rel_counts[isbn][0], rel_counts[isbn][1], comp_counts[isbn],
                          "yes" if isbn in replaces else "no", prev_counts[isbn], special_counts[isbn],
                          max(same_author, 0),
                          len(strong), cid, csize])

    summary = [["Books in list", len(books)],
               ["Books with VLB-TIX page data", len(info)],
               ["Related products saved", len(d["rels"])],
               ["  of which are in our list", sum(1 for r in d["rels"] if r["related_ean"] in books)],
               ["Books in a series (series name found)", sum(1 for i in info.values() if i["series"])],
               ["Preview catalogue memberships", sum(1 for p in d["previews"] if p["kind"] != "special")],
               ["Specials (e.g. award nominations, magazines)", sum(1 for p in d["previews"] if p["kind"] == "special")],
               ["Bundle candidates (score >= 2)", len(bundle_rows)],
               ["  of which backed by VLB-TIX links", sum(1 for r in bundle_rows if r[11] == "VLB-TIX")],
               ["Clusters of linked books", len(d["clusters"])],
               ["Report created (UTC)", now().replace("T", " ").split("+")[0]],
               ["Scoring", "same series / 'Produkt derselben Reihe' 3 · other Referenztitel 2 · same author 1 · "
                            "'Ähnliches Produkt' 1 · 'Vergleichstitel' 1 · same preview 0.5 · 'Ersetzt'/other editions = link only "
                            "(each kind counted once per pair)"]]

    path = write_xlsx(settings.path(settings.outputs_dir) / "book_relations.xlsx", [
        ("Summary", ["Metric", "Value"], summary),
        ("Bundle candidates", ["Book A", "Author A", "Publisher A", "Release A", "Price A (EUR)",
                               "Book B", "Author B", "Publisher B", "Release B", "Price B (EUR)",
                               "Score", "Evidence", "Why", "Same publisher", "Price difference (EUR)", "Both released",
                               "Cluster"],
         bundle_rows),
        ("Links", ["Our book", "Relation", "Related EAN", "Related in our list", "Title", "Author", "Publisher",
                   "Format", "Price (EUR)", "Release date", "Availability", "VLB-TIX page", "Seen at (UTC)"], links),
        ("Series", ["Series", "Volume", "EAN", "Author", "Publisher", "Release date", "Volumes in our list",
                    "Earlier volume in our list"], series_rows),
        ("Previews & Specials", ["Our book", "Type", "Name", "Publisher", "Date", "Products", "VLB-TIX page"],
         preview_rows),
        ("Clusters", ["Cluster", "Size", "EANs", "Authors", "Publishers"], d["clusters"]),
        ("Book features", ["EAN", "Series", "Volume", "Volumes of series in list", "Earlier volume in list",
                           "Related products (VLB-TIX)", "Related products in our list",
                           "Comparison titles (Vergleichstitel)", "Replaces an earlier edition", "Preview catalogues",
                           "Specials", "Other books by same author in list", "Strong links (score >= 2)", "Cluster",
                           "Cluster size"], feat_rows),
    ], {"Metric": 40, "Value": 70, "Why": 60, "Title": 40, "VLB-TIX page": 45, "EANs": 60})
    log.info("relations report: %d links, %d bundle candidates, %d clusters", len(links), len(bundle_rows),
             len(d["clusters"]))
    return {"related_products": len(links), "related_in_our_list": sum(1 for r in links if r[3] == "yes"),
            "series_books": sum(1 for i in info.values() if i["series"]), "bundle_candidates": len(bundle_rows),
            "bundle_candidates_backed_by_vlbtix": sum(1 for r in bundle_rows if r[11] == "VLB-TIX"),
            "clusters": len(d["clusters"]), "excel_file": str(path)}
