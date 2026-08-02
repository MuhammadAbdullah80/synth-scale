"""Unit tests for the SSRF guard (app/dbsafety.py). Fully offline: DNS
resolution is monkeypatched, so nothing here touches the network."""
from __future__ import annotations

import socket

import pytest

from app.dbsafety import UnsafeDatabaseURLError, assert_public_host


def _fake_addrinfo(ip: str):
    """Shape socket.getaddrinfo() actually returns: a list of 5-tuples with
    the address as the last element's first item."""

    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return fake


PRIVATE_AND_INTERNAL_IPS = [
    "127.0.0.1",        # loopback
    "10.0.0.5",          # RFC1918
    "172.16.0.5",        # RFC1918
    "192.168.1.5",       # RFC1918
    "169.254.169.254",   # cloud metadata endpoint
    "169.254.1.1",       # link-local
    "0.0.0.0",            # unspecified
    "::1",                # IPv6 loopback
    "fc00::1",             # IPv6 unique local
]


@pytest.mark.parametrize("ip", PRIVATE_AND_INTERNAL_IPS)
def test_rejects_private_and_internal_targets(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo(ip))
    with pytest.raises(UnsafeDatabaseURLError, match="private or internal"):
        assert_public_host(f"postgresql://user:pw@evil.example.com:5432/db")


def test_allows_public_looking_target(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    result = assert_public_host("postgresql://user:pw@db.example.com:5432/mydb")
    assert result == "postgresql://user:pw@db.example.com:5432/mydb"


def test_normalizes_postgres_scheme_before_checking(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    result = assert_public_host("postgres://user:pw@db.example.com:5432/mydb")
    assert result.startswith("postgresql://")


def test_rejects_unsupported_scheme(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    with pytest.raises(UnsafeDatabaseURLError, match="unsupported"):
        assert_public_host("mysql://user:pw@db.example.com:3306/mydb")


def test_rejects_missing_host():
    with pytest.raises(UnsafeDatabaseURLError, match="missing a host"):
        assert_public_host("postgresql:///mydb")


def test_unresolvable_host_is_rejected(monkeypatch):
    def raise_gaierror(host, port, *a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
    with pytest.raises(UnsafeDatabaseURLError, match="could not resolve"):
        assert_public_host("postgresql://user:pw@nonexistent.invalid:5432/db")


def test_rejects_if_any_resolved_address_is_private(monkeypatch):
    # A host resolving to multiple addresses (e.g. dual-stack) is rejected if
    # ANY of them is private/internal -- not just the first one returned.
    def multi(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", multi)
    with pytest.raises(UnsafeDatabaseURLError, match="private or internal"):
        assert_public_host("postgresql://user:pw@dual.example.com:5432/db")
