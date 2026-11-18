"""Source adapters discover image references and register them in `image_sources`.

Implemented:   url_manifest  (CSV of approved image URLs)       -> sources/url_manifest.py
               local_import  (files delivered by VLB/publisher)  -> sources/local_files.py
Optional/off:  vlbtix_playwright (authorized page access only)   -> sources/vlbtix_playwright.py
"""
from typing import Protocol
import sqlite3


class SourceAdapter(Protocol):
    name: str

    def discover(self, conn: sqlite3.Connection, limit: int | None = None, dry_run: bool = False) -> dict:
        """Register candidate images (status 'pending') and return a small report dict."""
        ...
