"""Method 1 — approved image URL manifest (CSV: isbn13, image_url[, source_page, rights_note])."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from ..db import now
from ..isbn import to_isbn13
from ..util import log, read_csv_rows


def import_url_manifest(conn: sqlite3.Connection, csv_path: Path, dry_run: bool = False) -> dict:
    rep = {"rows": 0, "registered": 0, "already_known": 0, "invalid_isbn": 0,
           "unknown_book": 0, "invalid_url": 0}
    known_books = {r[0] for r in conn.execute("SELECT isbn13 FROM books")}
    ts = now()
    for line_no, row in enumerate(read_csv_rows(csv_path), start=2):
        rep["rows"] += 1
        isbn = to_isbn13(row.get("isbn13") or row.get("isbn") or row.get("ean"))
        url = row.get("image_url") or row.get("url") or ""
        if not isbn:
            rep["invalid_isbn"] += 1
            log.warning("line %d: invalid ISBN", line_no)
            continue
        if isbn not in known_books:
            rep["unknown_book"] += 1
            log.warning("line %d: ISBN %s not imported yet (run import-books first)", line_no, isbn)
            continue
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("https", "http") or not parts.hostname:
            rep["invalid_url"] += 1
            log.warning("line %d: invalid URL %r", line_no, url[:200])
            continue
        if dry_run:
            rep["registered"] += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO image_sources(isbn13, url, source_page, method, rights_note, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (isbn, url, row.get("source_page") or None, "url_manifest",
             row.get("rights_note") or row.get("license") or None, ts, ts))
        rep["registered" if cur.rowcount else "already_known"] += 1
    conn.commit()
    return rep
