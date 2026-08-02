"""SSRF guard for the "Connect to Supabase" web flow.

This server is about to do something the rest of the codebase deliberately
never does: take a connection string typed by an anonymous visitor and dial
out to it. That turns the server into a generic "connect to any TCP host and
run SQL" proxy unless the target is constrained. The check here rejects
anything that resolves to a private, loopback, link-local, or otherwise
non-public address -- most importantly the cloud metadata IP
(169.254.169.254) that every "SSRF from a public web app" writeup targets.

Known limitation (documented, not silently ignored): this resolves DNS once
at check time and then a separate connection is opened moments later by
SQLAlchemy/psycopg2. A DNS-rebinding attacker who controls both the domain
and its TTL could, in principle, point the name at a public IP for this
check and a private one for the real connection. Closing that gap fully
means resolving once and connecting to the pinned IP directly (with the
original hostname kept only for TLS SNI/hostname verification), which is
real work; for a v1 explicitly labeled "beta" in the UI, with a short
connect timeout, no credential logging/persistence, and its own tight rate
limit, this is a deliberate scope cut -- not an oversight.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from datagen.dburl import ALLOWED_SCHEMES, normalize_db_url


class UnsafeDatabaseURLError(ValueError):
    """Raised when a caller-supplied connection string is rejected before
    the server ever opens a socket to it."""


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        # is_global covers a few extra IANA special-purpose ranges (e.g.
        # benchmarking, documentation) that the flags above miss.
        or not getattr(ip, "is_global", True)
    )


def assert_public_host(db_url: str) -> str:
    """Validate a connection string before it's used to open a real
    connection. Returns the normalized URL on success; raises
    UnsafeDatabaseURLError with a message safe to show the caller (their own
    input, not a secret) on rejection."""
    normalized = normalize_db_url(db_url)
    parts = urlsplit(normalized)

    scheme = parts.scheme
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeDatabaseURLError(
            f"unsupported connection string scheme {scheme!r}; expected a "
            "postgres(ql):// URL"
        )

    host = parts.hostname
    if not host:
        raise UnsafeDatabaseURLError("connection string is missing a host")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeDatabaseURLError(f"could not resolve host {host!r}: {exc}") from None

    resolved_ips = {info[4][0] for info in infos}
    for raw_ip in resolved_ips:
        ip = ipaddress.ip_address(raw_ip.split("%")[0])  # strip IPv6 zone id
        if not _is_public_ip(ip):
            raise UnsafeDatabaseURLError(
                f"host {host!r} resolves to a private or internal address "
                f"({raw_ip}); the web playground can only connect to publicly "
                "reachable databases. Use the CLI (pip install synth-scale) "
                "for anything on a private network."
            )

    return normalized
