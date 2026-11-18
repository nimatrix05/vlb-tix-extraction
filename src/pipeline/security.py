"""URL and path safety: domain allow-list, SSRF protection, safe filenames, path containment."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]

BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}


class UnsafeURLError(ValueError):
    pass


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos})


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_link_local
                                   or addr.is_loopback or addr.is_private or addr.is_unspecified)


def host_allowed(host: str, allowed_domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def check_url(url: str, allowed_domains: list[str], allow_http: bool = False,
              resolver: Resolver | None = default_resolver) -> str:
    """Raise UnsafeURLError unless the URL is safe to fetch. Returns the URL unchanged.

    Rules: https only (unless allow_http), no credentials in URL, default ports only,
    host must be on the allow-list, must not be an IP literal / localhost / metadata host,
    and every resolved address must be public (blocks private networks and 169.254.169.254).
    Note: DNS can change between this check and the connection (rebinding); the allow-list
    is the primary control, this is defence in depth.
    """
    if not url or len(url) > 2048 or any(c in url for c in "\r\n\t "):
        raise UnsafeURLError("empty, overlong or whitespace-containing URL")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or (scheme == "http" and not allow_http):
        raise UnsafeURLError(f"scheme not allowed: {scheme or '(none)'}")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURLError("missing host")
    try:
        port = parts.port
    except ValueError as e:
        raise UnsafeURLError(f"bad port: {e}") from None
    if port not in (None, 443, 80):
        raise UnsafeURLError(f"non-standard port {port} not allowed")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost") or host.endswith(".internal"):
        raise UnsafeURLError(f"blocked host: {host}")
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        raise UnsafeURLError("IP-literal hosts are not allowed")
    if not allowed_domains:
        raise UnsafeURLError("no allowed_domains configured (set them in config.json)")
    if not host_allowed(host, allowed_domains):
        raise UnsafeURLError(f"host not on allow-list: {host}")
    if resolver is not None:
        try:
            ips = resolver(host)
        except OSError as e:
            raise UnsafeURLError(f"DNS resolution failed for {host}: {e}") from None
        if not ips:
            raise UnsafeURLError(f"no addresses for {host}")
        bad = [ip for ip in ips if not _ip_is_public(ip)]
        if bad:
            raise UnsafeURLError(f"{host} resolves to non-public address(es) {bad}")
    return url


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}$")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_component(name: str) -> str:
    """Validate a single generated path component (we never use names from the web)."""
    if not _SAFE_COMPONENT.match(name) or ".." in name or name.split(".")[0].upper() in _WIN_RESERVED:
        raise ValueError(f"unsafe path component: {name!r}")
    return name


def contained_path(base: Path, *parts: str) -> Path:
    """Join parts under base and guarantee the result stays inside base (no traversal)."""
    base = base.resolve()
    p = base.joinpath(*[safe_component(x) for x in parts]).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"path escapes base directory: {p}")
    return p
