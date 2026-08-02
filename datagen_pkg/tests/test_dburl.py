"""Unit tests for datagen/dburl.py -- no database needed."""
from __future__ import annotations

from datagen.dburl import mask_secret, normalize_db_url, redact_db_url


# --- normalize_db_url --------------------------------------------------------

def test_normalize_rewrites_bare_postgres_scheme():
    assert normalize_db_url("postgres://u:p@host:5432/db") == "postgresql://u:p@host:5432/db"


def test_normalize_leaves_postgresql_scheme_alone():
    url = "postgresql://u:p@host:5432/db"
    assert normalize_db_url(url) == url


def test_normalize_leaves_driver_suffixed_scheme_alone():
    url = "postgresql+psycopg2://u:p@host:5432/db"
    assert normalize_db_url(url) == url


def test_normalize_strips_surrounding_whitespace():
    assert normalize_db_url("  postgres://u:p@host/db  ") == "postgresql://u:p@host/db"


def test_normalize_does_not_touch_password_containing_the_substring():
    # A password that happens to contain "postgres://" must not get mangled
    # -- normalize only rewrites the leading scheme.
    url = "postgresql://u:has-postgres://-in-it@host/db"
    assert normalize_db_url(url) == url


# --- redact_db_url ------------------------------------------------------------

def test_redact_masks_password_keeps_rest():
    out = redact_db_url("postgresql://myuser:supersecret@db.example.com:5432/mydb")
    assert "supersecret" not in out
    assert "myuser" in out
    assert "***" in out
    assert "db.example.com" in out
    assert "5432" in out
    assert "mydb" in out


def test_redact_no_password_no_mask_marker():
    out = redact_db_url("postgresql://myuser@db.example.com/mydb")
    assert "***" not in out
    assert "myuser" in out


def test_redact_unparseable_string_does_not_leak_raw_input():
    out = redact_db_url("not a url at all, definitely-secret-token")
    assert "definitely-secret-token" not in out


def test_redact_handles_pooler_style_url():
    out = redact_db_url(
        "postgresql://postgres.abcdxyz:p@ssw0rd@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
    )
    assert "p@ssw0rd" not in out
    assert "pooler.supabase.com" in out
    assert "6543" in out


# --- mask_secret --------------------------------------------------------------

def test_mask_secret_replaces_occurrences():
    assert mask_secret("connection to host failed: bad-pw invalid", "bad-pw") == (
        "connection to host failed: *** invalid"
    )


def test_mask_secret_noop_when_secret_empty_or_none():
    assert mask_secret("some message", None) == "some message"
    assert mask_secret("some message", "") == "some message"
