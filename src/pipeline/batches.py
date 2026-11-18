"""Module D — prepare image batches for MANUAL upload to ChatGPT (no automation of the account)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Settings
from .db import now
from .images import make_upload_copy
from .security import contained_path
from .storage import LocalStorage
from .util import log, write_csv

UPLOAD_HOWTO = """HOW TO PROCESS {batch_id} IN CHATGPT (manual step)
=================================================
Before you start: only upload images if IT/legal has approved sending these images to ChatGPT,
and use the company-approved ChatGPT workspace (with model training on your data disabled).

1. Open a NEW chat in ChatGPT (one chat per batch keeps results clean).
2. Open extraction_prompt.txt in this folder, copy ALL of its text, and paste it into the chat.
3. Attach every file from the images\\ subfolder ({n_images} files). Do not rename them.
   If ChatGPT refuses that many files, split into several messages and say
   "more images follow, wait" until the last message.
4. Send. ChatGPT must answer with ONE JSON object (batch_id + records).
5. Copy the JSON answer into a new text file:
       chatgpt_results\\{batch_id}.json
   (In Notepad: File > Save As > Encoding UTF-8.)
6. Import it:
       python -m pipeline import-results chatgpt_results\\{batch_id}.json
7. Check the report. Missing or rejected records can be re-done in ChatGPT and imported with:
       python -m pipeline import-results chatgpt_results\\{batch_id}_fix.json --batch {batch_id} --correction
"""


def build_prompt(conn: sqlite3.Connection, settings: Settings, batch_id: str,
                 items: list[tuple[str, str, str]]) -> str:
    """items = (upload_filename, isbn13, image_id). Includes the attribute names used so far."""
    template = settings.path("prompts/image_extraction.txt").read_text(encoding="utf-8")
    mapping = "\n".join(f"- {f}  ->  isbn: {i}, image_id: {m}" for f, i, m in items)
    known = conn.execute("SELECT f.attribute, COUNT(*) n FROM findings f JOIN extractions e ON "
                         "e.extraction_id=f.extraction_id AND e.is_current=1 GROUP BY 1 ORDER BY n DESC, 1 "
                         "LIMIT 150").fetchall()
    known_txt = ", ".join(r[0] for r in known) if known else "(none yet - this is the first batch)"
    return template.replace("{{BATCH_ID}}", batch_id).replace("{{IMAGE_COUNT}}", str(len(items))) \
                   .replace("{{IMAGE_MAPPING}}", mapping).replace("{{KNOWN_ATTRIBUTES}}", known_txt)


def refresh_prompts(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """Rewrite extraction_prompt.txt of every batch not sent yet ('prepared'), so it lists the
    attribute names ChatGPT has used up to now. Called automatically after each import."""
    done = []
    for (batch_id, folder) in conn.execute("SELECT batch_id, folder FROM batches WHERE status='prepared' "
                                           "ORDER BY batch_id").fetchall():
        items = conn.execute("SELECT upload_filename, isbn13, image_id FROM batch_items WHERE batch_id=? "
                             "ORDER BY upload_filename", (batch_id,)).fetchall()
        path = settings.root / folder / "extraction_prompt.txt"
        if items and path.parent.exists():
            path.write_text(build_prompt(conn, settings, batch_id, [tuple(r) for r in items]), encoding="utf-8")
            done.append(batch_id)
    return done


def _next_batch_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(CAST(SUBSTR(batch_id, 7) AS INTEGER)) FROM batches").fetchone()
    return f"batch_{(row[0] or 0) + 1:03d}"


_IN_WORK = ("batched", "extracted", "needs_review")


def prepare_batch(conn: sqlite3.Connection, settings: Settings, books: int = 10,
                  max_images: int | None = None, dry_run: bool = False,
                  only_isbns: list[str] | None = None, per_book: int | None = None) -> dict:
    """Copy the next waiting images into a new batch folder.
    only_isbns: restrict to these books (e.g. the discovery sample).
    per_book:   at most this many images per book in total (already batched/analysed ones count),
                lowest image numbers first - img_001 is normally the main cover."""
    cap = max_images or settings.batch_max_images
    sql = "SELECT isbn13, COUNT(*) AS n FROM images WHERE analysis_status='pending' "
    params: list = []
    if only_isbns is not None:
        if not only_isbns:
            return {"batch_id": None, "images": 0, "books": 0, "message": "no books in the selection"}
        sql += f"AND isbn13 IN ({','.join('?' * len(only_isbns))}) "
        params += list(only_isbns)
    candidates = conn.execute(sql + "GROUP BY isbn13 ORDER BY isbn13", params).fetchall()
    selected: list[sqlite3.Row] = []
    used_books = 0
    for c in candidates:
        if used_books >= books:
            break
        imgs = conn.execute("SELECT * FROM images WHERE isbn13=? AND analysis_status='pending' ORDER BY image_id",
                            (c["isbn13"],)).fetchall()
        if per_book is not None:
            already = conn.execute(
                f"SELECT COUNT(*) FROM images WHERE isbn13=? AND analysis_status IN ({','.join('?' * len(_IN_WORK))})",
                (c["isbn13"], *_IN_WORK)).fetchone()[0]
            imgs = imgs[:max(0, per_book - already)]
            if not imgs:
                continue
        used_books += 1
        room = cap - len(selected)
        if room <= 0:
            break
        if len(imgs) > room and selected:
            break  # keep a book's images together where possible
        selected.extend(imgs[:room])
    if not selected:
        return {"batch_id": None, "images": 0, "books": 0, "message": "no un-analyzed images available"
                + (" for this selection" if only_isbns is not None or per_book is not None else "")}

    batch_id = _next_batch_id(conn)
    n_books = len({r["isbn13"] for r in selected})
    if dry_run:
        return {"batch_id": batch_id, "images": len(selected), "books": n_books, "dry_run": True}

    bdir = contained_path(settings.path(settings.batches_dir), batch_id)
    img_dir = bdir / "images"
    img_dir.mkdir(parents=True, exist_ok=False)
    storage = LocalStorage(settings.path(settings.images_dir))
    manifest_rows = []
    for r in selected:
        stem = f"{r['isbn13']}_{r['image_id']}"
        ext = Path(r["local_path"]).suffix
        out = make_upload_copy(storage.local_path(r["local_path"]), img_dir / f"{stem}{ext}", settings.upload_max_edge)
        manifest_rows.append((batch_id, out.name, r["isbn13"], r["image_id"], r["image_key"], r["sha256"],
                              r["source_url"], r["width"], r["height"]))

    write_csv(bdir / "manifest.csv",
              ["batch_id", "upload_filename", "isbn13", "image_id", "image_key", "sha256", "source_url", "width", "height"],
              manifest_rows)
    prompt = build_prompt(conn, settings, batch_id, [(m[1], m[2], m[3]) for m in manifest_rows])
    (bdir / "extraction_prompt.txt").write_text(prompt, encoding="utf-8")
    (bdir / "UPLOAD_INSTRUCTIONS.txt").write_text(
        UPLOAD_HOWTO.format(batch_id=batch_id, n_images=len(manifest_rows)), encoding="utf-8")

    ts = now()
    conn.execute("INSERT INTO batches(batch_id, created_at, status, book_count, image_count, folder) VALUES (?,?,?,?,?,?)",
                 (batch_id, ts, "prepared", n_books, len(manifest_rows), str(bdir.relative_to(settings.root))))
    conn.executemany("INSERT INTO batch_items(batch_id, isbn13, image_id, upload_filename) VALUES (?,?,?,?)",
                     [(batch_id, m[2], m[3], m[1]) for m in manifest_rows])
    conn.executemany("UPDATE images SET analysis_status='batched' WHERE isbn13=? AND image_id=?",
                     [(m[2], m[3]) for m in manifest_rows])
    conn.execute("UPDATE books SET status='batched', updated_at=? WHERE isbn13 IN "
                 "(SELECT isbn13 FROM batch_items WHERE batch_id=?)", (ts, batch_id))
    conn.commit()
    log.info("Prepared %s: %d images from %d books in %s", batch_id, len(manifest_rows), n_books, bdir)
    return {"batch_id": batch_id, "images": len(manifest_rows), "books": n_books, "folder": str(bdir)}


def release_batch(conn: sqlite3.Connection, batch_id: str) -> int:
    """Cancel a batch that was not (fully) analyzed: its un-analyzed images go back to the pending pool
    and the batch is marked 'cancelled', so its folder can't be imported by mistake later."""
    if not conn.execute("SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)).fetchone():
        raise ValueError(f"unknown batch {batch_id}")
    cur = conn.execute("UPDATE images SET analysis_status='pending' WHERE analysis_status='batched' AND "
                       "(isbn13, image_id) IN (SELECT isbn13, image_id FROM batch_items WHERE batch_id=?)", (batch_id,))
    conn.execute("UPDATE batches SET status='cancelled' WHERE batch_id=? AND status='prepared'", (batch_id,))
    conn.commit()
    log.info("%s cancelled; %d image(s) back in the pool. You can delete its folder.", batch_id, cur.rowcount)
    return cur.rowcount


def reopen_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    """Undo release-batch: mark a cancelled batch as prepared again so its ChatGPT answer can be imported.
    Images of the batch that still exist and are not analysed elsewhere go back to 'batched';
    images removed in the meantime (e.g. listing covers) simply count as missing at import."""
    row = conn.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown batch {batch_id}")
    if row[0] != "cancelled":
        return {"batch_id": batch_id, "status": row[0], "changed": False}
    items = conn.execute("SELECT b.isbn13, b.image_id, i.analysis_status FROM batch_items b LEFT JOIN images i "
                         "ON i.isbn13=b.isbn13 AND i.image_id=b.image_id WHERE b.batch_id=?", (batch_id,)).fetchall()
    back = [(r[0], r[1]) for r in items if r[2] == "pending"]
    gone = [f"{r[0]}_{r[1]}" for r in items if r[2] is None]
    conn.executemany("UPDATE images SET analysis_status='batched' WHERE isbn13=? AND image_id=?", back)
    conn.execute("UPDATE batches SET status='prepared' WHERE batch_id=?", (batch_id,))
    conn.commit()
    log.info("%s reopened: %d image(s) back in the batch, %d removed meanwhile", batch_id, len(back), len(gone))
    return {"batch_id": batch_id, "status": "prepared", "images_back_in_batch": len(back), "removed_meanwhile": gone}


def rebuild_batch(conn: sqlite3.Connection, settings: Settings, batch_id: str) -> dict:
    """Recreate a batch folder (images, manifest, prompt, instructions) from the database, e.g. after the
    folder was deleted by mistake. Same batch ID, same images; the prompt gets the latest attribute names."""
    b = conn.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not b:
        raise ValueError(f"unknown batch {batch_id}")
    if b[0] == "cancelled":
        raise ValueError(f"{batch_id} is cancelled; run reopen-batch {batch_id} first if you still need it")
    rows = conn.execute("SELECT i.* FROM batch_items bi JOIN images i ON i.isbn13=bi.isbn13 AND i.image_id=bi.image_id "
                        "WHERE bi.batch_id=? ORDER BY i.isbn13, i.image_id", (batch_id,)).fetchall()
    if not rows:
        raise ValueError(f"{batch_id} has no images left (they may have been removed)")
    bdir = contained_path(settings.path(settings.batches_dir), batch_id)
    img_dir = bdir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage(settings.path(settings.images_dir))
    manifest_rows = []
    for r in rows:
        stem = f"{r['isbn13']}_{r['image_id']}"
        out = make_upload_copy(storage.local_path(r["local_path"]), img_dir / f"{stem}{Path(r['local_path']).suffix}",
                               settings.upload_max_edge)
        manifest_rows.append((batch_id, out.name, r["isbn13"], r["image_id"], r["image_key"], r["sha256"],
                              r["source_url"], r["width"], r["height"]))
        conn.execute("UPDATE batch_items SET upload_filename=? WHERE batch_id=? AND isbn13=? AND image_id=?",
                     (out.name, batch_id, r["isbn13"], r["image_id"]))
    write_csv(bdir / "manifest.csv",
              ["batch_id", "upload_filename", "isbn13", "image_id", "image_key", "sha256", "source_url", "width", "height"],
              manifest_rows)
    (bdir / "extraction_prompt.txt").write_text(
        build_prompt(conn, settings, batch_id, [(m[1], m[2], m[3]) for m in manifest_rows]), encoding="utf-8")
    (bdir / "UPLOAD_INSTRUCTIONS.txt").write_text(
        UPLOAD_HOWTO.format(batch_id=batch_id, n_images=len(manifest_rows)), encoding="utf-8")
    conn.commit()
    log.info("rebuilt %s: %d images in %s", batch_id, len(manifest_rows), bdir)
    return {"batch_id": batch_id, "images": len(manifest_rows), "books": len({m[2] for m in manifest_rows}),
            "folder": str(bdir), "status": b[0]}
