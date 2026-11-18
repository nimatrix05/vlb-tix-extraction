import time

import pytest

from conftest import ISBN_A, ISBN_B, write
from pipeline.books import import_books
from pipeline.isbn import is_valid_isbn13, isbn10_to_13, make_isbn13, to_isbn13
from pipeline.security import UnsafeURLError, check_url, contained_path
from pipeline.util import csv_safe, read_csv_rows


# ---------------------------------------------------------------- ISBN
@pytest.mark.parametrize("raw,ok", [
    ("9783161484100", True), ("978-3-16-148410-0", True), ("ISBN 978-3-16-148410-0", True),
    ("9783161484101", False), ("1234567890123", False), ("97831614841", False), ("", False), (None, False),
])
def test_isbn13_validation(raw, ok):
    assert is_valid_isbn13(raw) is ok


def test_isbn10_conversion():
    assert isbn10_to_13("3-16-148410-X") == "9783161484100"
    assert to_isbn13("316148410X") == "9783161484100"
    assert to_isbn13("3161484101") is None


def test_import_books_dedup_and_incremental(project, tmp_path):
    s, conn = project
    csv1 = write(tmp_path / "b1.csv", f"isbn13,title,publisher\n{ISBN_A},A,\n{ISBN_A},A dup,\n9780000000000,bad,\n")
    r = import_books(conn, csv1)
    assert (r.inserted, r.duplicates_in_file, len(r.invalid)) == (1, 1, 1)
    # second import: existing book untouched except filling blank publisher; new one added
    csv2 = write(tmp_path / "b2.csv", f"ISBN;Title;Publisher\n{ISBN_A};A changed;Verlag X\n{ISBN_B};B;Y\n")
    r = import_books(conn, csv2)
    assert (r.inserted, r.updated) == (1, 1)
    row = conn.execute("SELECT book_id, title, publisher FROM books WHERE isbn13=?", (ISBN_A,)).fetchone()
    assert tuple(row) == (f"bk_{ISBN_A}", "A", "Verlag X")
    assert import_books(conn, csv2).unchanged == 2  # idempotent


def test_large_csv_100k_isbns(project, tmp_path):
    """Phase 6 scale check: 100,000 synthetic ISBNs, no network."""
    s, conn = project
    p = tmp_path / "big.csv"
    with p.open("w", encoding="utf-8") as fh:
        fh.write("isbn13,title\n")
        for i in range(100_000):
            fh.write(f"{make_isbn13(f'979{i:09d}')},Synthetic {i}\n")
    t = time.perf_counter()
    r = import_books(conn, p)
    elapsed = time.perf_counter() - t
    assert r.inserted == 100_000 and not r.invalid
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 100_000
    assert import_books(conn, p).unchanged == 100_000  # incremental re-run does no new work
    assert elapsed < 120


def test_csv_reader_handles_semicolon_bom_cp1252(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes("ISBN;Verlag\n9783161484100;Müller & Söhne\n".encode("cp1252"))
    assert list(read_csv_rows(p)) == [{"isbn": "9783161484100", "verlag": "Müller & Söhne"}]


def test_csv_formula_injection_neutralised():
    assert csv_safe("=HYPERLINK(\"x\")").startswith("'=")
    assert csv_safe("normal") == "normal"


# ---------------------------------------------------------------- URL safety
ALLOW = ["img.example.com"]


def pub(_h):
    return ["93.184.216.34"]


@pytest.mark.parametrize("url", [
    "http://img.example.com/a.jpg",            # http not allowed
    "ftp://img.example.com/a.jpg",
    "file:///etc/passwd",
    "https://evil.com/a.jpg",                  # not on allow-list
    "https://img.example.com.evil.com/a.jpg",  # suffix trick
    "https://user:pw@img.example.com/a.jpg",   # credentials
    "https://img.example.com:8443/a.jpg",      # odd port
    "https://localhost/a.jpg",
    "https://127.0.0.1/a.jpg",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/a.jpg",
    "https://metadata.google.internal/",
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeURLError):
        check_url(url, ALLOW, resolver=pub)


@pytest.mark.parametrize("ip", ["10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1", "fd00::1",
                                "::ffff:10.0.0.1"])
def test_private_dns_resolution_rejected(ip):
    with pytest.raises(UnsafeURLError):
        check_url("https://img.example.com/a.jpg", ALLOW, resolver=lambda h: [ip])


def test_safe_url_and_subdomain_accepted():
    assert check_url("https://cdn.img.example.com/a.jpg", ALLOW, resolver=pub)


def test_no_allow_list_means_nothing_allowed():
    with pytest.raises(UnsafeURLError, match="allowed_domains"):
        check_url("https://img.example.com/a.jpg", [], resolver=pub)


@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "CON", "nul.txt", "", ".hidden"])
def test_path_traversal_blocked(tmp_path, bad):
    with pytest.raises(ValueError):
        contained_path(tmp_path, bad)
