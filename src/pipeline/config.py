"""Configuration: defaults in code, optional overrides from config.json, secrets only from env vars."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_FILENAME = "config.json"


@dataclass
class Settings:
    # --- locations (relative to project root) ---
    root: Path = field(default_factory=Path.cwd)
    db_file: str = "data/pipeline.db"
    images_dir: str = "data/images"
    batches_dir: str = "batches"
    results_dir: str = "chatgpt_results"
    outputs_dir: str = "outputs"
    log_dir: str = "logs"
    csv_delimiter: str = ";"            # ';' for German Excel, ',' for English Excel / other tools

    # --- web collection (secure defaults: nothing allowed until configured) ---
    allowed_domains: list[str] = field(default_factory=list)  # e.g. ["images.example-publisher.de"]
    allow_http: bool = False             # HTTPS only by default
    max_redirects: int = 3
    max_download_bytes: int = 15 * 1024 * 1024
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    max_retries: int = 3
    backoff_base_s: float = 1.0
    max_retry_after_s: float = 60.0
    concurrency: int = 2
    min_request_interval_s: float = 1.0  # per host
    user_agent: str = "EberleinKunz-ImagePipeline/0.1 (authorized research; contact: data team)"

    # --- image validation ---
    allowed_formats: list[str] = field(default_factory=lambda: ["JPEG", "PNG", "WEBP", "GIF"])
    min_width: int = 50
    min_height: int = 50
    max_pixels: int = 50_000_000         # decompression-bomb guard
    min_file_bytes: int = 500

    # --- ChatGPT batch prep ---
    batch_max_images: int = 20           # adjust to your ChatGPT upload limit
    upload_max_edge: int = 2048          # resize copies for upload (originals untouched); 0 = no resize

    # --- optional live VLB-TIX adapter (disabled until authorization is confirmed) ---
    vlbtix_live_enabled: bool = False
    vlbtix_allowed_page_domains: list[str] = field(default_factory=list)
    vlbtix_permission_reference: str = ""            # REQUIRED to enable: who approved, when, ticket/email ref
    vlbtix_login_url: str = ""                       # page where you log in manually (vlbtix-login)
    vlbtix_product_url_template: str = ""            # product page per book, e.g. "https://<host>/<path>/{isbn}"
    vlbtix_search_url_template: str = ""             # OR a search page: "...?q={isbn}" or VLB-TIX "...?qp={qp}"
    vlbtix_search_backlist_fallback: bool = True     # {qp}: if not in current titles, search the backlist too
    vlbtix_profile_dir: str = ".auth/vlbtix_profile" # dedicated browser profile; git-ignored; treat as secret
    vlbtix_skip_icon_names: list[str] = field(default_factory=lambda: [
        "instagram", "tiktok", "facebook", "youtube", "twitter", "x", "linkedin", "spotify", "pinterest",
        "threads", "bluesky", "podcast", "website", "homepage"])   # link icons, not book images
    vlbtix_relation_sections: list[str] = field(default_factory=lambda: [   # sections whose cards we save
        "Referenztitel", "Enthalten in Vorschauen", "Enthalten in Specials", "Weitere Ausgabearten"])
    vlbtix_relation_labels: list[str] = field(default_factory=lambda: [     # link-type labels inside them
        "Ähnliches Produkt", "Ähnliche Produkte", "Produkt derselben Reihe", "Produkte derselben Reihe"])
    vlbtix_page_sections: list[str] = field(default_factory=lambda: [      # top-level headings (section ends)
        "Cover & Inhalt", "Bibliografie", "Marketing", "Instagram", "TikTok", "Verkaufshinweise", "Referenztitel",
        "Urheber*innen", "Urheber", "Enthalten in Vorschauen", "Enthalten in Specials", "Specials",
        "Weitere Ausgabearten", "Bühne", "Inhalt"])
    vlbtix_save_relations: bool = True
    vlbtix_min_image_edge: int = 200                 # skip thumbnails smaller than this (longest side, px)
    vlbtix_page_interval_s: float = 3.0              # pause between product pages

    def path(self, rel: str) -> Path:
        return (self.root / rel).resolve()

    @property
    def db_path(self) -> Path:
        return self.path(self.db_file)


def load_settings(root: Path | None = None) -> Settings:
    root = (root or Path.cwd()).resolve()
    s = Settings(root=root)
    cfg = root / CONFIG_FILENAME
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)} - {"root"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown keys in {CONFIG_FILENAME}: {sorted(unknown)}")
        for k, v in data.items():
            setattr(s, k, v)
    # Non-secret env overrides (useful for CI); secrets are never read into Settings.
    if os.environ.get("PIPELINE_CONCURRENCY"):
        s.concurrency = int(os.environ["PIPELINE_CONCURRENCY"])
    s.allowed_domains = [d.lower().strip() for d in s.allowed_domains if d.strip()]
    s.concurrency = max(1, min(int(s.concurrency), 16))
    return s
