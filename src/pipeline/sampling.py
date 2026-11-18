"""Stratified sample of books for feature discovery (step 1 of the feature-list workflow).

Why: ChatGPT should see a *representative* set of books - hits and flops, many publishers, all
seasons and price levels - before we fix the feature list. The sales band from the weekly Excel
is used ONLY to choose the sample; it never becomes a feature.

Rules
- quotas per sales band (default Top 35, Good 40, Normal 40, Bad 35 = 150; Top is over-sampled)
- books already analysed by ChatGPT count towards their band's quota (no double work)
- within a band: spread over release quarters in proportion to the book list
- at most N books per publisher (default 3), new publishers preferred
- low / mid / high price thirds rotated within every cell
- seeded random draw: the same command gives the same list
- reserve books per cell replace chosen books that are not on VLB-TIX or have no images
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from .db import now
from .isbn import to_isbn13
from .tabular import read_book_rows
from .util import log, write_xlsx

DEFAULT_QUOTAS = {"Top": 35, "Good": 40, "Normal": 40, "Bad": 35}
PRICE_GROUPS = ("low", "mid", "high")
DROP_STATUSES = {"not_found": "not on VLB-TIX", "no_images": "no images on VLB-TIX"}


# ----------------------------------------------------------------------------- helpers
def parse_quotas(text: str | None, size: int | None = None) -> dict[str, int]:
    """'Top=35,Good=40' -> dict. Without text: default quotas, scaled to `size` if given."""
    if text:
        out = {}
        for part in text.split(","):
            if not part.strip():
                continue
            k, _, v = part.partition("=")
            if not k.strip() or not v.strip().isdigit():
                raise ValueError(f"bad quota {part!r}; use e.g. Top=35,Good=40,Normal=40,Bad=35")
            out[k.strip()] = int(v)
        return out
    if not size or size == sum(DEFAULT_QUOTAS.values()):
        return dict(DEFAULT_QUOTAS)
    return _largest_remainder({k: v for k, v in DEFAULT_QUOTAS.items()}, size)


def _largest_remainder(weights: dict[str, float], total: int, caps: dict[str, int] | None = None) -> dict[str, int]:
    """Split `total` proportionally to weights (integers, sum == total unless caps are too small)."""
    keys = [k for k, w in weights.items() if w > 0]
    if not keys or total <= 0:
        return {k: 0 for k in weights}
    wsum = sum(weights[k] for k in keys)
    raw = {k: total * weights[k] / wsum for k in keys}
    out = {k: int(math.floor(raw[k])) for k in keys}
    if caps:
        out = {k: min(out[k], caps.get(k, out[k])) for k in keys}
    left = total - sum(out.values())
    order = sorted(keys, key=lambda k: (-(raw[k] - math.floor(raw[k])), k))
    while left > 0:
        progressed = False
        for k in order:
            if left <= 0:
                break
            if caps is None or out[k] < caps.get(k, 10**9):
                out[k] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return {k: out.get(k, 0) for k in weights}


def quarter_of(date_text: str | None) -> str:
    try:
        month = int(str(date_text)[5:7])
    except (TypeError, ValueError):
        return "?"
    return f"Q{(month - 1) // 3 + 1}" if 1 <= month <= 12 else "?"


def _price(text: str | None) -> float | None:
    try:
        return float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return None


def read_pool(conn, path: Path, sheet: str | None, bands: list[str]) -> tuple[list[dict], dict]:
    """Books from the Excel that are in the database and have one of the wanted sales bands."""
    known = {r[0]: r for r in conn.execute("SELECT isbn13, title, publisher, publication_date FROM books")}
    canon = {b.lower(): b for b in bands}
    pool, seen, stats = [], set(), Counter()
    for _line, rec in read_book_rows(path, sheet):
        isbn = to_isbn13(rec.get("isbn") or "")
        if not isbn or isbn in seen:
            continue
        seen.add(isbn)
        if isbn not in known:
            stats["not_in_database"] += 1
            continue
        band = canon.get((rec.get("sales_band") or "").strip().lower())
        if not band:
            stats["band_" + ((rec.get("sales_band") or "").strip() or "(none)")] += 1
            continue
        b = known[isbn]
        date = rec.get("publication_date") or b[3] or ""
        pool.append({"isbn13": isbn, "band": band, "quarter": quarter_of(date),
                     "publisher": (rec.get("publisher") or b[2] or "").strip() or "(unknown)",
                     "price": _price(rec.get("price")), "title": b[1] or ""})
        stats["eligible"] += 1
    if not stats["eligible"] and not any(k.startswith("band_") and k != "band_(none)" for k in stats):
        raise ValueError("no sales band column found - expected a column 'Type of Release (Sales Rank)' "
                         "with values like Top / Good / Normal / Bad")
    # price thirds over the whole eligible pool
    prices = sorted(p["price"] for p in pool if p["price"] is not None)
    cuts = (prices[len(prices) // 3], prices[2 * len(prices) // 3]) if len(prices) >= 3 else None
    for p in pool:
        if p["price"] is None or cuts is None:
            p["price_group"] = "?"
        else:
            p["price_group"] = "low" if p["price"] < cuts[0] else ("mid" if p["price"] < cuts[1] else "high")
    return pool, dict(stats) | ({"price_cuts_eur": list(cuts)} if cuts else {})


def _pick(cands: list[dict], n: int, taken: set, pub_count: Counter, cap: int, rotate: int,
          count_publishers: bool) -> list[dict]:
    """Pick n books from shuffled candidates: rotate price groups, prefer new publishers, respect cap."""
    by_group = defaultdict(list)
    for c in cands:
        by_group[c["price_group"]].append(c)
    groups = [g for g in PRICE_GROUPS if by_group[g]] + (["?"] if by_group["?"] else [])
    if not groups:
        return []
    picked, step, local = [], rotate, Counter()

    def ok(c, max_count):
        return c["isbn13"] not in taken and pub_count[c["publisher"]] + local[c["publisher"]] < max_count

    while len(picked) < n:
        got = None
        for attempt in range(len(groups)):
            g = groups[(step + attempt) % len(groups)]
            for limit in (1, cap):                      # first a publisher not yet in the sample, then up to cap
                got = next((c for c in by_group[g] if ok(c, limit)), None)
                if got:
                    break
            if got:
                break
        if not got:
            break
        picked.append(got)
        taken.add(got["isbn13"])
        if count_publishers:
            pub_count[got["publisher"]] += 1
        else:
            local[got["publisher"]] += 1
        step += 1
    return picked


# ----------------------------------------------------------------------------- build
def build_sample(conn, settings, path: Path, sheet: str | None = None, name: str = "discovery",
                 quotas: dict[str, int] | None = None, seed: int = 42, max_per_publisher: int = 3,
                 reserve_share: float = 0.25, replace: bool = False, dry_run: bool = False) -> dict:
    quotas = quotas or dict(DEFAULT_QUOTAS)
    if conn.execute("SELECT 1 FROM sample_books WHERE sample_name=? LIMIT 1", (name,)).fetchone() and not replace:
        raise ValueError(f"sample {name!r} already exists - use sample-report to see it, "
                         f"--replace to draw it again, or --name for a second sample")
    pool, pool_stats = read_pool(conn, path, sheet, list(quotas))
    analysed = {r[0] for r in conn.execute("SELECT DISTINCT isbn13 FROM extractions WHERE is_current=1")}
    rng = random.Random(seed)
    taken: set = set()
    pub_count: Counter = Counter()
    rows, shortages, over_cap = [], [], []

    for band, quota in quotas.items():
        band_pool = sorted((p for p in pool if p["band"] == band), key=lambda p: p["isbn13"])
        pre = []
        for p in (p for p in band_pool if p["isbn13"] in analysed):
            if len(pre) >= quota:
                break
            if pub_count[p["publisher"]] >= max_per_publisher:   # keep the publisher mix balanced
                over_cap.append(p["isbn13"])
                continue
            pre.append(p)
            taken.add(p["isbn13"])
            pub_count[p["publisher"]] += 1
            rows.append(p | {"role": "chosen", "reason": "already analysed by ChatGPT"})
        rest = [p for p in band_pool if p["isbn13"] not in taken]
        cells = defaultdict(list)
        for p in rest:
            cells[p["quarter"]].append(p)
        for q in cells:
            rng.shuffle(cells[q])
        need = _largest_remainder({q: len(v) for q, v in sorted(cells.items())}, quota - len(pre),
                                  caps={q: len(v) for q, v in cells.items()})
        for i, (q, n) in enumerate(sorted(need.items())):
            got = _pick(cells[q], n, taken, pub_count, max_per_publisher, i, True)
            for p in got:
                rows.append(p | {"role": "chosen",
                                 "reason": f"random draw: {band} / {q} / price {p['price_group']}"})
            if len(got) < n:
                shortages.append(f"{band} {q}: wanted {n}, got {len(got)}")
            n_res = max(1, math.ceil(n * reserve_share)) if n else 0
            res = _pick(cells[q], n_res, taken, pub_count, max_per_publisher, i + 1, False)
            for p in res:
                rows.append(p | {"role": "reserve", "reason": f"reserve for {band} / {q}"})
        got_band = sum(1 for r in rows if r["band"] == band and r["role"] == "chosen")
        if got_band < quota:
            shortages.append(f"{band}: quota {quota}, only {got_band} available")

    chosen = [r for r in rows if r["role"] == "chosen"]
    summary = {
        "sample": name, "seed": seed, "pool": pool_stats, "chosen": len(chosen),
        "reserve": sum(1 for r in rows if r["role"] == "reserve"),
        "already_analysed_included": sum(1 for r in chosen if r["reason"].startswith("already")),
        "already_analysed_not_counted_publisher_cap": len(over_cap),
        "by_band": dict(Counter(r["band"] for r in chosen)),
        "by_quarter": dict(sorted(Counter(r["quarter"] for r in chosen).items())),
        "by_price_group": dict(Counter(r["price_group"] for r in chosen)),
        "publishers": len({r["publisher"] for r in chosen}),
        "max_books_per_publisher": max(Counter(r["publisher"] for r in chosen).values(), default=0),
        "shortages": shortages,
    }
    if dry_run:
        return summary | {"dry_run": True}
    ts = now()
    conn.execute("DELETE FROM sample_books WHERE sample_name=?", (name,))
    conn.executemany(
        "INSERT INTO sample_books(sample_name, isbn13, role, band, quarter, price_group, price_eur, publisher, "
        "reason, seed, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(name, r["isbn13"], r["role"], r["band"], r["quarter"], r["price_group"], r["price"], r["publisher"],
          r["reason"], seed, ts, ts) for r in rows])
    conn.commit()
    rep = sample_report(conn, settings, name)
    return summary | {"report": rep["report"]}


# ----------------------------------------------------------------------------- maintenance
def sample_isbns(conn, name: str, role: str = "chosen") -> list[str]:
    rows = conn.execute("SELECT isbn13 FROM sample_books WHERE sample_name=? AND role=? ORDER BY isbn13",
                        (name, role)).fetchall()
    if not rows and not conn.execute("SELECT 1 FROM sample_books WHERE sample_name=? LIMIT 1", (name,)).fetchone():
        raise ValueError(f"no sample named {name!r} - create it first with sample-books")
    return [r[0] for r in rows]


def refresh_sample(conn, name: str) -> dict:
    """Drop chosen books that are not on VLB-TIX / have no images and promote a reserve from the same cell
    (same band + quarter; otherwise same band)."""
    status = dict(conn.execute("SELECT isbn13, status FROM vlbtix_lookups").fetchall())
    rows = [dict(r) for r in conn.execute("SELECT * FROM sample_books WHERE sample_name=?", (name,))]
    dropped, promoted, unfilled = [], [], []
    ts = now()
    for r in rows:
        if r["role"] not in ("chosen", "reserve") or status.get(r["isbn13"]) not in DROP_STATUSES:
            continue
        was_chosen = r["role"] == "chosen"
        reason = DROP_STATUSES[status[r["isbn13"]]]
        conn.execute("UPDATE sample_books SET role='dropped', reason=?, updated_at=? WHERE sample_name=? AND isbn13=?",
                     (f"{reason} (was {r['role']})", ts, name, r["isbn13"]))
        r["role"] = "dropped"
        if not was_chosen:
            continue
        dropped.append(r["isbn13"])
        reserves = [x for x in rows if x["role"] == "reserve" and status.get(x["isbn13"]) not in DROP_STATUSES]
        pick = (next((x for x in reserves if x["band"] == r["band"] and x["quarter"] == r["quarter"]), None)
                or next((x for x in reserves if x["band"] == r["band"]), None))
        if not pick:
            unfilled.append(f"{r['band']} {r['quarter']} (for {r['isbn13']})")
            continue
        conn.execute("UPDATE sample_books SET role='chosen', reason=?, updated_at=? WHERE sample_name=? AND isbn13=?",
                     (f"reserve promoted - replaces {r['isbn13']} ({reason})", ts, name, pick["isbn13"]))
        pick["role"] = "chosen"
        promoted.append(pick["isbn13"])
    conn.commit()
    if unfilled:
        log.warning("no reserve left for: %s", "; ".join(unfilled))
    return {"dropped": dropped, "promoted": promoted, "no_reserve_left": unfilled}


def books_to_discover(conn, name: str) -> list[str]:
    """Chosen sample books not yet checked on VLB-TIX (or whose downloads failed)."""
    status = dict(conn.execute("SELECT isbn13, status FROM vlbtix_lookups").fetchall())
    return [i for i in sample_isbns(conn, name) if status.get(i) in (None, "download_failed")]


# ----------------------------------------------------------------------------- report
def sample_report(conn, settings, name: str = "discovery") -> dict:
    refresh = refresh_sample(conn, name)
    q = """
    SELECT s.role, s.isbn13, b.title, b.author, s.publisher, s.band, s.quarter, s.price_group, s.price_eur,
           COALESCE(l.status, 'not checked yet') AS vlbtix,
           (SELECT COUNT(*) FROM images i WHERE i.isbn13=s.isbn13) AS images,
           (SELECT COUNT(*) FROM images i WHERE i.isbn13=s.isbn13 AND i.analysis_status='pending') AS waiting,
           (SELECT COUNT(*) FROM images i WHERE i.isbn13=s.isbn13 AND i.analysis_status='batched') AS in_batch,
           (SELECT COUNT(*) FROM images i WHERE i.isbn13=s.isbn13
                AND i.analysis_status IN ('extracted','needs_review')) AS analysed,
           s.reason
    FROM sample_books s LEFT JOIN books b ON b.isbn13=s.isbn13 LEFT JOIN vlbtix_lookups l ON l.isbn13=s.isbn13
    WHERE s.sample_name=?
    ORDER BY CASE s.role WHEN 'chosen' THEN 0 WHEN 'reserve' THEN 1 ELSE 2 END, s.band, s.quarter, s.isbn13"""
    rows = [tuple(r) for r in conn.execute(q, (name,))]
    if not rows:
        raise ValueError(f"no sample named {name!r} - create it first with sample-books")
    head = ["Role", "EAN", "Title", "Author", "Publisher", "Sales band", "Quarter", "Price group", "Price EUR",
            "VLB-TIX", "Images", "Waiting", "In batch", "Analysed", "Why chosen"]
    chosen = [r for r in rows if r[0] == "chosen"]
    bands = sorted({r[5] for r in rows}, key=lambda b: list(DEFAULT_QUOTAS).index(b) if b in DEFAULT_QUOTAS else 99)
    summ = []
    for b in bands + ["Total"]:
        sel = chosen if b == "Total" else [r for r in chosen if r[5] == b]
        allb = rows if b == "Total" else [r for r in rows if r[5] == b]
        summ.append([b, len(sel), sum(1 for r in allb if r[0] == "reserve"), sum(1 for r in allb if r[0] == "dropped"),
                     *[sum(1 for r in sel if r[6] == qq) for qq in ("Q1", "Q2", "Q3", "Q4")],
                     len({r[4] for r in sel}), sum(1 for r in sel if r[9] == "found"),
                     sum(1 for r in sel if r[10] > 0), sum(1 for r in sel if r[13] > 0)])
    summ_head = ["Sales band", "Chosen", "Reserve", "Dropped", "Q1", "Q2", "Q3", "Q4", "Publishers",
                 "Found on VLB-TIX", "With images", "Analysed by ChatGPT"]
    guide = [
        ["What is this?", "A stratified random sample of books used to discover which features exist."],
        ["Role", "chosen = in the sample; reserve = replaces a chosen book that fails; dropped = not usable"],
        ["Sales band", "Used ONLY to pick a balanced sample (hits and flops) - never as a feature."],
        ["Next step", "python -m pipeline discover-vlbtix --sample   (finds images of the chosen books)"],
        ["Then", "python -m pipeline prepare-batch --sample --per-book 3 --count 20"],
    ]
    out = settings.path(settings.outputs_dir) / ("sample_books.xlsx" if name == "discovery" else f"sample_{name}.xlsx")
    written = write_xlsx(out, [("Summary", summ_head, summ), ("Sample", head, rows), ("How to read", ["Item", "Meaning"], guide)])
    return {"report": str(written), "chosen": len(chosen),
            "found_on_vlbtix": sum(1 for r in chosen if r[9] == "found"),
            "not_checked_yet": sum(1 for r in chosen if r[9] == "not checked yet"),
            "with_images": sum(1 for r in chosen if r[10] > 0),
            "analysed": sum(1 for r in chosen if r[13] > 0)} | refresh
