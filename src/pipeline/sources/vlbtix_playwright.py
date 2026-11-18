"""Method 2 — OPTIONAL authenticated VLB-TIX collector (Playwright persistent browser profile).

STATUS: NOT VERIFIED AGAINST THE LIVE SITE. Disabled by default.
It runs only when BOTH are set in config.json:
    "vlbtix_live_enabled": true
    "vlbtix_permission_reference": "<who approved automated access + data use, when, ticket/email ref>"
Set these only after VLB-TIX automation and data-use permissions are confirmed in writing.

Authentication (no passwords anywhere in this project):
  * First run (`python -m pipeline vlbtix-login`, or `discover-vlbtix` when no profile exists yet):
    a VISIBLE Chromium window opens with a dedicated profile folder. YOU log in, including MFA.
    Nothing is typed or clicked by the program.
  * Later runs reuse that profile (headless) via Playwright's launch_persistent_context.
  * If the session has expired (login page / password field detected), collection STOPS and asks
    you to run `vlbtix-login` again. It never fills in login forms, never bypasses MFA/CAPTCHAs,
    and never continues after an access-denied page.
  * The profile folder (default .auth/vlbtix_profile, override with env VLBTIX_PROFILE_DIR) holds
    session cookies = credentials. It is git-ignored, created owner-only where the OS supports it,
    and should be deleted (`vlbtix-logout`) when the project ends.

No VLB-TIX endpoint, API or page structure is assumed: the adapter opens the product-page URL from
books.source_ref, collects generic image references (<img src/srcset>, og:image), keeps only URLs on
the image allow-list, and registers them as pending sources. The regular downloader fetches them with
all security checks, using session cookies only for allow-listed hosts.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..config import Settings
from ..db import now
from ..security import UnsafeURLError, check_url, host_allowed
from ..util import log

LOGIN_URL_MARKERS = ("login", "anmelden", "signin", "sign-in", "/auth", "sso", "oauth")
CHALLENGE_MARKERS = ("captcha", "cf-challenge", "are you a robot", "access denied", "zugriff verweigert")

_JS_COLLECT = """
(args) => {
  const [ids, topLevel, relationSections] = args;
  const lc = s => (s || '').trim().toLowerCase();
  // Returns every image reference with context: page section, alt text, size, and whether it sits in a
  // block that describes a DIFFERENT product (e.g. 'Referenztitel' / reference-title covers).
  const norm = s => (s || '').replace(/[\\s\\-\\u2010\\u2011\\u2013]/g, '').toUpperCase();
  const isbnRe = /97[89]\\d{10}/g;
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,[role=heading]'));
  const sectionOf = el => {
    let best = '';
    for (const h of headings) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = (h.innerText || '').trim();
    }
    return best.slice(0, 60);
  };
  const otherProduct = el => {
    let n = el;
    for (let depth = 0; n && depth < 8; depth++, n = n.parentElement) {
      if (n === document.body) break;
      const found = (norm(n.innerText).match(isbnRe) || []);
      if (found.length) return found.some(x => !ids.includes(x)) && !found.includes(ids[0]);
      const link = n.matches && n.matches('a[href]') ? n.getAttribute('href') : null;
      if (link && /\\/app\\/products\\/[0-9a-f]{16,}/.test(link) && !location.href.includes(link)) return true;
    }
    return false;
  };
  // the top-level page section an element sits in (e.g. 'Referenztitel', 'Enthalten in Specials')
  const tops = headings.filter(h => topLevel.some(n => lc(h.innerText).startsWith(lc(n))));
  const topOf = el => {
    let best = '';
    for (const h of tops) if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = lc(h.innerText);
    return best;
  };
  const inRelation = el => { const t = topOf(el); return !!t && relationSections.some(n => t.startsWith(lc(n))); };
  const out = [];
  const add = (url, el) => { if (url && !url.startsWith('data:')) out.push({
      url, alt: (el && el.alt) || '', w: (el && el.naturalWidth) || 0, h: (el && el.naturalHeight) || 0,
      section: el ? sectionOf(el) : 'meta', other: el ? otherProduct(el) : false,
      related_section: el ? inRelation(el) : false }); };
  document.querySelectorAll('img').forEach(i => {
    add(i.currentSrc, i); add(i.getAttribute('src'), i);
    (i.getAttribute('srcset') || '').split(',').forEach(p => { const u = p.trim().split(' ')[0]; add(u, i); });
  });
  document.querySelectorAll('meta[property="og:image"]').forEach(m => add(m.content, null));
  return out;
}
"""


SECRET_QUERY_KEYS = {"access_token", "token", "sid", "session", "sessionid", "auth", "jwt", "id_token", "key", "apikey"}


def redact_url(url: str) -> str:
    """Remove session tokens from a URL before it is stored, logged or exported."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SECRET_QUERY_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


class ReauthenticationRequired(RuntimeError):
    pass


PLACEHOLDER_MARKERS = ("<name>", "<date>", "<ticket", "approved by <", "todo", "tbd", "xxx")


def ensure_enabled(settings: Settings) -> None:
    ref = settings.vlbtix_permission_reference.strip()
    if ref and any(m in ref.lower() for m in PLACEHOLDER_MARKERS):
        raise RuntimeError(
            "vlbtix_permission_reference still contains the example text. Replace it with the real approval "
            "(who approved automated access and image use, when, ticket/email reference).")
    if not settings.vlbtix_live_enabled or not ref:
        raise RuntimeError(
            "Live VLB-TIX collection is disabled. It requires vlbtix_live_enabled=true AND a non-empty "
            "vlbtix_permission_reference in config.json, set only after automation and data-use "
            "permissions are confirmed.")


def profile_dir(settings: Settings) -> Path:
    p = Path(os.environ.get("VLBTIX_PROFILE_DIR") or settings.vlbtix_profile_dir)
    return p if p.is_absolute() else settings.path(str(p))


def _protect(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:  # POSIX: owner-only. On Windows the folder inherits your user-profile ACLs.
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed: pip install playwright && python -m playwright install chromium")


def looks_like_login_page(url: str, html: str) -> bool:
    parts = urlsplit(url)
    target = (parts.path + "?" + parts.query).lower()
    h = (html or "").lower()
    return any(m in target for m in LOGIN_URL_MARKERS) or 'type="password"' in h or "type='password'" in h


def _open_context(p, settings: Settings, headless: bool):
    prof = profile_dir(settings)
    _protect(prof)
    return p.chromium.launch_persistent_context(str(prof), headless=headless, accept_downloads=False)


def interactive_login(settings: Settings, start_url: str | None = None, wait_for_user=input) -> Path:
    """Open a VISIBLE browser on the dedicated profile; the user logs in manually (incl. MFA)."""
    ensure_enabled(settings)
    url = start_url or settings.vlbtix_login_url
    if not url:
        raise RuntimeError("Set vlbtix_login_url in config.json (or pass --url) to the VLB-TIX login/start page.")
    check_url(url, settings.vlbtix_allowed_page_domains, settings.allow_http)
    sync_playwright = _import_playwright()
    with sync_playwright() as p:
        ctx = _open_context(p, settings, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url)
            wait_for_user("\nLog in to VLB-TIX in the browser window YOURSELF (including MFA).\n"
                          "When your logged-in start page is visible, return here and press Enter... ")
            if looks_like_login_page(page.url, page.content() or ""):
                raise ReauthenticationRequired("Still on a login page - not logged in. Run vlbtix-login again.")
        finally:
            ctx.close()
    log.info("VLB-TIX session stored in dedicated profile %s (git-ignored; treat like a password).",
             profile_dir(settings))
    return profile_dir(settings)


def logout(settings: Settings) -> bool:
    """Delete the dedicated profile (session cookies) from disk."""
    prof = profile_dir(settings)
    if prof.exists():
        shutil.rmtree(prof)
        return True
    return False


def session_cookies(settings: Settings) -> list[dict]:
    """Cookies from the profile, restricted to allow-listed IMAGE hosts, for the httpx downloader.
    Values are never logged."""
    if not profile_dir(settings).exists():
        return []
    sync_playwright = _import_playwright()
    with sync_playwright() as p:
        ctx = _open_context(p, settings, headless=True)
        try:
            cookies = ctx.cookies()
        finally:
            ctx.close()
    return [c for c in cookies if host_allowed(c.get("domain", "").lstrip("."), settings.allowed_domains)]


def _msgpack(obj) -> bytes:
    """Minimal MessagePack encoder for the small dicts VLB-TIX puts in its search address."""
    if isinstance(obj, bool):
        return b"\xc3" if obj else b"\xc2"
    if isinstance(obj, int) and 0 <= obj < 128:
        return bytes([obj])
    if isinstance(obj, int) and 0 <= obj < 65536:
        return b"\xcd" + obj.to_bytes(2, "big")
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        if len(raw) < 32:
            return bytes([0xA0 | len(raw)]) + raw
        return b"\xd9" + bytes([len(raw)]) + raw
    if isinstance(obj, dict) and len(obj) < 65536:  # map16, as the VLB-TIX app encodes it
        return b"\xde" + len(obj).to_bytes(2, "big") + b"".join(_msgpack(k) + _msgpack(v) for k, v in obj.items())
    raise TypeError(f"unsupported value {obj!r}")


def vlbtix_qp(query: str, backlist: bool = False, size: int = 20) -> str:
    """Encode a VLB-TIX 'Produkte finden' search the way the web app does:
    MessagePack {q, backlist, size} -> zlib -> base64url without padding.
    (Derived from a search address the user copied; 'q' is the EAN typed into the search box.)"""
    import base64
    import zlib
    packed = _msgpack({"q": query, "backlist": backlist, "size": size})
    return base64.urlsafe_b64encode(zlib.compress(packed, 9)).decode().rstrip("=")


def vlbtix_qp_decode(qp: str) -> bytes:
    import base64
    import zlib
    return zlib.decompress(base64.urlsafe_b64decode(qp + "=" * (-len(qp) % 4)))


def start_urls(settings: Settings, isbn13: str, source_ref: str | None) -> list[str]:
    """Pages to try for a book, in order: source_ref, product template, search template.
    A search template may use {isbn} or {qp} (VLB-TIX encoded search; tries current titles,
    then backlist)."""
    if source_ref and source_ref.startswith("http"):
        return [source_ref]
    urls = []
    t = settings.vlbtix_product_url_template
    if t and "{isbn}" in t:
        urls.append(t.replace("{isbn}", isbn13))
    t = settings.vlbtix_search_url_template
    if t and "{qp}" in t:
        urls.append(t.replace("{qp}", vlbtix_qp(isbn13, backlist=False)))
        if settings.vlbtix_search_backlist_fallback:
            urls.append(t.replace("{qp}", vlbtix_qp(isbn13, backlist=True)))
    elif t and "{isbn}" in t:
        urls.append(t.replace("{isbn}", isbn13))
    return urls


def product_url(settings: Settings, isbn13: str, source_ref: str | None) -> str | None:
    urls = start_urls(settings, isbn13, source_ref)
    return urls[0] if urls else None


_AVAIL_WORDS = ("lieferbar", "erschienen", "vergriffen", "angekündigt", "in vorbereitung", "print on demand")
_SKIP_LINES = ("position ", "empfehlungstitel", "alternativvorschlag", "notizen", "marketing", "reinlesen", "mehr",
               "ähnliches produkt", "ähnliche produkte", "produkt derselben reihe", "produkte derselben reihe")


def parse_product_card(text: str, isbn: str | None) -> dict:
    """Pull the useful fields out of a related-product card's visible text (robust to layout changes;
    anything not recognised stays in the raw text column)."""
    lines = [re.sub(r"^(Position\s*\d+|Empfehlungstitel|Alternativvorschlag des Verlags)\s*", "", l.strip())
             for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    def first(pattern):
        for l in lines:
            m = re.search(pattern, l)
            if m:
                return m.group(1).strip()
        return None
    title = next((l for l in lines if not l.lower().startswith(_SKIP_LINES) and not re.search(r"\d{3}-\d", l)
                  and not l.startswith(("von ", "EUR", "ET:"))), None)
    author = first(r"^von\s+(.+)$")
    if author:  # 'Robert Rath, übersetzt von Birgit Hausmayer' -> 'Robert Rath'
        author = re.split(r",?\s*(?:übersetzt|herausgegeben|illustriert) von\s", author)[0].strip()
    avail = next((l for l in lines if any(w in l.lower() for w in _AVAIL_WORDS)), None)
    price = first(r"EUR\s*([\d.]+,\d{2}|[\d.]+)")
    fmt = first(r"^((?:Paperback|Hardcover|Taschenbuch|Softcover|E-Book|Hörbuch|Gebunden|Kartoniert)\S*.*)$")
    publisher = None
    von = [i for i, l in enumerate(lines) if l.startswith("von ")]
    if von and von[0] + 1 < len(lines):
        nxt = lines[von[0] + 1]
        if not nxt.startswith(("EUR", "ET:", "übersetzt")) and nxt != fmt and not re.search(r"\d{3}-\d", nxt):
            publisher = nxt
    return {"related_ean": isbn, "title": title, "author": author, "publisher": publisher, "format": fmt,
            "price_eur": price.replace(".", "").replace(",", ".") if price else None,
            "release_date": first(r"ET:\s*(\d{2}\.\d{2}\.\d{4})"), "availability": avail}


def parse_preview_card(text: str) -> dict:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    date = next((m.group(0) for l in lines for m in [re.search(r"\d{2}\.\d{2}\.\d{4}", l)] if m), None)
    count = next((int(m.group(1)) for l in lines for m in [re.search(r"(\d+)\s+Produkte?", l)] if m), None)
    name = next((l for l in lines if not re.search(r"\d{2}\.\d{2}\.\d{4}|\d+\s+Produkte?", l)), None)
    publisher = next((l for l in lines[1:] if l != name and not re.search(r"\d", l)), None)
    return {"preview_name": name, "preview_publisher": publisher or name, "preview_date": date, "product_count": count}


def parse_series(page_text: str) -> tuple[str | None, int | None]:
    """'The Scouring, Band 1' -> ('The Scouring', 1). Looks for '<name>, Band/Teil/Bd. <n>' lines."""
    for l in (page_text or "").splitlines():
        m = re.match(r"^\s*(.{2,120}?),\s*(?:Band|Bd\.|Teil|Folge|Volume|Vol\.)\s*(\d{1,4})\s*$", l.strip())
        if m:
            return m.group(1).strip(), int(m.group(2))
    return None, None


def is_card_thumbnail(url: str, isbn13: str) -> bool:
    """VLB-TIX shows OTHER items (related books, preview catalogues, magazines, award lists) as small
    'medium' thumbnails whose address ends in /m; the book's own images use /l or full size.
    Verified on 143 saved images: all 42 '/m' images were listings, none was the book's own."""
    path = urlsplit(url).path.rstrip("/")
    return path.endswith("/m") and f"/cover/tix/{isbn13}/" not in path + "/"


def is_link_icon(alt: str, section: str, names: list[str]) -> bool:
    """Platform logos (alt text or section is just 'Instagram', 'TikTok', ...) carry no book information."""
    n = {x.lower() for x in names}
    a, sec = (alt or "").strip().lower(), (section or "").strip().lower()
    return a in n or (sec in n and (not a or a in n or a == sec))


def isbn13_to_10(isbn13: str) -> str | None:
    if not isbn13.startswith("978"):
        return None
    body = isbn13[3:12]
    check = (11 - sum((10 - i) * int(c) for i, c in enumerate(body)) % 11) % 11
    return body + ("X" if check == 10 else str(check))


def isbn_on_page(text: str, isbn13: str) -> bool:
    """True if the page text shows this book's ISBN-13 or ISBN-10 (hyphens/spaces ignored)."""
    compact = re.sub(r"[\s\-‐‑–]", "", text or "").upper()
    i10 = isbn13_to_10(isbn13)
    return isbn13 in compact or bool(i10 and i10 in compact)


_JS_RESULT_LINKS = """
(ids) => {
  const norm = s => (s || '').replace(/[\\s\\-\\u2010\\u2011\\u2013]/g, '').toUpperCase();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const box = a.closest('li, article, tr, [class*=result], [class*=item], [class*=card]') || a;
    const hay = norm(a.getAttribute('href')) + ' ' + norm(box.innerText);
    if (ids.some(id => id && hay.includes(id))) out.push(a.href);
  }
  return out;
}
"""


_JS_MARK_RESULT = """
(ids) => {
  const norm = s => (s || '').replace(/[\\s\\-\\u2010\\u2011\\u2013]/g, '').toUpperCase();
  let best = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 0 && el.innerText && el.innerText.length > 400) continue;
    if (ids.some(id => id && norm(el.innerText).includes(id))) best = el;  // last = most specific
    if (best && best.children.length === 0) break;
  }
  if (!best) return false;
  const target = best.closest('a, [role=link], [role=button], button, [onclick], li, article, tr, ' +
                              '[class*=result], [class*=item], [class*=card], [class*=product]') || best;
  target.setAttribute('data-pipeline-target', '1');
  return true;
}
"""


_JS_HAS_ISBN = """
(ids) => {
  const t = (document.body ? document.body.innerText : '').replace(/[\\s\\-\\u2010\\u2011\\u2013]/g, '').toUpperCase();
  return ids.some(id => id && t.includes(id));
}
"""


_JS_RELATIONS = """
(args) => {
  // Layout seen on VLB-TIX book pages (Nov 2026):
  //   <h> Referenztitel            (top-level section; ends at the next top-level section heading)
  //     <label> Ähnliches Produkt  (link type; may also be a series name)
  //       card: [cover img] [<h> title] ... EUR .. ET .. 978-.. Lieferbar
  //   <h> Enthalten in Vorschauen
  //       card: [img] name / publisher / date / 'N Produkte'
  // A card = the smallest element holding a cover image AND exactly one foreign EAN (or one 'N Produkte').
  const [wanted, topLevel, ownIds] = args;
  const lc = s => (s || '').trim().toLowerCase();
  const norm = s => (s || '').replace(/[\\s\\-\\u2010\\u2011\\u2013]/g, '').toUpperCase();
  const foreign = el => Array.from(new Set((norm(el.innerText).match(/97[89]\\d{10}/g) || [])))
                          .filter(x => !ownIds.includes(x));
  const hasOwn = el => (norm(el.innerText).match(/97[89]\\d{10}/g) || []).some(x => ownIds.includes(x));
  const counts = el => ((el.innerText || '').match(/\\b\\d+\\s+Produkte?\\b/g) || []).length;
  const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]'));
  const isTop = h => topLevel.some(n => lc(h.innerText).startsWith(lc(n)));
  const all = Array.from(document.querySelectorAll('body *'));
  const result = [];
  heads.forEach((h, idx) => {
    const name = wanted.find(n => lc(h.innerText).startsWith(lc(n)));
    if (!name) return;
    const next = heads.slice(idx + 1).find(x => !h.contains(x) && isTop(x));
    const inRange = el => (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && !el.contains(h)
        && !(next && (el === next || el.contains(next) || (next.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)));
    const els = all.filter(inRange);
    const isPreview = /vorschau|special/i.test(name);
    const imgs = el => el.querySelectorAll('img').length;
    // a listing card = ONE picture plus its words: a 'N Produkte' count or a title heading
    // (the bare picture alone is not a card - it carries no name)
    const isCard = el => !hasOwn(el) && (isPreview
        ? imgs(el) <= 1 && (counts(el) === 1 || (imgs(el) === 1 && heads.some(x => el.contains(x))))
        : foreign(el).length === 1);
    let cards = els.filter(el => isCard(el) && el.querySelector('img'));
    cards = cards.filter(el => !cards.some(o => o !== el && el.contains(o)));        // smallest boxes
    if (!cards.length) {                                                             // text-only fallback
      cards = els.filter(el => isCard(el) && !(el.parentElement && inRange(el.parentElement) && isCard(el.parentElement)));
    }
    const labels = heads.filter(x => x !== h && inRange(x) && !cards.some(c => c.contains(x)));
    for (const c of cards) {
      const label = labels.filter(l => l.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING).pop();
      const t = heads.find(x => c.contains(x));
      const a = c.querySelector('a[href*="/app/"]') || (c.closest && c.closest('a[href*="/app/"]'));
      const img = c.querySelector('img');
      result.push({ section: name, label: label ? label.innerText.trim() : '', title: t ? t.innerText.trim() : '',
                    text: (c.innerText || '').trim().slice(0, 1500), isbns: foreign(c),
                    href: a ? a.href : '', img: img ? (img.currentSrc || img.src || '') : '' });
    }
  });
  return result;
}
"""


class _Stop(Exception):
    """Login expired or access restricted: stop the whole run."""


class VlbTixPlaywrightSource:
    name = "vlbtix_playwright"

    def __init__(self, settings: Settings, wait_for_user=input, debug: bool = False):
        self.s = settings
        self.wait_for_user = wait_for_user
        self.debug_dir = settings.path(f"{settings.log_dir}/vlbtix_debug") if debug else None
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
        self._debug_n = 0

    def _is_search(self, url: str) -> bool:
        t = self.s.vlbtix_search_url_template
        if not t:
            return False
        prefix = t.split("{qp}")[0].split("{isbn}")[0]
        return url.startswith(prefix)

    # ---------------------------------------------------------------- helpers
    def _goto(self, page, url: str):
        """Navigate; raise _Stop on login page / access restriction. Returns HTTP status (0 if unknown)."""
        resp = page.goto(url, wait_until="networkidle", timeout=30000)
        status = resp.status if resp else 0
        html = page.content() or ""
        if looks_like_login_page(page.url, html) or status == 401:
            raise _Stop("VLB-TIX session expired or not logged in - collection stopped. "
                        "Re-authenticate manually: python -m pipeline vlbtix-login")
        if status in (403, 429) or any(m in html.lower()[:200000] for m in CHALLENGE_MARKERS):
            raise _Stop(f"access restriction at {page.url} (HTTP {status}); stopped, not bypassed")
        return status

    def _wait_for_isbn(self, page, isbn: str, timeout_ms: int = 10000) -> bool:
        """Single-page apps render results after the page 'loads': wait until the EAN appears."""
        try:
            page.wait_for_function(_JS_HAS_ISBN, arg=[isbn, isbn13_to_10(isbn)], timeout=timeout_ms)
            return True
        except Exception:
            return False

    def _debug(self, page, isbn: str, step: str) -> None:
        if not self.debug_dir:
            return
        self._debug_n += 1
        base = self.debug_dir / f"{isbn}_{self._debug_n:02d}_{step}"
        try:
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            text = page.inner_text("body")[:30000]
            base.with_suffix(".txt").write_text(f"URL: {page.url}\n\n{text}", encoding="utf-8")
            log.info("debug: saved %s.png/.txt", base.name)
        except Exception as e:
            log.info("debug capture failed (%s)", type(e).__name__)

    def _open_result(self, page, isbn: str) -> bool:
        """On a search page: open the result that belongs to this EAN. Returns True if we left the search page."""
        ids = [isbn, isbn13_to_10(isbn)]
        if not self._wait_for_isbn(page, isbn):
            self._debug(page, isbn, "no_result_shown")
            return False
        self._debug(page, isbn, "search_results")
        links = [u for u in page.evaluate(_JS_RESULT_LINKS, ids)
                 if urlsplit(u).hostname and host_allowed(urlsplit(u).hostname, self.s.vlbtix_allowed_page_domains)]
        if links:
            self._goto(page, links[0])
            return not self._is_search(page.url)
        # Single-page apps often use click handlers instead of links: click the result showing the EAN.
        if page.evaluate(_JS_MARK_RESULT, ids):
            before = page.url
            page.click("[data-pipeline-target='1']", timeout=10000)
            try:
                page.wait_for_url(lambda u: u != before, timeout=15000)
            except Exception:
                return False
            page.wait_for_load_state("networkidle", timeout=30000)
            if looks_like_login_page(page.url, page.content() or ""):
                raise _Stop("VLB-TIX session expired - collection stopped. Re-authenticate: python -m pipeline vlbtix-login")
            return not self._is_search(page.url)
        return False

    def _load_full_page(self, page, max_wait_s: float = 12.0) -> None:
        """Related sections, previews and some images load only when scrolled into view."""
        try:
            for _ in range(12):
                page.evaluate("window.scrollBy(0, Math.max(800, window.innerHeight))")
                page.wait_for_timeout(250)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            deadline = time.monotonic() + max_wait_s
            while time.monotonic() < deadline:
                txt = page.inner_text("body")
                if "Laden…" not in txt and "Laden..." not in txt:
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
            page.wait_for_load_state("networkidle", timeout=10000)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception as e:
            log.debug("full-page load incomplete (%s)", type(e).__name__)

    def _locate(self, page, isbn: str, urls: list[str], rep: dict) -> str | None:
        """Find the product page for this EAN. Returns its URL only if it shows the same ISBN."""
        for url in urls:
            try:
                check_url(url, self.s.vlbtix_allowed_page_domains, self.s.allow_http)
            except UnsafeURLError as e:
                rep["skipped_unsafe"] += 1
                log.warning("page %s not allowed: %s", url, e)
                continue
            try:
                self._goto(page, url)
                rep["pages"] += 1
                if self._is_search(page.url) or not isbn_on_page(page.inner_text("body"), isbn):
                    if not self._open_result(page, isbn):
                        continue  # not in these results (e.g. try the backlist search next)
                if not self._is_search(page.url):
                    self._wait_for_isbn(page, isbn)
                    self._load_full_page(page)
                    self._debug(page, isbn, "product_page")
                    if isbn_on_page(page.inner_text("body"), isbn):
                        return page.url
            except _Stop:
                raise
            except Exception as e:  # timeout, download, network: try next start URL
                log.warning("%s: could not open %s (%s)", isbn, url, type(e).__name__)
            finally:
                time.sleep(max(self.s.min_request_interval_s, self.s.vlbtix_page_interval_s))
        return None

    # ---------------------------------------------------------------- main
    # ---------------------------------------------------------------- image download (same session)
    def _download(self, ctx, url: str) -> tuple[Path | None, str | None]:
        """Fetch an image through the logged-in browser session (tokens in the URL stay valid).
        Returns (tmp_file, error). Same limits as the regular downloader."""
        try:
            check_url(url, self.s.allowed_domains, self.s.allow_http)
        except UnsafeURLError as e:
            return None, f"unsafe URL: {e}"
        last = "unknown"
        for attempt in range(self.s.max_retries + 1):
            time.sleep(self.s.min_request_interval_s)
            try:
                resp = ctx.request.get(url, timeout=self.s.read_timeout_s * 1000, max_redirects=self.s.max_redirects,
                                       fail_on_status_code=False)
            except Exception as e:
                last = type(e).__name__
                continue
            final_host = urlsplit(resp.url).hostname or ""
            if not host_allowed(final_host, self.s.allowed_domains):
                return None, f"redirected to non-allowed host {final_host}"
            if resp.status in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status}"
                time.sleep(self.s.backoff_base_s * (2 ** attempt))
                continue
            if resp.status in (401, 403):
                raise _Stop(f"image download refused (HTTP {resp.status}) - session expired? "
                            "Re-authenticate: python -m pipeline vlbtix-login")
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            ctype = resp.headers.get("content-type", "").lower()
            if ctype and not ctype.startswith(("image/", "application/octet-stream", "binary/octet-stream")):
                return None, f"unexpected content-type {ctype[:40]}"
            clen = resp.headers.get("content-length", "")
            if clen.isdigit() and int(clen) > self.s.max_download_bytes:
                return None, "too large"
            body = resp.body()
            if len(body) > self.s.max_download_bytes:
                return None, "too large"
            tmp = self.tmp_dir / f"{uuid.uuid4().hex}.part"
            tmp.write_bytes(body)
            return tmp, None
        return None, f"gave up after retries: {last}"

    def _record_lookup(self, conn, isbn: str, status: str, product_page: str | None, n_images: int, note: str = ""):
        conn.execute("INSERT INTO vlbtix_lookups(isbn13, status, product_page, images_saved, checked_at, note) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(isbn13) DO UPDATE SET status=excluded.status, "
                     "product_page=excluded.product_page, images_saved=excluded.images_saved, "
                     "checked_at=excluded.checked_at, note=excluded.note",
                     (isbn, status, product_page, n_images, now(), note[:500]))

    def _save_relations(self, conn, page, isbn: str, rep: dict, dry_run: bool) -> None:
        text = page.inner_text("body")
        series, volume = parse_series(text)
        price = re.search(r"EUR\s*([\d.]+,\d{2})", text)
        et = re.search(r"ET:\s*(\d{2}\.\d{2}\.\d{4})", text)
        avail = next((l.strip() for l in text.splitlines()
                      if l.strip().lower() in ("lieferbar", "vorübergehend nicht lieferbar", "nicht lieferbar",
                                               "noch nicht lieferbar", "noch nicht erschienen", "vergriffen",
                                               "nicht mehr lieferbar", "lieferbar on demand")), None)
        cards = page.evaluate(_JS_RELATIONS, [self.s.vlbtix_relation_sections, self.s.vlbtix_page_sections,
                                              [isbn, isbn13_to_10(isbn) or ""]])
        rel_rows, prev_rows = [], []
        for c in cards:
            if "vorschau" in c["section"].lower() or "special" in c["section"].lower():
                pv = parse_preview_card(c["text"])
                if c.get("title"):
                    pv["preview_name"] = c["title"]
                if pv["preview_name"]:
                    prev_rows.append({**pv, "kind": "special" if "special" in c["section"].lower() else "preview",
                                      "url": redact_url(c["href"]) if c["href"] else None})
            elif len(c["isbns"]) == 1:
                pc = parse_product_card(c["text"], c["isbns"][0])
                if c.get("title"):
                    pc["title"] = c["title"]
                relation = c.get("label") or c["section"]       # e.g. 'Ähnliches Produkt' inside 'Referenztitel'
                rel_rows.append({**pc, "relation": relation, "url": redact_url(c["href"]) if c["href"] else None,
                                 "text": c["text"]})
        rep["series_found"] += bool(series)
        if len(rep["relations"]) < 100:
            rep["relations"].append({"isbn": isbn, "series": series, "volume": volume,
                                     "related": [f'{r["relation"]}: {r["related_ean"]} {r["title"] or ""}'.strip()
                                                 for r in rel_rows],
                                     "previews": [f'{p["kind"]}: {p["preview_name"]} ({p["preview_date"]}, '
                                                  f'{p["product_count"]} Produkte)' for p in prev_rows]})
        if dry_run:
            return
        ts = now()
        conn.execute("INSERT OR REPLACE INTO book_vlbtix_info(isbn13, series_name, volume, price_eur, release_date, "
                     "availability, seen_at) VALUES (?,?,?,?,?,?,?)",
                     (isbn, series, volume, float(price.group(1).replace(".", "").replace(",", ".")) if price else None,
                      et.group(1) if et else None, avail, ts))
        conn.execute("DELETE FROM product_relations WHERE isbn13=?", (isbn,))   # keep the latest snapshot
        conn.execute("DELETE FROM preview_memberships WHERE isbn13=?", (isbn,))
        for r in rel_rows:
            conn.execute("INSERT OR IGNORE INTO product_relations(isbn13, relation, related_ean, title, author, "
                         "publisher, format, price_eur, release_date, availability, related_url, card_text, seen_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (isbn, r["relation"], r["related_ean"], r["title"], r["author"], r["publisher"], r["format"],
                          float(r["price_eur"]) if r["price_eur"] else None, r["release_date"], r["availability"],
                          r["url"], r["text"][:1500], ts))
        for pv in prev_rows:
            conn.execute("INSERT OR IGNORE INTO preview_memberships(isbn13, preview_name, preview_publisher, "
                         "preview_date, product_count, preview_url, seen_at, kind) VALUES (?,?,?,?,?,?,?,?)",
                         (isbn, pv["preview_name"], pv["preview_publisher"], pv["preview_date"], pv["product_count"],
                          pv["url"], ts, pv["kind"]))
        rep["related_saved"] += len(rel_rows)
        rep["previews_saved"] += len(prev_rows)

    # ---------------------------------------------------------------- main
    def discover(self, conn: sqlite3.Connection, limit: int | None = None, dry_run: bool = False,
                 isbns: list[str] | None = None, retry_not_found: bool = False, refresh: bool = False) -> dict:
        """Find each book by EAN, pick its images, download them in the same session and store them.
        Books already completed are skipped; books with failed downloads are retried automatically."""
        from ..collector import _tmp_dir, ingest_file
        from ..storage import LocalStorage
        ensure_enabled(self.s)
        if not profile_dir(self.s).exists():
            log.info("No VLB-TIX profile yet - opening a visible browser for manual login (first run).")
            interactive_login(self.s, wait_for_user=self.wait_for_user)
        sync_playwright = _import_playwright()
        skip_status = ("found", "no_images") + (() if retry_not_found else ("not_found",))
        done = set() if refresh else {r[0] for r in conn.execute(
            f"SELECT isbn13 FROM vlbtix_lookups WHERE status IN ({','.join('?' * len(skip_status))})", skip_status)}
        rows = [{"isbn13": r[0], "urls": start_urls(self.s, r[0], r[1])}
                for r in conn.execute("SELECT isbn13, source_ref FROM books ORDER BY isbn13")]
        if isbns:
            wanted = set(isbns)
            rows = [r for r in rows if r["isbn13"] in wanted]   # explicit --isbn re-checks even finished books
            missing = wanted - {r["isbn13"] for r in rows}
            if missing:
                log.warning("not in the book list (run import-books first): %s", sorted(missing))
        else:
            rows = [r for r in rows if r["isbn13"] not in done]
        rows = [r for r in rows if r["urls"]][: limit or None]
        if not rows:
            log.warning("Nothing to do (all books checked, or no search template in config.json). "
                        "Use --retry-not-found to re-check books that were not found.")
        rep = {"books": len(rows), "found": 0, "pages": 0, "images_saved": 0, "images_duplicate": 0,
               "images_rejected": 0, "images_failed": 0, "skipped_unsafe": 0, "not_found": [],
               "related_saved": 0, "previews_saved": 0, "series_found": 0,
               "stopped_reason": None, "permission_reference": self.s.vlbtix_permission_reference, "images": [],
               "relations": []}
        hosts_seen: dict[str, int] = {}
        storage = LocalStorage(self.s.path(self.s.images_dir))
        self.tmp_dir = _tmp_dir(self.s)
        with sync_playwright() as p:
            ctx = _open_context(p, self.s, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                for i, r in enumerate(rows, 1):
                    isbn = r["isbn13"]
                    try:
                        product_page = self._locate(page, isbn, r["urls"], rep)
                    except _Stop as e:
                        rep["stopped_reason"] = str(e)
                        log.error(rep["stopped_reason"])
                        break
                    if not product_page:
                        rep["not_found"].append(isbn)  # never take images from a page we can't tie to the EAN
                        log.warning("%s: not found in VLB-TIX (or page does not show this ISBN) - skipped", isbn)
                        if not dry_run:
                            self._record_lookup(conn, isbn, "not_found", None, 0)
                            conn.commit()
                        continue
                    rep["found"] += 1
                    product_page = redact_url(product_page)
                    if self.s.vlbtix_save_relations:
                        try:
                            self._save_relations(conn, page, isbn, rep, dry_run)
                        except Exception as e:  # never lose the images because of the extra data
                            log.warning("%s: related products not read (%s)", isbn, type(e).__name__)
                    candidates, seen_keys = [], set()
                    for img in page.evaluate(_JS_COLLECT, [[isbn, isbn13_to_10(isbn) or ""],
                                                           self.s.vlbtix_page_sections,
                                                           self.s.vlbtix_relation_sections]):
                        u = urljoin(page.url, img["url"])
                        key = redact_url(u)               # same image, different token -> one entry
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        host = urlsplit(u).hostname or ""
                        hosts_seen[host] = hosts_seen.get(host, 0) + 1
                        edge = max(img["w"], img["h"])
                        if img["other"]:
                            verdict = "skip: belongs to another product"
                        elif img.get("related_section") or any(
                                (img["section"] or "").lower().startswith(n.lower())
                                for n in self.s.vlbtix_relation_sections + self.s.vlbtix_relation_labels):
                            verdict = "skip: related-products section"
                        elif not host_allowed(host, self.s.allowed_domains):
                            verdict = "skip: host not in allowed_domains"
                        elif is_card_thumbnail(key, isbn):
                            verdict = "skip: card thumbnail of another listing"
                        elif is_link_icon(img["alt"], img["section"], self.s.vlbtix_skip_icon_names):
                            verdict = "skip: social-media/link icon"
                        elif 0 < edge < self.s.vlbtix_min_image_edge:
                            verdict = f"skip: thumbnail ({img['w']}x{img['h']})"
                        else:
                            verdict = "keep"
                        entry = {"isbn": isbn, "section": img["section"], "alt": img["alt"][:60],
                                 "size": f'{img["w"]}x{img["h"]}', "verdict": verdict, "url": key}
                        if len(rep["images"]) < 300:
                            rep["images"].append(entry)
                        if verdict == "keep":
                            candidates.append((u, key, img, entry))
                    if dry_run:
                        continue
                    saved = failed = 0
                    try:
                        for u, key, img, entry in candidates:
                            ts = now()
                            conn.execute(
                                "INSERT OR IGNORE INTO image_sources(isbn13, url, source_page, method, rights_note, "
                                "discovered_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                                (isbn, key, product_page, self.name,
                                 f"VLB-TIX section '{img['section'] or img['alt']}'; basis: "
                                 f"{self.s.vlbtix_permission_reference}", ts, ts))
                            src = conn.execute("SELECT * FROM image_sources WHERE isbn13=? AND url=?",
                                               (isbn, key)).fetchone()
                            if src["status"] in ("downloaded", "duplicate"):
                                entry["result"] = "already saved"
                                saved += 1
                                continue
                            tmp, err = self._download(ctx, u)   # URL with token used only here, never stored
                            if err:
                                conn.execute("UPDATE image_sources SET status='failed', last_error=?, "
                                             "attempts=attempts+1, updated_at=? WHERE source_id=?",
                                             (err, now(), src["source_id"]))
                                entry["result"] = f"failed: {err}"
                                rep["images_failed"] += 1
                                failed += 1
                                continue
                            status, detail = ingest_file(conn, self.s, storage, tmp, src)
                            entry["result"] = {"downloaded": f"saved as {detail}", "duplicate": f"duplicate of {detail}",
                                               "rejected": f"rejected: {detail}"}[status]
                            key_name = {"downloaded": "images_saved", "duplicate": "images_duplicate",
                                        "rejected": "images_rejected"}[status]
                            rep[key_name] += 1
                            saved += status in ("downloaded", "duplicate")
                            conn.commit()
                    except _Stop as e:
                        conn.commit()
                        rep["stopped_reason"] = str(e)
                        log.error(rep["stopped_reason"])
                        self._record_lookup(conn, isbn, "download_failed", product_page, saved, "session expired")
                        conn.commit()
                        break
                    status = "download_failed" if failed else ("found" if candidates else "no_images")
                    self._record_lookup(conn, isbn, status, product_page, saved)
                    conn.commit()
                    log.info("%s: found, %d image(s) kept, %d saved, %d failed", isbn, len(candidates), saved, failed)
                    if i % 25 == 0:
                        log.info("progress: %d/%d books, %d found, %d images saved", i, len(rows), rep["found"],
                                 rep["images_saved"])
            finally:
                ctx.close()
        rep["image_hosts_seen_on_book_pages"] = dict(sorted(hosts_seen.items(), key=lambda x: -x[1]))
        if dry_run:
            rep["dry_run"] = True
        return rep
