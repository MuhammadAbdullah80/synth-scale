"""Tests for POST /api/contact.

SMTP env vars are never set in the test environment, so these exercise the
"stored but not emailed" path -- send_contact_email returning False is
expected and correct here, not a failure; the durable JSONL store is the
thing under test. A dedicated unit test below covers the SMTP header-
injection guard directly, without needing a real mail server.
"""
from __future__ import annotations

import pytest

from app import main
from app.contact import _clean_header_field, send_contact_email


def test_contact_submit_stores_message(client):
    r = client.post(
        "/api/contact",
        json={"name": "Ada", "email": "ada@example.com", "message": "Love the tool."},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    line = main.contact_store.path.read_text(encoding="utf-8").strip()
    assert '"email": "ada@example.com"' in line
    assert '"message": "Love the tool."' in line
    assert '"ts":' in line and '"ua":' in line


def test_contact_name_optional(client):
    r = client.post("/api/contact", json={"email": "anon@example.com", "message": "hi"})
    assert r.status_code == 200
    line = main.contact_store.path.read_text(encoding="utf-8").strip()
    assert '"name": ""' in line


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "x @y.com", "", "a@@b.com"])
def test_contact_rejects_invalid_email(client, bad):
    r = client.post("/api/contact", json={"email": bad, "message": "hi"})
    assert r.status_code == 422


def test_contact_rejects_empty_message(client):
    r = client.post("/api/contact", json={"email": "a@b.com", "message": "   "})
    assert r.status_code == 422


def test_contact_rejects_oversized_message(client):
    r = client.post("/api/contact", json={"email": "a@b.com", "message": "x" * 4001})
    assert r.status_code == 422


def test_contact_has_own_rate_limit(client):
    body = {"email": "a@b.com", "message": "hi"}
    for _ in range(main.contact_limiter.limit):
        assert client.post("/api/contact", json=body).status_code == 200
    r = client.post("/api/contact", json=body)
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    # /api/waitlist's own limit (there isn't one) is untouched.
    assert client.post("/api/waitlist", json={"email": "still@works.com"}).status_code == 200


def test_send_contact_email_without_smtp_config_returns_false(monkeypatch):
    for var in ("SYNTH_SCALE_SMTP_HOST", "SYNTH_SCALE_SMTP_USER", "SYNTH_SCALE_SMTP_PASS"):
        monkeypatch.delenv(var, raising=False)
    assert send_contact_email("Ada", "ada@example.com", "hi") is False


def test_clean_header_field_strips_crlf():
    # The email address itself can't contain CR/LF (valid_email's regex
    # excludes whitespace), but the free-text display name could without
    # this guard -- e.g. "Ada\r\nBcc: attacker@evil.com" injecting a header.
    assert _clean_header_field("Ada\r\nBcc: attacker@evil.com") == "AdaBcc: attacker@evil.com"
    assert "\r" not in _clean_header_field("a\r\nb")
    assert "\n" not in _clean_header_field("a\r\nb")
