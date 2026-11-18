"""Module F — import JSON results produced manually in ChatGPT, validated against the batch manifest."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .config import Settings
from .db import now
from .isbn import to_isbn13
from .util import log, slug, strip_code_fences, write_json

EVIDENCE_TYPES = {"visible_text", "visual_observation", "interpretation"}
IMAGE_ID_RE = re.compile(r"^img_\d{3,6}$")


class ResultImportError(Exception):
    pass


def _norm_attr(a) -> str | None:
    if not isinstance(a, str) or not a.strip():
        return None
    return slug(a, 80)


def _parse(raw_text: str):
    try:
        return json.loads(strip_code_fences(raw_text))
    except json.JSONDecodeError as e:
        raise ResultImportError(f"invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from None


def import_results(conn: sqlite3.Connection, settings: Settings, path: Path,
                   batch_id: str | None = None, correction: bool = False) -> dict:
    raw_bytes = path.read_bytes()
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ResultImportError("file is not UTF-8. In Notepad use File > Save As > Encoding: UTF-8") from None
    digest = hashlib.sha256(raw_bytes).hexdigest()
    prev = conn.execute("SELECT import_id, imported_at FROM result_imports WHERE raw_sha256=?", (digest,)).fetchone()
    if prev:
        raise ResultImportError(f"this exact file was already imported (import_id {prev[0]} at {prev[1]})")

    data = _parse(raw_text)
    if isinstance(data, list):
        records, file_batch = data, None
    elif isinstance(data, dict) and isinstance(data.get("records"), list):
        records, file_batch = data["records"], data.get("batch_id")
    else:
        raise ResultImportError('expected {"batch_id": ..., "records": [...]} or a JSON list of records')

    m = re.search(r"batch_\d+", path.stem)
    batch_id = batch_id or file_batch or (m.group(0) if m else None)
    if not batch_id:
        raise ResultImportError("cannot determine batch: pass --batch batch_XXX")
    if file_batch and file_batch != batch_id:
        raise ResultImportError(f"file says batch_id={file_batch!r} but import targets {batch_id!r}")
    brow = conn.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not brow:
        raise ResultImportError(f"unknown batch {batch_id}")
    if brow[0] == "cancelled":
        raise ResultImportError(f"{batch_id} was cancelled (release-batch). If its ChatGPT answer is valid, run: "
                                f"python -m pipeline reopen-batch {batch_id} - then import again")
    manifest = {(r[0], r[1]) for r in conn.execute(
        "SELECT isbn13, image_id FROM batch_items WHERE batch_id=?", (batch_id,))}
    batch_isbns = {k[0] for k in manifest}

    # Preserve the original output untouched before anything else.
    ts = now()
    archive_dir = settings.path(settings.results_dir) / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{batch_id}__{ts.replace(':', '').replace('+', '_')}__{digest[:10]}{path.suffix or '.json'}"
    archive.write_bytes(raw_bytes)
    try:
        os.chmod(archive, 0o444)
    except OSError:
        pass

    cur = conn.execute(
        "INSERT INTO result_imports(batch_id, source_file, archive_path, raw_sha256, raw_text, is_correction, imported_at) "
        "VALUES (?,?,?,?,?,?,?)", (batch_id, path.name, str(archive.relative_to(settings.root)), digest, raw_text,
                                   int(correction), ts))
    import_id = cur.lastrowid
    issues: list[tuple] = []

    def issue(idx, isbn, image_id, severity, text):
        issues.append((import_id, idx, isbn, image_id, severity, text))

    seen: set[tuple[str, str]] = set()
    accepted = rejected = 0
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            issue(idx, None, None, "error", "record is not an object")
            rejected += 1
            continue
        isbn = to_isbn13(str(rec.get("isbn") or rec.get("isbn13") or ""))
        image_id = str(rec.get("image_id") or "").strip()
        if not isbn:
            issue(idx, rec.get("isbn"), image_id, "error", "missing or invalid ISBN")
            rejected += 1
            continue
        if not IMAGE_ID_RE.match(image_id):
            issue(idx, isbn, image_id, "error", "missing or malformed image_id")
            rejected += 1
            continue
        key = (isbn, image_id)
        if key not in manifest:
            why = ("image_id not in this batch for this ISBN (possible ISBN/image mix-up)"
                   if isbn in batch_isbns else "ISBN not part of this batch")
            issue(idx, isbn, image_id, "error", why)
            rejected += 1
            continue
        if key in seen:
            issue(idx, isbn, image_id, "error", "duplicate record for the same image in this file")
            rejected += 1
            continue
        seen.add(key)
        existing = conn.execute("SELECT extraction_id FROM extractions WHERE isbn13=? AND image_id=? AND is_current=1",
                                key).fetchone()
        if existing and not correction:
            issue(idx, isbn, image_id, "error", "already imported; re-run with --correction to supersede")
            rejected += 1
            continue

        needs_review = bool(rec.get("needs_review"))
        review_reason = rec.get("review_reason") if isinstance(rec.get("review_reason"), str) else None
        vt = rec.get("visible_text")
        if isinstance(vt, str):
            vt = [vt]
        if vt is not None and not (isinstance(vt, list) and all(isinstance(x, str) for x in vt)):
            issue(idx, isbn, image_id, "warning", "visible_text is not a list of strings; stored as-is")
        image_type = rec.get("image_type")
        image_type = slug(image_type, 60) if isinstance(image_type, str) and image_type.strip() else None

        clean_findings = []
        raw_findings = rec.get("findings") or []
        if not isinstance(raw_findings, list):
            issue(idx, isbn, image_id, "warning", "findings is not a list; ignored")
            raw_findings, needs_review = [], True
        for fi, f in enumerate(raw_findings):
            attr = _norm_attr(f.get("attribute")) if isinstance(f, dict) else None
            if not attr:
                issue(idx, isbn, image_id, "warning", f"finding {fi} has no attribute; dropped")
                needs_review = True
                continue
            et = f.get("evidence_type")
            et = et.strip().lower() if isinstance(et, str) else None
            if et not in EVIDENCE_TYPES:
                issue(idx, isbn, image_id, "warning", f"finding {fi}: unknown evidence_type {et!r}")
            if not f.get("evidence"):
                issue(idx, isbn, image_id, "warning", f"finding {fi} ({attr}) has no evidence")
            ev = f.get("evidence")
            clean_findings.append((attr, json.dumps(f.get("value"), ensure_ascii=False),
                                   ev if isinstance(ev, str) else (json.dumps(ev, ensure_ascii=False) if ev else None), et))

        if correction and existing:
            conn.execute("UPDATE extractions SET is_current=0 WHERE isbn13=? AND image_id=? AND is_current=1", key)
        ecur = conn.execute(
            "INSERT INTO extractions(import_id, batch_id, isbn13, image_id, image_type, visible_text, needs_review, "
            "review_reason, raw_record, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (import_id, batch_id, isbn, image_id, image_type, json.dumps(vt, ensure_ascii=False),
             int(needs_review), review_reason, json.dumps(rec, ensure_ascii=False), ts))
        conn.executemany(
            "INSERT INTO findings(extraction_id, isbn13, image_id, attribute, value, evidence, evidence_type) "
            "VALUES (?,?,?,?,?,?,?)", [(ecur.lastrowid, isbn, image_id, *cf) for cf in clean_findings])
        conn.execute("UPDATE images SET analysis_status=? WHERE isbn13=? AND image_id=?",
                     ("needs_review" if needs_review else "extracted", isbn, image_id))
        accepted += 1

    have = {(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT isbn13, image_id FROM extractions WHERE batch_id=? AND is_current=1", (batch_id,))}
    missing = sorted(manifest - have)
    for isbn, image_id in missing:
        issue(None, isbn, image_id, "missing", "no result for this image yet")
    conn.executemany("INSERT INTO import_issues(import_id, record_index, isbn13, image_id, severity, issue) "
                     "VALUES (?,?,?,?,?,?)", issues)
    conn.execute("UPDATE result_imports SET records_total=?, accepted=?, rejected=?, missing=? WHERE import_id=?",
                 (len(records), accepted, rejected, len(missing), import_id))
    conn.execute("UPDATE batches SET status=? WHERE batch_id=?",
                 ("imported" if not missing else "partially_imported", batch_id))
    conn.execute("UPDATE books SET status='analyzed', updated_at=? WHERE isbn13 IN (SELECT isbn13 FROM batch_items "
                 "WHERE batch_id=?) AND NOT EXISTS (SELECT 1 FROM images i WHERE i.isbn13=books.isbn13 AND "
                 "i.analysis_status IN ('pending','batched'))", (ts, batch_id))
    conn.commit()

    report = {"import_id": import_id, "batch_id": batch_id, "file": path.name, "archived_as": archive.name,
              "records_in_file": len(records), "accepted": accepted, "rejected": rejected,
              "missing": [f"{i}_{j}" for i, j in missing],
              "issues": [{"record": x[1], "isbn": x[2], "image_id": x[3], "severity": x[4], "issue": x[5]}
                         for x in issues]}
    from .batches import refresh_prompts
    try:
        updated = refresh_prompts(conn, settings)
        if updated:
            report["prompts_updated"] = updated
            log.info("updated prompts of %d waiting batch(es) with the latest attribute names", len(updated))
    except Exception as e:  # never fail an import because of this convenience step
        log.warning("could not update waiting prompts (%s)", type(e).__name__)
    write_json(settings.path(settings.results_dir) / "_reports" / f"import_{import_id:04d}_{batch_id}.json", report)
    log.info("import %d: accepted=%d rejected=%d missing=%d", import_id, accepted, rejected, len(missing))
    for x in issues:
        if x[4] == "error":
            log.warning("record %s (%s %s): %s", x[1], x[2], x[3], x[5])
    return report
