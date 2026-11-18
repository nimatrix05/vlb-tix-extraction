"""Module B — downloader for registered image sources, plus the shared ingest step
(validate -> hash -> dedupe -> store -> record) used by every source adapter."""
from __future__ import annotations

import os
import random
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings
from .db import now
from .images import InvalidImageError, validate_image
from .security import Resolver, UnsafeURLError, check_url, default_resolver
from .storage import LocalStorage, Storage
from .util import log

RETRY_STATUS = {429, 500, 502, 503, 504}
OK_CONTENT_TYPES = ("image/", "application/octet-stream", "binary/octet-stream")


class PermanentError(Exception):
    """Do not retry (4xx, unsafe URL, wrong content type, too large)."""


class TransientError(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class RateLimiter:
    """Minimum interval between requests to the same host, shared across worker threads."""

    def __init__(self, min_interval_s: float, sleep=time.sleep, clock=time.monotonic):
        self.min_interval = min_interval_s
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._sleep, self._clock = sleep, clock

    def wait(self, host: str) -> None:
        with self._lock:
            t = self._clock()
            start = max(t, self._next.get(host, 0.0))
            self._next[host] = start + self.min_interval
        if start > t:
            self._sleep(start - t)


@dataclass
class FetchResult:
    source_id: int
    tmp_path: Path | None
    error: str | None
    permanent: bool
    final_url: str | None = None


class Downloader:
    def __init__(self, settings: Settings, tmp_dir: Path, client: httpx.Client | None = None,
                 resolver: Resolver | None = default_resolver, sleep=time.sleep):
        self.s = settings
        self.tmp_dir = tmp_dir
        self.resolver = resolver
        self.sleep = sleep
        self.limiter = RateLimiter(settings.min_request_interval_s, sleep=sleep)
        self.client = client or httpx.Client(
            follow_redirects=False,  # every hop is validated manually
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            headers={"User-Agent": settings.user_agent, "Accept": "image/*"},
            trust_env=True,          # honour corporate proxy settings (HTTPS_PROXY)
        )

    def close(self):
        self.client.close()

    def _check(self, url: str) -> None:
        try:
            check_url(url, self.s.allowed_domains, self.s.allow_http, self.resolver)
        except UnsafeURLError as e:
            raise PermanentError(f"unsafe URL: {e}") from None

    def _once(self, url: str, dest: Path) -> str:
        """One attempt incl. redirects. Returns the final URL."""
        current = url
        for _hop in range(self.s.max_redirects + 1):
            self._check(current)
            self.limiter.wait(urlsplit(current).hostname or "")
            try:
                with self.client.stream("GET", current) as r:
                    if r.is_redirect:
                        loc = r.headers.get("location")
                        if not loc:
                            raise PermanentError("redirect without Location")
                        current = urljoin(current, loc)
                        continue
                    if r.status_code in RETRY_STATUS:
                        ra = r.headers.get("retry-after")
                        raise TransientError(f"HTTP {r.status_code}",
                                             float(ra) if ra and ra.isdigit() else None)
                    if r.status_code != 200:
                        raise PermanentError(f"HTTP {r.status_code}")
                    ctype = r.headers.get("content-type", "").lower()
                    if ctype and not ctype.startswith(OK_CONTENT_TYPES):
                        raise PermanentError(f"unexpected content-type {ctype[:60]}")
                    clen = r.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > self.s.max_download_bytes:
                        raise PermanentError(f"too large ({clen} bytes)")
                    total = 0
                    with dest.open("wb") as fh:
                        for chunk in r.iter_bytes(65536):
                            total += len(chunk)
                            if total > self.s.max_download_bytes:
                                raise PermanentError("download exceeded size limit")
                            fh.write(chunk)
                    return current
            except httpx.TransportError as e:
                raise TransientError(f"{type(e).__name__}: {e}") from None
        raise PermanentError(f"too many redirects (> {self.s.max_redirects})")

    def fetch(self, source_id: int, url: str) -> FetchResult:
        dest = self.tmp_dir / f"{uuid.uuid4().hex}.part"
        last = "unknown error"
        for attempt in range(self.s.max_retries + 1):
            try:
                final = self._once(url, dest)
                return FetchResult(source_id, dest, None, False, final)
            except PermanentError as e:
                dest.unlink(missing_ok=True)
                return FetchResult(source_id, None, str(e), True)
            except TransientError as e:
                dest.unlink(missing_ok=True)
                last = str(e)
                if attempt < self.s.max_retries:
                    delay = e.retry_after if e.retry_after is not None else \
                        self.s.backoff_base_s * (2 ** attempt) + random.uniform(0, 0.5)
                    delay = min(delay, self.s.max_retry_after_s)
                    log.info("retry %d for source %d in %.1fs (%s)", attempt + 1, source_id, delay, last)
                    self.sleep(delay)
        return FetchResult(source_id, None, f"gave up after retries: {last}", False)


# ---------------------------------------------------------------- shared ingest step
def next_image_id(conn: sqlite3.Connection, isbn13: str) -> str:
    row = conn.execute("SELECT MAX(CAST(SUBSTR(image_id, 5) AS INTEGER)) FROM images WHERE isbn13=?",
                       (isbn13,)).fetchone()
    return f"img_{(row[0] or 0) + 1:03d}"


def ingest_file(conn: sqlite3.Connection, settings: Settings, storage: Storage, tmp_file: Path,
                source: sqlite3.Row) -> tuple[str, str]:
    """Validate and store a downloaded/local file for a source row. Consumes tmp_file.
    Returns (status, detail) where status is downloaded | duplicate | rejected."""
    ts = now()
    try:
        info = validate_image(tmp_file, settings)
    except InvalidImageError as e:
        tmp_file.unlink(missing_ok=True)
        conn.execute("UPDATE image_sources SET status='rejected', last_error=?, attempts=attempts+1, updated_at=? "
                     "WHERE source_id=?", (f"invalid image: {e}", ts, source["source_id"]))
        return "rejected", str(e)
    isbn = source["isbn13"]
    dup = conn.execute("SELECT image_id FROM images WHERE isbn13=? AND sha256=?", (isbn, info.sha256)).fetchone()
    if dup:
        tmp_file.unlink(missing_ok=True)
        conn.execute("UPDATE image_sources SET status='duplicate', image_id=?, attempts=attempts+1, last_error=NULL, "
                     "updated_at=? WHERE source_id=?", (dup[0], ts, source["source_id"]))
        return "duplicate", dup[0]
    other = conn.execute("SELECT image_key FROM images WHERE sha256=? LIMIT 1", (info.sha256,)).fetchone()
    if other:
        log.info("image for %s is identical to %s (shared asset); stored for both", isbn, other[0])
    image_id = next_image_id(conn, isbn)
    key = storage.put(tmp_file, isbn, f"{image_id}{info.ext}")
    conn.execute(
        "INSERT INTO images(image_key, isbn13, image_id, source_id, source_url, source_page, local_path, sha256, "
        "width, height, format, bytes, downloaded_at, rights_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{isbn}_{image_id}", isbn, image_id, source["source_id"], source["url"], source["source_page"], key,
         info.sha256, info.width, info.height, info.format, info.bytes, ts, source["rights_note"]))
    conn.execute("UPDATE image_sources SET status='downloaded', image_id=?, attempts=attempts+1, last_error=NULL, "
                 "updated_at=? WHERE source_id=?", (image_id, ts, source["source_id"]))
    conn.execute("UPDATE books SET status='images_collected', updated_at=? WHERE isbn13=? AND status='new'", (ts, isbn))
    return "downloaded", image_id


def _tmp_dir(settings: Settings) -> Path:
    d = settings.path("data/tmp")
    d.mkdir(parents=True, exist_ok=True)
    for stale in d.glob("*.part"):  # partial files from an interrupted run
        stale.unlink(missing_ok=True)
    return d


def collect(conn: sqlite3.Connection, settings: Settings, limit_books: int | None = None,
            dry_run: bool = False, concurrency: int | None = None, client: httpx.Client | None = None,
            resolver: Resolver | None = default_resolver, sleep=time.sleep) -> dict:
    """Download pending URL sources for up to `limit_books` books. Safe to interrupt and re-run."""
    # VLB-TIX images are downloaded during discover-vlbtix (same login session); collect handles URL lists.
    q = ("SELECT * FROM image_sources WHERE status='pending' AND method='url_manifest' "
         "AND isbn13 IN (SELECT DISTINCT isbn13 FROM image_sources WHERE status='pending' "
         "AND method='url_manifest' ORDER BY isbn13 {lim}) ORDER BY isbn13, source_id")
    rows = conn.execute(q.format(lim="LIMIT ?" if limit_books else ""),
                        (limit_books,) if limit_books else ()).fetchall()
    rep = {"sources": len(rows), "downloaded": 0, "duplicate": 0, "rejected": 0, "failed": 0, "unsafe": 0}
    if dry_run:
        for r in rows:
            try:
                check_url(r["url"], settings.allowed_domains, settings.allow_http, resolver)
                log.info("[dry-run] would fetch %s -> %s", r["url"], r["isbn13"])
            except UnsafeURLError as e:
                rep["unsafe"] += 1
                log.warning("[dry-run] would reject %s: %s", r["url"], e)
        return rep
    if not rows:
        return rep
    storage = LocalStorage(settings.path(settings.images_dir))
    dl = Downloader(settings, _tmp_dir(settings), client=client, resolver=resolver, sleep=sleep)
    by_id = {r["source_id"]: r for r in rows}
    workers = concurrency or settings.concurrency
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(dl.fetch, r["source_id"], r["url"]) for r in rows]
            for i, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                src = by_id[res.source_id]
                if res.tmp_path is None:  # DB writes happen only on this (main) thread
                    status = "rejected" if res.permanent else "failed"
                    if res.error and res.error.startswith("unsafe URL"):
                        rep["unsafe"] += 1
                    conn.execute("UPDATE image_sources SET status=?, last_error=?, attempts=attempts+1, updated_at=? "
                                 "WHERE source_id=?", (status, res.error, now(), res.source_id))
                    rep[status] += 1
                    log.warning("%s %s: %s", status.upper(), src["url"], res.error)
                else:
                    status, detail = ingest_file(conn, settings, storage, res.tmp_path, src)
                    rep[status] += 1
                    log.debug("%s %s -> %s", status, src["url"], detail)
                conn.commit()
                if i % 25 == 0 or i == len(rows):
                    log.info("progress: %d/%d sources processed", i, len(rows))
    finally:
        if client is None:
            dl.close()
    return rep


def import_local_images(conn: sqlite3.Connection, settings: Settings, csv_path: Path, dry_run: bool = False) -> dict:
    """Ingest image files delivered by an authorized party (e.g. a VLB/publisher export).
    CSV columns: isbn13, file_path (relative to the CSV folder or absolute), rights_note, source_page."""
    from .isbn import to_isbn13
    from .util import read_csv_rows
    rep = {"rows": 0, "downloaded": 0, "duplicate": 0, "rejected": 0, "already_known": 0, "errors": 0}
    known = {r[0] for r in conn.execute("SELECT isbn13 FROM books")}
    storage = LocalStorage(settings.path(settings.images_dir))
    tmp = _tmp_dir(settings)
    base = csv_path.parent.resolve()
    for line_no, row in enumerate(read_csv_rows(csv_path), start=2):
        rep["rows"] += 1
        isbn = to_isbn13(row.get("isbn13") or row.get("isbn"))
        fp = row.get("file_path") or row.get("path") or ""
        src_path = (base / fp).resolve() if not Path(fp).is_absolute() else Path(fp).resolve()
        if not isbn or isbn not in known or not fp or not src_path.is_file():
            rep["errors"] += 1
            log.warning("line %d: skipped (isbn=%r known=%s file_exists=%s)", line_no, isbn,
                        isbn in known, src_path.is_file() if fp else False)
            continue
        ref = "file:" + (src_path.relative_to(base).as_posix() if base in src_path.parents else src_path.name)
        if dry_run:
            log.info("[dry-run] would ingest %s for %s", ref, isbn)
            continue
        ts = now()
        cur = conn.execute(
            "INSERT OR IGNORE INTO image_sources(isbn13, url, source_page, method, rights_note, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (isbn, ref, row.get("source_page") or None, "local_import",
                                       row.get("rights_note") or None, ts, ts))
        if not cur.rowcount:
            rep["already_known"] += 1
            continue
        src = conn.execute("SELECT * FROM image_sources WHERE isbn13=? AND url=?", (isbn, ref)).fetchone()
        t = tmp / f"{uuid.uuid4().hex}.part"
        shutil.copyfile(src_path, t)
        status, _ = ingest_file(conn, settings, storage, t, src)
        rep[status] += 1
        conn.commit()
    return rep


def reset_failed(conn: sqlite3.Connection, include_rejected: bool = False) -> int:
    statuses = ("failed", "rejected") if include_rejected else ("failed",)
    cur = conn.execute(f"UPDATE image_sources SET status='pending', updated_at=? WHERE status IN "
                       f"({','.join('?'*len(statuses))}) AND method = 'url_manifest'", (now(), *statuses))
    conn.commit()
    return cur.rowcount


def prune_icon_images(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> dict:
    """Remove already-saved VLB-TIX images that were found in social-media/link-icon sections
    (e.g. 'Instagram', 'TikTok'). Files are MOVED to data/_removed/<EAN>/ (not deleted) and the
    database rows are dropped, so they never go to ChatGPT. Images already analyzed are left alone."""
    names = {x.lower() for x in settings.vlbtix_skip_icon_names}
    rows = conn.execute(
        "SELECT i.image_key, i.isbn13, i.image_id, i.local_path, i.source_id, s.rights_note FROM images i "
        "JOIN image_sources s ON s.source_id=i.source_id WHERE s.method='vlbtix_playwright' "
        "AND i.analysis_status='pending'").fetchall()
    hits = []
    for r in rows:
        note = r["rights_note"] or ""
        sec = note.split("section '", 1)[1].split("'", 1)[0].strip().lower() if "section '" in note else ""
        if sec in names:
            hits.append(r)
    if dry_run:
        return {"would_remove": [f"{r['isbn13']}/{r['image_id']}" for r in hits]}
    base = settings.path(settings.images_dir)
    removed_dir = settings.path("data/_removed")
    for r in hits:
        src = base / r["local_path"]
        if src.exists():
            dest = removed_dir / r["isbn13"]
            dest.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest / src.name)
        conn.execute("DELETE FROM images WHERE image_key=?", (r["image_key"],))
        conn.execute("UPDATE image_sources SET status='rejected', image_id=NULL, last_error='social-media icon', "
                     "updated_at=? WHERE source_id=?", (now(), r["source_id"]))
    conn.commit()
    return {"removed": [f"{r['isbn13']}/{r['image_id']}" for r in hits], "moved_to": str(removed_dir)}


def remove_images(conn: sqlite3.Connection, settings: Settings, isbn13: str, image_ids: list[str]) -> dict:
    """Take specific images out of the pipeline (e.g. saved by mistake). Files are MOVED to
    data/_removed/<EAN>/, DB rows dropped; the source is marked rejected so it is not downloaded again.
    Images that were already sent to ChatGPT are refused (their results would lose their image)."""
    removed, refused = [], []
    base, dest_dir = settings.path(settings.images_dir), settings.path("data/_removed") / isbn13
    for image_id in image_ids:
        r = conn.execute("SELECT * FROM images WHERE isbn13=? AND image_id=?", (isbn13, image_id)).fetchone()
        if not r:
            refused.append(f"{image_id}: not found")
            continue
        if r["analysis_status"] != "pending":
            refused.append(f"{image_id}: already {r['analysis_status']}")
            continue
        src = base / r["local_path"]
        if src.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest_dir / src.name)
        conn.execute("DELETE FROM images WHERE image_key=?", (r["image_key"],))
        conn.execute("UPDATE image_sources SET status='rejected', image_id=NULL, last_error='removed by user', "
                     "updated_at=? WHERE source_id=?", (now(), r["source_id"]))
        removed.append(image_id)
    conn.commit()
    return {"removed": removed, "refused": refused}


def prune_card_thumbnails(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> dict:
    """Remove saved VLB-TIX images that are card thumbnails of other listings (address ends in /m):
    preview catalogues, magazines, award lists, related books. Only images not yet analysed by ChatGPT.
    Files are MOVED to data/_removed/<EAN>/. Images in a prepared batch: run release-batch first."""
    from .sources.vlbtix_playwright import is_card_thumbnail
    rows = conn.execute(
        "SELECT i.image_key, i.isbn13, i.image_id, i.local_path, i.source_id, i.analysis_status, s.url "
        "FROM images i JOIN image_sources s ON s.source_id=i.source_id WHERE s.method='vlbtix_playwright'").fetchall()
    hits = [r for r in rows if is_card_thumbnail(r["url"], r["isbn13"])]
    blocked = [f"{r['isbn13']}/{r['image_id']}" for r in hits if r["analysis_status"] == "batched"]
    done = [f"{r['isbn13']}/{r['image_id']}" for r in hits if r["analysis_status"] in ("extracted", "needs_review")]
    todo = [r for r in hits if r["analysis_status"] == "pending"]
    if dry_run:
        return {"would_remove": len(todo), "in_prepared_batch": blocked, "already_analysed_kept": done}
    base, removed = settings.path(settings.images_dir), settings.path("data/_removed")
    for r in todo:
        src = base / r["local_path"]
        if src.exists():
            (removed / r["isbn13"]).mkdir(parents=True, exist_ok=True)
            os.replace(src, removed / r["isbn13"] / src.name)
        conn.execute("DELETE FROM images WHERE image_key=?", (r["image_key"],))
        conn.execute("UPDATE image_sources SET status='rejected', image_id=NULL, last_error='listing thumbnail', "
                     "updated_at=? WHERE source_id=?", (now(), r["source_id"]))
    conn.commit()
    out = {"removed": len(todo), "moved_to": str(removed)}
    if blocked:
        out["not_removed_in_prepared_batch"] = blocked
        out["hint"] = "run release-batch for that batch, then prune-card-images again"
    if done:
        out["already_analysed_kept"] = done
    return out
