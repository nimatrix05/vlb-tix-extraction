"""Shared fixtures. Everything is synthetic: no network, no VLB-TIX, no paid API calls."""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pipeline import db as dbm  # noqa: E402
from pipeline.config import load_settings  # noqa: E402
from pipeline.isbn import make_isbn13  # noqa: E402

ISBN_A = make_isbn13("978399990000")
ISBN_B = make_isbn13("978399990001")
ISBN_C = make_isbn13("978399990002")


def image_bytes(color=(10, 20, 30), size=(200, 300), fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def project(tmp_path):
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    (tmp_path / "config.json").write_text('{"allowed_domains": ["img.example.com"], "min_request_interval_s": 0, '
                                          '"backoff_base_s": 0, "min_file_bytes": 10}', encoding="utf-8")
    s = load_settings(tmp_path)
    conn = dbm.connect(s.db_path)
    dbm.init_db(conn)
    yield s, conn
    conn.close()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]
