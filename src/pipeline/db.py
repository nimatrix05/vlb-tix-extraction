"""SQLite schema and connection helpers.

All SQL lives behind this module and the stage modules; a later move to PostgreSQL
means replacing connect() and the few SQLite-specific statements (INSERT OR IGNORE, PRAGMA).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS books (
    book_id      TEXT PRIMARY KEY,           -- stable: 'bk_' + isbn13
    isbn13       TEXT NOT NULL UNIQUE,
    title        TEXT,
    publisher    TEXT,
    source_ref   TEXT,                       -- VLB-TIX URL or approved source reference
    asin         TEXT,
    author       TEXT,
    publication_date TEXT,
    status       TEXT NOT NULL DEFAULT 'new', -- new | images_collected | batched | analyzed
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_sources (      -- one row per candidate image (URL or local file)
    source_id     INTEGER PRIMARY KEY,
    isbn13        TEXT NOT NULL REFERENCES books(isbn13),
    url           TEXT NOT NULL,                -- https URL or 'file:' reference for local imports
    source_page   TEXT,
    method        TEXT NOT NULL,                -- url_manifest | local_import | vlbtix_playwright
    rights_note   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | downloaded | duplicate | failed | rejected
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    image_id      TEXT,
    discovered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (isbn13, url)
);
CREATE INDEX IF NOT EXISTS ix_sources_status ON image_sources(status);

CREATE TABLE IF NOT EXISTS images (
    image_key       TEXT PRIMARY KEY,           -- '<isbn13>_img_001' (globally unique)
    isbn13          TEXT NOT NULL REFERENCES books(isbn13),
    image_id        TEXT NOT NULL,              -- 'img_001' (unique per ISBN)
    source_id       INTEGER REFERENCES image_sources(source_id),
    source_url      TEXT NOT NULL,
    source_page     TEXT,
    local_path      TEXT NOT NULL,              -- storage key, relative to images_dir
    sha256          TEXT NOT NULL,
    width           INTEGER, height INTEGER,
    format          TEXT, bytes INTEGER,
    downloaded_at   TEXT NOT NULL,
    rights_note     TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'pending', -- pending | batched | extracted | needs_review
    UNIQUE (isbn13, image_id),
    UNIQUE (isbn13, sha256)
);
CREATE INDEX IF NOT EXISTS ix_images_sha ON images(sha256);

CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,              -- prepared | partially_imported | imported
    book_count  INTEGER, image_count INTEGER,
    folder      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    isbn13          TEXT NOT NULL,
    image_id        TEXT NOT NULL,
    upload_filename TEXT NOT NULL,
    PRIMARY KEY (batch_id, isbn13, image_id)
);

CREATE TABLE IF NOT EXISTS result_imports (
    import_id     INTEGER PRIMARY KEY,
    batch_id      TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    archive_path  TEXT NOT NULL,            -- untouched copy of what ChatGPT returned
    raw_sha256    TEXT NOT NULL UNIQUE,
    raw_text      TEXT NOT NULL,
    is_correction INTEGER NOT NULL DEFAULT 0,
    imported_at   TEXT NOT NULL,
    records_total INTEGER, accepted INTEGER, rejected INTEGER, missing INTEGER
);
CREATE TABLE IF NOT EXISTS import_issues (
    import_id INTEGER NOT NULL, record_index INTEGER,
    isbn13 TEXT, image_id TEXT, severity TEXT NOT NULL, issue TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
    extraction_id INTEGER PRIMARY KEY,
    import_id     INTEGER NOT NULL REFERENCES result_imports(import_id),
    batch_id      TEXT NOT NULL,
    isbn13        TEXT NOT NULL,
    image_id      TEXT NOT NULL,
    image_type    TEXT,
    visible_text  TEXT,                     -- JSON array
    needs_review  INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT,
    raw_record    TEXT NOT NULL,            -- JSON of the record as received
    is_current    INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_extr_current ON extractions(isbn13, image_id, is_current);
CREATE TABLE IF NOT EXISTS findings (
    finding_id    INTEGER PRIMARY KEY,
    extraction_id INTEGER NOT NULL REFERENCES extractions(extraction_id),
    isbn13        TEXT NOT NULL,
    image_id      TEXT NOT NULL,
    attribute     TEXT NOT NULL,
    value         TEXT,                     -- JSON-encoded scalar/list
    evidence      TEXT,
    evidence_type TEXT
);
CREATE INDEX IF NOT EXISTS ix_findings_attr ON findings(attribute);

CREATE TABLE IF NOT EXISTS taxonomy_proposals (
    proposal_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    raw_json    TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS taxonomy_versions (
    version      INTEGER PRIMARY KEY,
    proposal_id  INTEGER REFERENCES taxonomy_proposals(proposal_id),
    review_file  TEXT,
    created_at   TEXT NOT NULL,
    taxonomy_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS category_reviews (
    version        INTEGER NOT NULL,
    proposed_path  TEXT,                    -- 'Category > Subcategory' as proposed (NULL if custom)
    decision       TEXT NOT NULL,           -- approve | rename | merge | reject | add
    approved_path  TEXT,
    notes          TEXT
);
CREATE TABLE IF NOT EXISTS attribute_mappings (
    version      INTEGER NOT NULL,
    attribute    TEXT NOT NULL,
    category     TEXT NOT NULL,
    subcategory  TEXT,
    PRIMARY KEY (version, attribute)
);

CREATE TABLE IF NOT EXISTS feature_runs (      -- one row per build-features run (snapshot)
    run_id             INTEGER PRIMARY KEY,
    dictionary_version TEXT NOT NULL,
    computed_at        TEXT NOT NULL,
    books              INTEGER, features INTEGER
);
CREATE TABLE IF NOT EXISTS book_features (     -- feature values per book and run, with timestamps
    run_id          INTEGER NOT NULL REFERENCES feature_runs(run_id),
    isbn13          TEXT NOT NULL,
    feature         TEXT NOT NULL,
    value           TEXT,                        -- JSON
    has_value       INTEGER NOT NULL,
    evidence_images TEXT,                        -- image_ids that support the value
    first_seen_at   TEXT,                        -- earliest capture (download) of an evidence image
    observed_at     TEXT,                        -- latest capture of any image of the book
    extracted_at    TEXT,                        -- earliest LLM extraction of an evidence image
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, isbn13, feature)
);
CREATE INDEX IF NOT EXISTS ix_book_features_book ON book_features(isbn13, feature);

CREATE TABLE IF NOT EXISTS sample_books (      -- stratified discovery sample (sample-books)
    sample_name  TEXT NOT NULL,
    isbn13       TEXT NOT NULL,
    role         TEXT NOT NULL,                  -- chosen | reserve | dropped
    band         TEXT, quarter TEXT, price_group TEXT, price_eur REAL, publisher TEXT,
    reason       TEXT,
    seed         INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (sample_name, isbn13)
);

CREATE TABLE IF NOT EXISTS vlbtix_lookups (    -- one row per book checked on VLB-TIX
    isbn13       TEXT PRIMARY KEY,
    status       TEXT NOT NULL,                  -- found | no_images | not_found | download_failed
    product_page TEXT,                           -- without session tokens
    images_saved INTEGER NOT NULL DEFAULT 0,
    checked_at   TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS book_vlbtix_info (  -- facts about OUR book read from its VLB-TIX page
    isbn13       TEXT PRIMARY KEY,
    series_name  TEXT,
    volume       INTEGER,
    price_eur    REAL,
    release_date TEXT,
    availability TEXT,
    seen_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_relations (  -- 'related products' listed on our book's page
    isbn13       TEXT NOT NULL,                  -- our book
    relation     TEXT NOT NULL,                  -- section heading, e.g. Referenztitel
    related_ean  TEXT NOT NULL,
    title TEXT, author TEXT, publisher TEXT, format TEXT, price_eur REAL, release_date TEXT, availability TEXT,
    related_url  TEXT,
    card_text    TEXT,                           -- raw text, in case parsing missed something
    seen_at      TEXT NOT NULL,
    UNIQUE (isbn13, relation, related_ean)
);
CREATE INDEX IF NOT EXISTS ix_rel_related ON product_relations(related_ean);
CREATE TABLE IF NOT EXISTS preview_memberships ( -- 'Enthalten in Vorschauen'
    isbn13 TEXT NOT NULL, preview_name TEXT NOT NULL, preview_publisher TEXT, preview_date TEXT,
    product_count INTEGER, preview_url TEXT, seen_at TEXT NOT NULL,
    UNIQUE (isbn13, preview_name, preview_date)
);

CREATE TABLE IF NOT EXISTS events (           -- simple audit log of pipeline runs
    event_id INTEGER PRIMARY KEY, at TEXT NOT NULL, command TEXT NOT NULL, detail TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


MIGRATIONS = {  # table -> columns added after v1 (for databases created by an earlier version)
    "books": {"asin": "TEXT", "author": "TEXT", "publication_date": "TEXT"},
    "preview_memberships": {"kind": "TEXT DEFAULT 'preview'"},   # preview | special
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()


def log_event(conn: sqlite3.Connection, command: str, detail: str = "") -> None:
    conn.execute("INSERT INTO events(at, command, detail) VALUES (?,?,?)", (now(), command, detail[:2000]))
    conn.commit()
