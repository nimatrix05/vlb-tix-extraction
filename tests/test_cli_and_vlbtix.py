import json
import sys

import pytest

from conftest import ROOT
from pipeline.cli import main
from pipeline.config import load_settings
from pipeline.sources import vlbtix_playwright as vp
from pipeline.util import RedactSecrets
import logging


def test_cli_end_to_end_with_demo_data(project, capsys, monkeypatch):
    s, conn = project
    root = str(s.root)
    sys.path.insert(0, str(ROOT / "tools"))
    import make_demo_data
    monkeypatch.setattr(make_demo_data, "ROOT", s.root)
    make_demo_data.main()
    assert main(["--root", root, "init"]) == 0
    assert main(["--root", root, "import-books", str(s.root / "input/demo_books.csv")]) == 0
    assert main(["--root", root, "import-local-images", str(s.root / "input/demo_local_images.csv")]) == 0
    assert main(["--root", root, "prepare-batch", "--books", "10"]) == 0
    assert main(["--root", root, "status"]) == 0
    assert main(["--root", root, "export"]) == 0
    assert main(["--root", root, "import-results", "does_not_exist.json"]) != 0
    out = capsys.readouterr().out
    assert '"batch_id": "batch_001"' in out


def test_vlbtix_disabled_by_default(project):
    s, conn = project
    with pytest.raises(RuntimeError, match="disabled"):
        vp.VlbTixPlaywrightSource(s).discover(conn)
    with pytest.raises(RuntimeError, match="disabled"):
        vp.interactive_login(s, "https://vlbtix.example/login")


def test_vlbtix_needs_permission_reference(project):
    s, conn = project
    s.vlbtix_live_enabled = True           # flag alone is not enough
    with pytest.raises(RuntimeError, match="permission_reference"):
        vp.ensure_enabled(s)
    s.vlbtix_permission_reference = "Legal approval 2026-11-20, ticket IT-123"
    vp.ensure_enabled(s)


def test_login_page_detection():
    assert vp.looks_like_login_page("https://x.de/login?next=/", "")
    assert vp.looks_like_login_page("https://x.de/product/1", '<input type="password" name="pw">')
    assert not vp.looks_like_login_page("https://x.de/product/9783161484100", "<img src='a.jpg'>")


def test_profile_dir_env_override_and_logout(project, tmp_path, monkeypatch):
    s, _ = project
    assert vp.profile_dir(s) == s.path(".auth/vlbtix_profile")
    monkeypatch.setenv("VLBTIX_PROFILE_DIR", str(tmp_path / "prof"))
    (tmp_path / "prof").mkdir()
    assert vp.logout(s) is True and not (tmp_path / "prof").exists()


def test_gitignore_protects_sessions_and_data():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".auth/", ".env", "data/*", "chatgpt_results/*", "batches/*"):
        assert pattern in gi


def test_secret_redaction():
    rec = logging.LogRecord("pipeline", logging.INFO, "", 0, "login token=abc123 password: hunter2", (), None)
    RedactSecrets().filter(rec)
    assert "abc123" not in rec.getMessage() and "hunter2" not in rec.getMessage()


def test_unknown_config_key_rejected(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"alowed_domains": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown keys"):
        load_settings(tmp_path)


def test_product_url_template(project):
    s, _ = project
    assert vp.product_url(s, "9783161484100", None) is None
    s.vlbtix_product_url_template = "https://tix.example.de/title/{isbn}"
    assert vp.product_url(s, "9783161484100", "") == "https://tix.example.de/title/9783161484100"
    assert vp.product_url(s, "9783161484100", "https://other.example/x") == "https://other.example/x"


def test_search_template_and_isbn_verification(project):
    s, _ = project
    s.vlbtix_search_url_template = "https://tix.example.de/search?q={isbn}"
    assert vp.product_url(s, "9783161484100", None) == "https://tix.example.de/search?q=9783161484100"
    assert vp.isbn13_to_10("9783161484100") == "316148410X"
    assert vp.isbn_on_page("ISBN: 978-3-16-148410-0", "9783161484100")
    assert vp.isbn_on_page("ISBN-10 3-16-148410-X", "9783161484100")
    assert not vp.isbn_on_page("ISBN: 978-3-16-148411-7", "9783161484100")
    src = vp.VlbTixPlaywrightSource(s)
    assert src._is_search("https://tix.example.de/search?q=1") and not src._is_search("https://tix.example.de/p/1")


def test_vlbtix_qp_matches_real_search_address():
    # address copied from a manual 'Produkte finden' search for EAN 9783747407905
    assert vp.vlbtix_qp("9783747407905") == "eNq7x8C8sHCtpbmFsbmJuYmBuaWB6YqkxOTsnMzikkNLijOrUkUA0t0MgQ"
    assert vp.vlbtix_qp_decode(vp.vlbtix_qp("9783550201806", backlist=True)).endswith(b"backlist\xc3\xa4size\x14")


def test_start_urls_with_qp_and_backlist_fallback(project):
    s, _ = project
    s.vlbtix_search_url_template = "https://www.vlbtix.de/app/products?qp={qp}"
    urls = vp.start_urls(s, "9783747407905", None)
    assert len(urls) == 2 and urls[0].endswith("eNq7x8C8sHCtpbmFsbmJuYmBuaWB6YqkxOTsnMzikkNLijOrUkUA0t0MgQ")
    s.vlbtix_search_backlist_fallback = False
    assert len(vp.start_urls(s, "9783747407905", None)) == 1


def test_placeholder_permission_reference_rejected(project):
    s, _ = project
    s.vlbtix_live_enabled = True
    s.vlbtix_permission_reference = "Approved by <name>, <date>, <ticket/email>"
    with pytest.raises(RuntimeError, match="example text"):
        vp.ensure_enabled(s)


def test_redact_url_removes_session_tokens():
    u = "https://api.vlb.de/api/v1/cover/tix/9783747407905/l?access_token=959debf5-d650&size=l"
    assert vp.redact_url(u) == "https://api.vlb.de/api/v1/cover/tix/9783747407905/l?size=l"
    assert vp.redact_url("https://x.de/a.jpg") == "https://x.de/a.jpg"


def test_social_icons_detected():
    names = ["instagram", "tiktok"]
    assert vp.is_link_icon("Instagram", "Instagram", names)
    assert vp.is_link_icon("TikTok", "TikTok", names)
    assert not vp.is_link_icon("Cover", "Cover & Inhalt", names)
    assert not vp.is_link_icon("Bühne: Lust – Macht – Begehren", "", names)
    assert not vp.is_link_icon("Instagram-Post: Leserin mit Buch", "Instagram", names)  # a real post image


def test_prune_icon_images(project, tmp_path):
    from conftest import ISBN_A, image_bytes, write
    from pipeline.books import import_books
    from pipeline.collector import ingest_file, prune_icon_images
    from pipeline.storage import LocalStorage
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13\n{ISBN_A}\n"))
    storage = LocalStorage(s.path(s.images_dir))
    for n, sec in enumerate(["Cover & Inhalt", "Instagram"]):
        conn.execute("INSERT INTO image_sources(isbn13,url,method,rights_note,discovered_at,updated_at) "
                     "VALUES (?,?,?,?,?,?)", (ISBN_A, f"https://api.vlb.de/{n}", "vlbtix_playwright",
                                               f"VLB-TIX section '{sec}'; basis: x", "t", "t"))
        src = conn.execute("SELECT * FROM image_sources WHERE url=?", (f"https://api.vlb.de/{n}",)).fetchone()
        t = tmp_path / f"{n}.png"
        t.write_bytes(image_bytes((n * 90, 1, 1)))
        ingest_file(conn, s, storage, t, src)
    assert prune_icon_images(conn, s, dry_run=True)["would_remove"] == [f"{ISBN_A}/img_002"]
    prune_icon_images(conn, s)
    assert [r[0] for r in conn.execute("SELECT image_id FROM images")] == ["img_001"]
    assert (s.path("data/_removed") / ISBN_A / "img_002.png").exists()


def test_card_thumbnail_rule():
    e = "9783036950938"
    assert vp.is_card_thumbnail("https://api.vlb.de/api/v1/asset/proprietary/file/abc/m", e)
    assert vp.is_card_thumbnail("https://api.vlb.de/api/v1/cover/tix/9781804078280/m", e)      # another book
    assert not vp.is_card_thumbnail(f"https://api.vlb.de/api/v1/cover/tix/{e}/l", e)              # own cover
    assert not vp.is_card_thumbnail(f"https://api.vlb.de/api/v1/cover/tix/{e}/m", e)              # own cover, medium
    assert not vp.is_card_thumbnail("https://api.vlb.de/api/v1/thumbnail/f7b7/1200/350", e)       # Bühne banner
    assert not vp.is_card_thumbnail("https://api.vlb.de/api/v1/asset/mmo/file/f9a2", e)          # marketing asset
