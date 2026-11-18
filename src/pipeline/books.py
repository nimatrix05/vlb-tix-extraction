"""Module A — ISBN input from CSV or Excel (.xlsx)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import now
from .isbn import normalize_isbn, to_isbn13
from .tabular import read_book_rows
from .util import log

CHUNK = 900  # stays under SQLite host-parameter limits
META = ("title", "publisher", "source_ref", "asin", "author", "publication_date")


@dataclass
class ImportReport:
    rows: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates_in_file: int = 0
    from_asin: int = 0
    invalid: list[tuple[int, str, str, str]] = field(default_factory=list)  # (line, ean, asin, reason)

    def summary(self) -> str:
        return (f"rows={self.rows} inserted={self.inserted} updated={self.updated} "
                f"unchanged={self.unchanged} duplicates_in_file={self.duplicates_in_file} "
                f"isbn_from_asin={self.from_asin} invalid={len(self.invalid)}")

    def invalid_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for *_x, reason in self.invalid:
            out[reason] = out.get(reason, 0) + 1
        return out


def book_id_for(isbn13: str) -> str:
    return f"bk_{isbn13}"


def _why_invalid(raw: str) -> str:
    s = normalize_isbn(raw)
    if not s:
        return "no EAN/ISBN"
    if not s.isdigit() and not (len(s) == 10 and s[:9].isdigit()):
        return "not numeric"
    if len(s) == 13 and s[:3] not in ("978", "979"):
        return "EAN is not a book (prefix not 978/979)"
    if len(s) not in (10, 13):
        return f"wrong length ({len(s)} digits)"
    return "check digit wrong"


def _asin_to_isbn13(asin: str) -> str | None:
    a = (asin or "").strip().upper()
    if a.isdigit() and len(a) < 10:
        a = a.zfill(10)  # Excel drops leading zeros of numeric ISBN-10 ASINs
    if len(a) == 10 and not a.startswith("B"):
        return to_isbn13(a)
    return None


def import_books(conn: sqlite3.Connection, path: Path, dry_run: bool = False, sheet: str | None = None) -> ImportReport:
    """Import/merge books. Existing ISBNs are kept; empty metadata is filled from the new file
    but never overwritten with blanks, so re-imports are idempotent.
    ISBN source: EAN/ISBN column; if missing or invalid, an ISBN-10 ASIN is converted."""
    rep = ImportReport()
    seen: set[str] = set()
    pending: list[tuple] = []

    def flush():
        if not pending or dry_run:
            pending.clear()
            return
        ts = now()
        existing = {r["isbn13"]: r for r in conn.execute(
            f"SELECT isbn13, {', '.join(META)} FROM books WHERE isbn13 IN ({','.join('?' * len(pending))})",
            [p[0] for p in pending])}
        for isbn, meta in pending:
            old = existing.get(isbn)
            if old is None:
                conn.execute(f"INSERT INTO books(book_id, isbn13, {', '.join(META)}, created_at, updated_at) "
                             f"VALUES (?,?,{','.join('?' * len(META))},?,?)",
                             (book_id_for(isbn), isbn, *[meta.get(k) or None for k in META], ts, ts))
                rep.inserted += 1
                continue
            new = tuple(old[k] or meta.get(k) or None for k in META)
            if new != tuple(old[k] for k in META):
                conn.execute(f"UPDATE books SET {', '.join(k + '=?' for k in META)}, updated_at=? WHERE isbn13=?",
                             (*new, ts, isbn))
                rep.updated += 1
            else:
                rep.unchanged += 1
        conn.commit()
        pending.clear()

    for line_no, row in read_book_rows(path, sheet):
        rep.rows += 1
        raw, asin = row.get("isbn", ""), row.get("asin", "")
        isbn = to_isbn13(raw)
        if not isbn:
            isbn = _asin_to_isbn13(asin)
            if isbn:
                rep.from_asin += 1
        if not isbn:
            rep.invalid.append((line_no, raw, asin, _why_invalid(raw)))
            continue
        if isbn in seen:
            rep.duplicates_in_file += 1
            continue
        seen.add(isbn)
        pending.append((isbn, {k: (row.get(k) or "").strip() for k in META}))
        if len(pending) >= CHUNK:
            flush()
    flush()
    for line, raw, asin, reason in rep.invalid[:10]:
        log.warning("line %s skipped: %s (EAN=%r, ASIN=%r)", line, reason, raw, asin)
    if len(rep.invalid) > 10:
        log.warning("... %d more skipped rows (see rejected-rows file)", len(rep.invalid) - 10)
    return rep


def remove_demo_books(conn: sqlite3.Connection) -> dict:
    """Delete synthetic demo books (title starts with 'SYNTHETIC') and anything attached to them.
    Real books are never touched. Demo image files on disk are left in place (delete data/images/9783999900*)."""
    isbns = [r[0] for r in conn.execute("SELECT isbn13 FROM books WHERE title LIKE 'SYNTHETIC%'")]
    if not isbns:
        return {"removed_books": 0}
    q = ",".join("?" * len(isbns))
    conn.execute(f"DELETE FROM findings WHERE isbn13 IN ({q})", isbns)
    conn.execute(f"DELETE FROM extractions WHERE isbn13 IN ({q})", isbns)
    conn.execute(f"DELETE FROM batch_items WHERE isbn13 IN ({q})", isbns)
    conn.execute(f"DELETE FROM images WHERE isbn13 IN ({q})", isbns)
    conn.execute(f"DELETE FROM image_sources WHERE isbn13 IN ({q})", isbns)
    conn.execute(f"DELETE FROM vlbtix_lookups WHERE isbn13 IN ({q})", isbns)
    conn.execute(f"DELETE FROM books WHERE isbn13 IN ({q})", isbns)
    conn.commit()
    return {"removed_books": len(isbns), "isbns": isbns}
