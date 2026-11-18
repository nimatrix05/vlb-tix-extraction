"""Module H output + status reporting. Everything stays keyed by ISBN and keeps its evidence."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from .config import Settings
from .db import now
from .taxonomy import current_mapping, iter_current_findings
from .util import csv_safe, log, slug, write_csv, write_json, write_xlsx


def status(conn: sqlite3.Connection) -> dict:
    def grp(sql):
        return {r[0]: r[1] for r in conn.execute(sql)}
    return {
        "books": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "books_by_status": grp("SELECT status, COUNT(*) FROM books GROUP BY 1"),
        "image_sources_by_status": grp("SELECT status, COUNT(*) FROM image_sources GROUP BY 1"),
        "images": conn.execute("SELECT COUNT(*) FROM images").fetchone()[0],
        "images_by_analysis_status": grp("SELECT analysis_status, COUNT(*) FROM images GROUP BY 1"),
        "batches_by_status": grp("SELECT status, COUNT(*) FROM batches GROUP BY 1"),
        "result_imports": conn.execute("SELECT COUNT(*) FROM result_imports").fetchone()[0],
        "current_extractions": conn.execute("SELECT COUNT(*) FROM extractions WHERE is_current=1").fetchone()[0],
        "findings": conn.execute("SELECT COUNT(*) FROM findings f JOIN extractions e ON "
                                 "e.extraction_id=f.extraction_id AND e.is_current=1").fetchone()[0],
        "distinct_attributes": conn.execute("SELECT COUNT(DISTINCT attribute) FROM findings").fetchone()[0],
        "taxonomy_version": conn.execute("SELECT MAX(version) FROM taxonomy_versions").fetchone()[0],
        "failed_sources_sample": [dict(r) for r in conn.execute(
            "SELECT isbn13, url, status, last_error FROM image_sources WHERE status IN ('failed','rejected') "
            "ORDER BY updated_at DESC LIMIT 5")],
    }


def write_image_manifest(conn: sqlite3.Connection, settings: Settings) -> int:
    rows = conn.execute(
        "SELECT i.image_key, i.isbn13, i.image_id, b.title, i.source_url, i.source_page, i.local_path, i.sha256, "
        "i.width, i.height, i.format, i.bytes, i.downloaded_at, i.rights_note, i.analysis_status "
        "FROM images i JOIN books b ON b.isbn13=i.isbn13 ORDER BY i.isbn13, i.image_id")
    header = ["image_key", "isbn13", "image_id", "title", "source_url", "source_page", "local_path", "sha256",
              "width", "height", "format", "bytes", "downloaded_at", "rights_note", "analysis_status"]
    rows = [tuple(r) for r in rows]
    n = write_csv(settings.path(settings.outputs_dir) / "image_manifest.csv", header, rows)
    write_xlsx(settings.path(settings.outputs_dir) / "image_manifest.xlsx", [("Images", header, rows)])
    # keep a copy next to the images, as documented in the folder layout
    write_csv(settings.path("data/image_manifest.csv"), header, (tuple(r) for r in conn.execute(
        "SELECT i.image_key, i.isbn13, i.image_id, b.title, i.source_url, i.source_page, i.local_path, i.sha256, "
        "i.width, i.height, i.format, i.bytes, i.downloaded_at, i.rights_note, i.analysis_status "
        "FROM images i JOIN books b ON b.isbn13=i.isbn13 ORDER BY i.isbn13, i.image_id")))
    return n


def export_all(conn: sqlite3.Connection, settings: Settings) -> dict:
    out = settings.path(settings.outputs_dir)
    out.mkdir(parents=True, exist_ok=True)
    n_manifest = write_image_manifest(conn, settings)
    version, mapping = current_mapping(conn)

    # image_findings.jsonl — one line per finding, with category if an approved taxonomy exists
    n_find = 0
    per_book: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    with (out / "image_findings.jsonl").open("w", encoding="utf-8") as fh:
        for f in iter_current_findings(conn):
            cat, sub = mapping.get(f["attribute"], (None, None))
            f.update({"category": cat, "subcategory": sub, "taxonomy_version": version})
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
            n_find += 1
            if cat:
                per_book[f["isbn"]][f"cat__{slug(cat)}"] += 1
                if sub:
                    per_book[f["isbn"]][f"sub__{slug(cat)}__{slug(sub)}"] += 1
            else:
                per_book[f["isbn"]]["uncategorized_findings"] += 1

    # book_image_features.csv — one row per ISBN, counts per approved (sub)category
    feature_cols = sorted({k for d in per_book.values() for k in d if k != "uncategorized_findings"})
    img_types = defaultdict(list)
    for isbn, t in conn.execute("SELECT isbn13, image_type FROM extractions WHERE is_current=1 ORDER BY isbn13, image_id"):
        if t:
            img_types[isbn].append(t)
    counts = {r[0]: r[1:] for r in conn.execute(
        "SELECT b.isbn13, COUNT(i.image_key), SUM(i.analysis_status IN ('extracted','needs_review')), "
        "SUM(i.analysis_status='needs_review') FROM books b LEFT JOIN images i ON i.isbn13=b.isbn13 GROUP BY b.isbn13")}
    header = ["isbn13", "asin", "title", "author", "publisher", "images_collected", "images_analyzed", "images_needing_review",
              "image_types", "taxonomy_version", "uncategorized_findings", *feature_cols]

    def gen():
        for r in conn.execute("SELECT isbn13, asin, title, author, publisher FROM books ORDER BY isbn13"):
            c = counts.get(r[0], (0, 0, 0))
            d = per_book.get(r[0], {})
            yield [r[0], r[1], r[2], r[3], r[4], c[0] or 0, c[1] or 0, c[2] or 0, ";".join(sorted(set(img_types.get(r[0], [])))),
                   version or "", d.get("uncategorized_findings", 0), *[d.get(k, 0) for k in feature_cols]]
    feat_rows = list(gen())
    n_books = write_csv(out / "book_image_features.csv", header, feat_rows)
    write_xlsx(out / "book_image_features.xlsx", [("Book features", header, feat_rows)])

    if version:
        tx = conn.execute("SELECT taxonomy_json FROM taxonomy_versions WHERE version=?", (version,)).fetchone()[0]
        write_json(out / "image_taxonomy.json", json.loads(tx))
    collection_report(conn, settings)
    from .relations import relations_report
    relations_report(conn, settings)
    report = {"generated_at": now(), **status(conn), "exported": {
        "image_manifest_rows": n_manifest, "image_findings_lines": n_find, "book_feature_rows": n_books,
        "feature_columns": len(feature_cols), "taxonomy_version": version}}
    write_json(out / "processing_report.json", report)
    log.info("export complete: %d images, %d findings, %d books, taxonomy v%s", n_manifest, n_find, n_books, version)
    return report["exported"]


STATUS_DE = {"found": "found - images saved", "no_images": "found - no usable images",
             "not_found": "not found on VLB-TIX", "download_failed": "found - download failed (retried next run)",
             "not_checked": "not checked yet"}


def collection_report(conn: sqlite3.Connection, settings: Settings) -> dict:
    """outputs/collection_report.xlsx (tabs: Summary, Books, Images) + collection_report.csv."""
    books = conn.execute(
        "SELECT b.isbn13, b.asin, b.author, b.publisher, b.publication_date, COALESCE(l.status, 'not_checked'), "
        "(SELECT COUNT(*) FROM images i WHERE i.isbn13=b.isbn13), "
        "(SELECT COUNT(*) FROM image_sources s WHERE s.isbn13=b.isbn13 AND s.status='failed'), "
        "(SELECT COUNT(*) FROM image_sources s WHERE s.isbn13=b.isbn13 AND s.status='rejected'), "
        "l.product_page, l.checked_at "
        "FROM books b LEFT JOIN vlbtix_lookups l ON l.isbn13=b.isbn13 ORDER BY b.isbn13").fetchall()
    book_header = ["EAN / ISBN-13", "ASIN", "Author", "Publisher", "Publication date", "VLB-TIX status",
                   "Images saved", "Images failed", "Images rejected", "VLB-TIX page", "Checked at (UTC)"]
    book_rows = [[r[0], r[1], r[2], r[3], r[4], STATUS_DE.get(r[5], r[5]), r[6], r[7], r[8], r[9],
                  (r[10] or "").replace("T", " ").split("+")[0]] for r in books]

    imgs = conn.execute(
        "SELECT i.isbn13, i.image_id, i.local_path, i.width, i.height, i.format, i.bytes, "
        "COALESCE(s.rights_note, ''), i.source_url, i.downloaded_at, i.analysis_status "
        "FROM images i LEFT JOIN image_sources s ON s.source_id=i.source_id ORDER BY i.isbn13, i.image_id").fetchall()
    img_header = ["EAN / ISBN-13", "Image ID", "File (in data\\images)", "Width", "Height", "Format", "Size (KB)",
                  "Found in section", "Source URL (no token)", "Downloaded at (UTC)", "ChatGPT analysis"]

    def section(note: str) -> str:
        return note.split("section '", 1)[1].split("'", 1)[0] if "section '" in note else ""
    img_rows = [[r[0], r[1], r[2].replace("/", "\\"), r[3], r[4], r[5], round((r[6] or 0) / 1024, 1),
                 section(r[7]), r[8], (r[9] or "").replace("T", " ").split("+")[0], r[10]] for r in imgs]
    fails = conn.execute("SELECT isbn13, url, status, last_error, updated_at FROM image_sources "
                         "WHERE status IN ('failed','rejected') ORDER BY isbn13").fetchall()
    fail_rows = [[r[0], r[1], r[2], r[3], (r[4] or "").replace("T", " ").split("+")[0]] for r in fails]

    by_status: dict[str, int] = {}
    for r in books:
        by_status[r[5]] = by_status.get(r[5], 0) + 1
    summary = [["Books in list", len(books)],
               *[[f"  {STATUS_DE.get(k, k)}", v] for k, v in sorted(by_status.items())],
               ["Books with at least one image", sum(1 for r in books if r[6])],
               ["Images saved (total)", len(imgs)],
               ["Image downloads failed / rejected", len(fails)],
               ["Report created (UTC)", now().replace("T", " ").split("+")[0]]]

    out = settings.path(settings.outputs_dir)
    xlsx = write_xlsx(out / "collection_report.xlsx", [("Summary", ["Metric", "Value"], summary),
                      ("Books", book_header, book_rows),
                      ("Images", img_header, img_rows),
                      ("Problems", ["EAN / ISBN-13", "Source URL", "Status", "Error", "When (UTC)"], fail_rows)],
               {"Metric": 40, "Value": 22, "VLB-TIX page": 50, "Source URL (no token)": 60, "Source URL": 60})
    write_csv(out / "collection_report.csv", book_header, book_rows)
    for stale in out.glob("*.tmp"):         # leftovers from a run that hit a locked file
        stale.unlink(missing_ok=True)
    return {"books": len(books), "by_vlbtix_status": by_status,
            "books_with_images": sum(1 for r in books if r[6]), "images_total": len(imgs),
            "excel_file": str(xlsx)}
