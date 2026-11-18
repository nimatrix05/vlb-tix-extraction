"""Excel import mimicking the 'Weekly Book Release' layout (synthetic values only)."""
import datetime as dt

import openpyxl
import pytest

from pipeline import db as dbm
from pipeline.books import import_books
from pipeline.isbn import make_isbn13
from pipeline.tabular import read_book_rows

E1, E2, E3 = make_isbn13("978399990010"), make_isbn13("978399990011"), make_isbn13("978399990012")


def make_workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Overview"
    ws.append(["Last updated:", dt.datetime(2026, 10, 30), None, "Lookup tab only"])                 # title row
    ws.append(["Color Status", "Comments", "ASIN", "EAN", "Author", "Brand", "CM", "Publication Date"])  # EN header
    ws.append(["Farbstatus", "Kommentare", "ASIN", "EAN", "Autor", "Marke", "CM", "Veröffentlichungsdatum"])  # DE header
    ws.append(["Red", "intern", "3999900103", E1, "Muster, Max", "Fiktiv Verlag", "Some Person", dt.datetime(2026, 12, 31)])
    ws.append([None, None, "B0TESTKIND", int(E2), "Beispiel, Eva", "Beispiel & Söhne", None, None])   # numeric EAN cell
    ws.append([None, None, "B0GADGET01", "4255799802182", None, "Spielwaren GmbH", None, None])      # non-book EAN
    ws.append([None, None, E3[3:12] + "X", "", "Nur ASIN", "X", None, None])                           # placeholder, fixed below
    ws.append([None, None, "3999900103", E1, None, None, None, None])                                    # duplicate
    ws.append([None] * 8)                                                                                 # empty row
    ws.append([None, None, "B0ONLYASIN", "", None, None, None, None])                                    # nothing usable
    # row 7: an ISBN-10 ASIN without EAN -> must be converted
    isbn10_body = E3[3:12]
    total = sum((10 - i) * int(c) for i, c in enumerate(isbn10_body))
    check = (11 - total % 11) % 11
    ws.cell(row=7, column=3, value=isbn10_body + ("X" if check == 10 else str(check)))
    ws2 = wb.create_sheet("Keepa_SalesRank")
    ws2.append(["ASIN", "Date", "SR"])
    wb.save(path)


def test_header_detection_skips_title_and_second_header(tmp_path):
    p = tmp_path / "w.xlsx"
    make_workbook(p)
    rows = list(read_book_rows(p))
    assert rows[0][0] == 4 and rows[0][1]["isbn"] == E1
    assert rows[0][1]["publisher"] == "Fiktiv Verlag" and rows[0][1]["author"] == "Muster, Max"
    assert rows[0][1]["publication_date"] == "2026-12-31"
    assert "cm" not in rows[0][1] and "comments" not in rows[0][1]       # business columns are not read
    assert rows[1][1]["isbn"] == E2                                          # int cell -> clean string


def test_xlsx_import_with_asin_fallback_and_reasons(project, tmp_path):
    s, conn = project
    p = tmp_path / "w.xlsx"
    make_workbook(p)
    rep = import_books(conn, p)
    assert rep.inserted == 3 and rep.duplicates_in_file == 1 and rep.from_asin == 1
    reasons = rep.invalid_by_reason()
    assert reasons == {"EAN is not a book (prefix not 978/979)": 1, "no EAN/ISBN": 1}
    row = conn.execute("SELECT asin, author, publisher, publication_date FROM books WHERE isbn13=?", (E1,)).fetchone()
    assert tuple(row) == ("3999900103", "Muster, Max", "Fiktiv Verlag", "2026-12-31")
    assert import_books(conn, p).unchanged == 3


def test_unknown_sheet_and_missing_header(tmp_path, project):
    s, conn = project
    p = tmp_path / "w.xlsx"
    make_workbook(p)
    with pytest.raises(ValueError, match="not found"):
        list(read_book_rows(p, sheet="Nope"))
    with pytest.raises(ValueError, match="no ISBN/EAN/ASIN header"):
        wb = openpyxl.Workbook()
        for i in range(20):
            wb.active.append(["x", i])
        wb.save(tmp_path / "bad.xlsx")
        list(read_book_rows(tmp_path / "bad.xlsx"))


def test_migration_adds_columns_to_old_database(tmp_path):
    conn = dbm.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE books (book_id TEXT PRIMARY KEY, isbn13 TEXT NOT NULL UNIQUE, title TEXT, publisher TEXT,"
                 " source_ref TEXT, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    dbm.init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
    assert {"asin", "author", "publication_date"} <= cols


def test_collection_report_excel_layout(project, tmp_path):
    from pipeline.export import collection_report
    s, conn = project
    p = tmp_path / "w.xlsx"
    make_workbook(p)
    import_books(conn, p)
    conn.execute("INSERT INTO vlbtix_lookups(isbn13, status, product_page, images_saved, checked_at) "
                 "VALUES (?, 'not_found', NULL, 0, '2026-11-17T09:00:00+00:00')", (E1,))
    conn.commit()
    rep = collection_report(conn, s)
    assert rep["by_vlbtix_status"] == {"not_found": 1, "not_checked": 2}
    wb = openpyxl.load_workbook(rep["excel_file"])
    assert wb.sheetnames == ["Summary", "Books", "Images", "Problems"]
    ws = wb["Books"]
    assert [c.value for c in ws[1]][:6] == ["EAN / ISBN-13", "ASIN", "Author", "Publisher", "Publication date",
                                           "VLB-TIX status"]
    assert ws.max_column == 11 and ws.freeze_panes == "A2" and ws.auto_filter.ref
    assert ws.cell(row=2, column=1).value == E1 and ws.cell(row=2, column=6).value == "not found on VLB-TIX"
    csv_head = (s.path("outputs/collection_report.csv")).read_text(encoding="utf-8-sig").splitlines()[0]
    assert csv_head.count(";") == 10   # ';' so German Excel splits columns


def test_xlsx_never_writes_formulas(tmp_path):
    from pipeline.util import write_xlsx
    write_xlsx(tmp_path / "x.xlsx", [("T", ["a"], [["=HYPERLINK(\"http://evil\")"]])])
    c = openpyxl.load_workbook(tmp_path / "x.xlsx")["T"]["A2"]
    assert c.data_type == "s"


def test_remove_demo_books(project, tmp_path):
    from pipeline.books import remove_demo_books
    s, conn = project
    from conftest import write
    import_books(conn, write(tmp_path / "b.csv", f"isbn13,title\n{E1},SYNTHETIC – demo\n{E2},Echtes Buch\n"))
    assert remove_demo_books(conn)["removed_books"] == 1
    assert [r[0] for r in conn.execute("SELECT isbn13 FROM books")] == [E2]


def test_locked_file_falls_back_to_timestamped_copy(tmp_path, monkeypatch):
    import os
    from pipeline import util
    target = tmp_path / "report.csv"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace

    def locked(src, dst):
        if str(dst) == str(target):
            raise PermissionError(13, "file is open in Excel")
        return real_replace(src, dst)
    monkeypatch.setattr(util.os, "replace", locked)
    util.write_csv(target, ["a"], [["1"]])
    copies = [p.name for p in tmp_path.iterdir() if p.name.startswith("report_")]
    assert target.read_text(encoding="utf-8") == "old" and len(copies) == 1
