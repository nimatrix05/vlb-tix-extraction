"""ISBN-13 normalisation and check-digit validation."""
from __future__ import annotations

import re

_STRIP = re.compile(r"[\s\-‐‑‒–—]")


def normalize_isbn(raw: str | None) -> str:
    """Remove spaces/hyphens and an optional 'ISBN' prefix. Does not validate."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.upper().startswith("ISBN"):
        s = s[4:].lstrip(":").strip()
    return _STRIP.sub("", s)


def isbn13_check_digit(first12: str) -> int:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12))
    return (10 - total % 10) % 10


def is_valid_isbn13(raw: str | None) -> bool:
    s = normalize_isbn(raw)
    if len(s) != 13 or not s.isdigit() or s[:3] not in ("978", "979"):
        return False
    return isbn13_check_digit(s[:12]) == int(s[12])


def isbn10_to_13(raw: str) -> str | None:
    s = normalize_isbn(raw).upper()
    if len(s) != 10 or not (s[:9].isdigit() and (s[9].isdigit() or s[9] == "X")):
        return None
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s))
    if total % 11 != 0:
        return None
    body = "978" + s[:9]
    return body + str(isbn13_check_digit(body))


def to_isbn13(raw: str | None) -> str | None:
    """Return a valid ISBN-13 or None. ISBN-10 input is converted."""
    s = normalize_isbn(raw)
    if is_valid_isbn13(s):
        return s
    if len(s) == 10:
        return isbn10_to_13(s)
    return None


def make_isbn13(prefix12: str) -> str:
    """Build a valid ISBN-13 from 12 digits (used for synthetic fixtures)."""
    assert len(prefix12) == 12 and prefix12.isdigit()
    return prefix12 + str(isbn13_check_digit(prefix12))
