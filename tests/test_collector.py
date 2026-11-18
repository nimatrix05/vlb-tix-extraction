"""Downloader tests with mocked HTTP (httpx.MockTransport) - nothing leaves the machine."""
import httpx
import pytest
from PIL import Image

from conftest import ISBN_A, ISBN_B, image_bytes, public_resolver, write
from pipeline.books import import_books
from pipeline.collector import collect, import_local_images, reset_failed
from pipeline.images import InvalidImageError, validate_image
from pipeline.sources.url_manifest import import_url_manifest

BASE = "https://img.example.com"


def setup_sources(project, tmp_path, urls):
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13\n{ISBN_A}\n{ISBN_B}\n"))
    lines = "\n".join(f"{isbn},{u}" for isbn, u in urls)
    import_url_manifest(conn, write(tmp_path / "u.csv", "isbn13,image_url\n" + lines + "\n"))
    return s, conn


def run(s, conn, handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return collect(conn, s, client=client, resolver=public_resolver, sleep=lambda _x: None, **kw)


def test_download_store_and_dedupe(project, tmp_path):
    a, b = image_bytes((1, 2, 3)), image_bytes((200, 0, 0), fmt="JPEG")
    s, conn = setup_sources(project, tmp_path, [
        (ISBN_A, f"{BASE}/a.png"), (ISBN_A, f"{BASE}/a_copy.png"), (ISBN_A, f"{BASE}/b.jpg"), (ISBN_B, f"{BASE}/a.png")])
    body = {"/a.png": a, "/a_copy.png": a, "/b.jpg": b}
    rep = run(s, conn, lambda req: httpx.Response(200, content=body[req.url.path],
                                                  headers={"content-type": "image/png"}), concurrency=1)
    assert rep["downloaded"] == 3 and rep["duplicate"] == 1
    rows = conn.execute("SELECT image_key, local_path, format FROM images ORDER BY image_key").fetchall()
    assert [r[0] for r in rows] == [f"{ISBN_A}_img_001", f"{ISBN_A}_img_002", f"{ISBN_B}_img_001"]
    for r in rows:
        assert (s.path(s.images_dir) / r[1]).is_file()
    # same image for a second ISBN is stored for that ISBN too (shared marketing asset)
    assert conn.execute("SELECT COUNT(DISTINCT sha256) FROM images").fetchone()[0] == 2


def test_retry_then_success_and_permanent_errors(project, tmp_path):
    s, conn = setup_sources(project, tmp_path, [(ISBN_A, f"{BASE}/flaky.png"), (ISBN_A, f"{BASE}/missing.png"),
                                                (ISBN_A, f"{BASE}/html.png")])
    calls = {"flaky": 0}

    def handler(req):
        if req.url.path == "/flaky.png":
            calls["flaky"] += 1
            if calls["flaky"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, content=image_bytes())
        if req.url.path == "/html.png":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(404)

    rep = run(s, conn, handler)
    assert calls["flaky"] == 3 and rep["downloaded"] == 1 and rep["rejected"] == 2


def test_transport_errors_exhaust_retries_then_retry_failed(project, tmp_path):
    s, conn = setup_sources(project, tmp_path, [(ISBN_A, f"{BASE}/x.png")])

    def boom(req):
        raise httpx.ConnectError("down", request=req)
    rep = run(s, conn, boom)
    assert rep["failed"] == 1
    assert conn.execute("SELECT attempts FROM image_sources").fetchone()[0] == 1
    assert reset_failed(conn) == 1
    rep = run(s, conn, lambda r: httpx.Response(200, content=image_bytes()))
    assert rep["downloaded"] == 1


def test_unsafe_redirect_blocked(project, tmp_path):
    s, conn = setup_sources(project, tmp_path, [(ISBN_A, f"{BASE}/r1"), (ISBN_A, f"{BASE}/r2"), (ISBN_A, f"{BASE}/ok")])

    def handler(req):
        if req.url.path == "/r1":
            return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data"})
        if req.url.path == "/r2":
            return httpx.Response(301, headers={"location": "https://evil.com/x.png"})
        if req.url.path == "/ok":
            return httpx.Response(302, headers={"location": "/final.png"})
        return httpx.Response(200, content=image_bytes())
    rep = run(s, conn, handler)
    assert rep["downloaded"] == 1 and rep["unsafe"] == 2


def test_redirect_loop_and_size_limit(project, tmp_path):
    s, conn = setup_sources(project, tmp_path, [(ISBN_A, f"{BASE}/loop"), (ISBN_A, f"{BASE}/big.png")])
    s.max_download_bytes = 5000

    def handler(req):
        if req.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        return httpx.Response(200, content=b"\x89PNG" + b"0" * 10000)  # no content-length check bypass
    rep = run(s, conn, handler)
    errs = [r[0] for r in conn.execute("SELECT last_error FROM image_sources ORDER BY source_id")]
    assert rep["rejected"] == 2
    assert "redirect" in errs[0] and ("size" in errs[1] or "large" in errs[1])


def test_resume_after_interruption(project, tmp_path):
    s, conn = setup_sources(project, tmp_path, [(ISBN_A, f"{BASE}/1.png"), (ISBN_B, f"{BASE}/2.png")])
    (s.path("data/tmp")).mkdir(parents=True, exist_ok=True)
    (s.path("data/tmp") / "stale.part").write_bytes(b"partial")  # leftover from a crash
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, content=image_bytes((len(seen), 0, 0)))
    run(s, conn, handler, limit_books=1)          # "interrupted" after the first book
    run(s, conn, handler)                         # resume: only the remaining source is fetched
    assert seen == ["/1.png", "/2.png"]
    assert not list(s.path("data/tmp").glob("*.part"))
    run(s, conn, handler)                         # nothing left: no requests
    assert len(seen) == 2


def test_dry_run_makes_no_requests(project, tmp_path):
    s, conn = setup_sources(project, tmp_path, [(ISBN_A, f"{BASE}/1.png")])
    rep = run(s, conn, lambda r: pytest.fail("network used in dry-run"), dry_run=True)
    assert rep["sources"] == 1 and conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0


# ---------------------------------------------------------------- validation
def test_validation_rejects_corrupt_tiny_bomb_and_format(project, tmp_path):
    s, _ = project
    p = tmp_path / "x"
    p.write_bytes(b"not an image" * 100)
    with pytest.raises(InvalidImageError, match="corrupt"):
        validate_image(p, s)
    p.write_bytes(image_bytes(size=(20, 20)))
    with pytest.raises(InvalidImageError, match="too small"):
        validate_image(p, s)
    good = image_bytes(size=(300, 300))
    p.write_bytes(good[: len(good) // 2])
    with pytest.raises(InvalidImageError):
        validate_image(p, s)
    Image.new("RGB", (100, 100)).save(p, "BMP")
    with pytest.raises(InvalidImageError, match="unsupported"):
        validate_image(p, s)
    s.max_pixels = 10_000
    p.write_bytes(image_bytes(size=(1000, 1000)))
    with pytest.raises(InvalidImageError):
        validate_image(p, s)


def test_local_import(project, tmp_path):
    s, conn = project
    import_books(conn, write(tmp_path / "b.csv", f"isbn13\n{ISBN_A}\n"))
    (tmp_path / "imgs").mkdir()
    (tmp_path / "imgs" / "c.png").write_bytes(image_bytes())
    m = write(tmp_path / "m.csv", f"isbn13,file_path\n{ISBN_A},imgs/c.png\n{ISBN_A},imgs/missing.png\n")
    rep = import_local_images(conn, s, m)
    assert rep["downloaded"] == 1 and rep["errors"] == 1
    assert import_local_images(conn, s, m)["already_known"] == 1
