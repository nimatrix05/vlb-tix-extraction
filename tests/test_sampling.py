"""Stratified discovery sample (synthetic Excel shaped like the weekly book list)."""
import datetime as dt
from collections import Counter

import openpyxl
import pytest

from pipeline.batches import prepare_batch
from pipeline.books import import_books
from pipeline.cli import main
from pipeline.collector import import_local_images
from pipeline.db import now
from pipeline.isbn import make_isbn13
from pipeline.sampling import build_sample, parse_quotas, refresh_sample, sample_isbns, quarter_of

from conftest import image_bytes

BANDS = ["Top", "Good", "Normal", "Bad", ""]


def _eans(n):
    return [make_isbn13(f"97839998{i:04d}") for i in range(n)]


def make_list(path, n=200):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Overview"
    ws.append(["Last updated:", dt.datetime(2026, 10, 30)])
    ws.append(["ASIN", "EAN", "Author", "AMZ Offer", "Brand", "Publication Date", "Type of Release (Sales Rank)"])
    ws.append(["ASIN", "EAN", "Autor", "Angebotspreis", "Marke", "Veröffentlichungsdatum", None])
    for i, e in enumerate(_eans(n)):
        ws.append([f"B0{i:08d}", e, f"Autor {i}", 15 + (i % 12), f"Verlag {i % 25}",
                   dt.datetime(2026, 1 + (i % 12), 1 + (i % 27)), BANDS[i % 5]])
    wb.save(path)
    return path


@pytest.fixture
def listed(project, tmp_path):
    s, conn = project
    p = make_list(tmp_path / "weekly.xlsx")
    import_books(conn, p)
    return s, conn, p


def test_quotas_strata_and_publisher_cap(listed):
    s, conn, p = listed
    q = {"Top": 8, "Good": 10, "Normal": 10, "Bad": 8}
    rep = build_sample(conn, s, p, quotas=q, seed=7, max_per_publisher=3)
    chosen = conn.execute("SELECT * FROM sample_books WHERE role='chosen'").fetchall()
    assert Counter(r["band"] for r in chosen) == Counter(q)
    assert max(Counter(r["publisher"] for r in chosen).values()) <= 3
    assert {r["quarter"] for r in chosen} >= {"Q1", "Q2", "Q3", "Q4"}
    assert {r["price_group"] for r in chosen} == {"low", "mid", "high"}
    assert rep["reserve"] > 0 and not rep["shortages"]
    assert "" not in {r["band"] for r in chosen}               # books without a band never drawn
    assert (s.path(s.outputs_dir) / "sample_books.xlsx").exists()


def test_same_seed_same_sample_and_no_silent_overwrite(listed):
    s, conn, p = listed
    q = {"Top": 5, "Bad": 5}
    build_sample(conn, s, p, quotas=q, seed=1)
    first = sample_isbns(conn, "discovery")
    with pytest.raises(ValueError, match="already exists"):
        build_sample(conn, s, p, quotas=q, seed=1)
    build_sample(conn, s, p, quotas=q, seed=1, replace=True)
    assert sample_isbns(conn, "discovery") == first
    build_sample(conn, s, p, quotas=q, seed=2, replace=True)
    assert sample_isbns(conn, "discovery") != first


def test_already_analysed_books_count_towards_quota(listed):
    s, conn, p = listed
    top_book = _eans(200)[0]                                     # i=0 -> band Top
    conn.execute("PRAGMA foreign_keys=OFF")                      # shortcut: fake one analysed book
    conn.execute("INSERT INTO extractions(import_id, batch_id, isbn13, image_id, image_type, visible_text, "
                 "needs_review, raw_record, is_current, created_at) VALUES (1,'b',?, 'img_001','front_cover','',0,'{}',1,?)",
                 (top_book, now()))
    build_sample(conn, s, p, quotas={"Top": 4}, seed=3)
    row = conn.execute("SELECT * FROM sample_books WHERE isbn13=?", (top_book,)).fetchone()
    assert row["role"] == "chosen" and row["reason"].startswith("already analysed")
    assert len(sample_isbns(conn, "discovery")) == 4


def test_failed_book_replaced_by_reserve_from_same_cell(listed):
    s, conn, p = listed
    build_sample(conn, s, p, quotas={"Good": 6}, seed=5)
    victim = sample_isbns(conn, "discovery")[0]
    cell = conn.execute("SELECT band, quarter FROM sample_books WHERE isbn13=?", (victim,)).fetchone()
    conn.execute("INSERT INTO vlbtix_lookups(isbn13, status, checked_at) VALUES (?, 'not_found', ?)", (victim, now()))
    rep = refresh_sample(conn, "discovery")
    assert rep["dropped"] == [victim] and len(rep["promoted"]) == 1
    new = conn.execute("SELECT * FROM sample_books WHERE isbn13=?", (rep["promoted"][0],)).fetchone()
    assert new["role"] == "chosen" and new["band"] == cell["band"]
    assert victim not in sample_isbns(conn, "discovery") and len(sample_isbns(conn, "discovery")) == 6


def test_prepare_batch_only_sample_and_per_book_cap(listed):
    s, conn, p = listed
    build_sample(conn, s, p, quotas={"Top": 2}, seed=9)
    inside = sample_isbns(conn, "discovery")
    outside = next(e for e in _eans(200) if e not in inside)
    folder = s.root / "imgs"
    folder.mkdir()
    rows = []
    for isbn, n in ((inside[0], 5), (inside[1], 1), (outside, 2)):
        for k in range(n):
            name = f"{isbn}_{k}.png"
            (folder / name).write_bytes(image_bytes((k * 40, 10, 200 if isbn == outside else 20)))
            rows.append(f"{isbn},imgs/{name}")
    (s.root / "m.csv").write_text("isbn13,file_path\n" + "\n".join(rows) + "\n", encoding="utf-8")
    import_local_images(conn, s, s.root / "m.csv")
    first = prepare_batch(conn, s, books=10, only_isbns=inside, per_book=3)
    assert first["images"] == 4                                   # 3 of 5 + 1 of 1, nothing from outside
    again = prepare_batch(conn, s, books=10, only_isbns=inside, per_book=3)
    assert again["batch_id"] is None                              # cap already used up
    keys = {r[0] for r in conn.execute("SELECT isbn13 FROM batch_items")}
    assert outside not in keys


def test_cli_commands(listed, capsys):
    s, conn, p = listed
    assert main(["--root", str(s.root), "sample-books", str(p), "--quota", "Top=3,Bad=3", "--dry-run"]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert main(["--root", str(s.root), "sample-books", str(p), "--size", "20"]) == 0
    assert main(["--root", str(s.root), "sample-report"]) == 0
    assert main(["--root", str(s.root), "prepare-batch", "--sample", "--per-book", "3", "--count", "5"]) == 0


def test_helpers():
    assert parse_quotas(None) == {"Top": 35, "Good": 40, "Normal": 40, "Bad": 35}
    assert sum(parse_quotas(None, 60).values()) == 60
    assert parse_quotas("Top=1, Bad=2") == {"Top": 1, "Bad": 2}
    with pytest.raises(ValueError):
        parse_quotas("Top=x")
    assert quarter_of("2026-11-03") == "Q4" and quarter_of("") == "?"


def test_analysed_books_respect_publisher_cap(listed):
    s, conn, p = listed
    conn.execute("PRAGMA foreign_keys=OFF")
    same_pub = [e for i, e in enumerate(_eans(200)) if i % 25 == 0 and i % 5 == 0][:5]   # Verlag 0, band Top
    for e in same_pub:
        conn.execute("INSERT INTO extractions(import_id, batch_id, isbn13, image_id, image_type, visible_text, "
                     "needs_review, raw_record, is_current, created_at) VALUES (1,'b',?,'img_001','x','',0,'{}',1,?)",
                     (e, now()))
    rep = build_sample(conn, s, p, quotas={"Top": 6}, seed=1, max_per_publisher=2)
    assert rep["already_analysed_included"] == 2 and rep["already_analysed_not_counted_publisher_cap"] == 3
    assert rep["max_books_per_publisher"] <= 2
