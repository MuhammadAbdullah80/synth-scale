"""Tests for POST /api/connect ("Connect to Supabase").

Split in two:
  * Offline, monkeypatched tests below -- rate limiting, cap propagation,
    the 503-when-unavailable path, response-shape parity with /api/generate.
    These never touch a real database or network.
  * The live section at the bottom runs ONLY when SYNTH_PG_URL is set (same
    gating contract as datagen_pkg/tests/test_postgres_load.py) and proves
    the whole thing end to end against a real Postgres: introspect over the
    network, generate, and confirm zero write-back (the DB is untouched).
"""
from __future__ import annotations

import os

import pytest

from app import main, service, service_db


# ---------------------------------------------------------------------------
# Offline tests
# ---------------------------------------------------------------------------
def test_connect_503_when_db_support_unavailable(client, monkeypatch):
    monkeypatch.setattr(main, "DB_CONNECT_AVAILABLE", False)
    resp = client.post("/api/connect", json={"db_url": "postgresql://u@h/db"})
    assert resp.status_code == 503
    assert "postgres" in resp.json()["detail"].lower()


def test_connect_unsafe_host_rejected_without_network(client, monkeypatch):
    # loopback is rejected by dbsafety before any connection is attempted --
    # no monkeypatching of introspect_db needed, this alone proves the guard
    # runs ahead of the network call.
    resp = client.post("/api/connect", json={"db_url": "postgresql://u:p@127.0.0.1:5432/db"})
    assert resp.status_code == 422
    assert "private or internal" in resp.json()["detail"]


def test_connect_happy_path_preview(client, monkeypatch):
    from datagen.ddl_parser import parse_ddl

    schema = parse_ddl(
        "CREATE TABLE widgets (id SERIAL PRIMARY KEY, name VARCHAR(40) NOT NULL);"
    )

    def fake_introspect(db_url, connect_timeout=None):
        assert connect_timeout is not None  # the bounded-timeout contract
        return schema

    monkeypatch.setattr(service_db, "introspect_db", fake_introspect)
    monkeypatch.setattr(service_db, "assert_public_host", lambda url: url)

    resp = client.post(
        "/api/connect",
        json={"db_url": "postgresql://u:p@example.com:5432/db", "rows": 10, "seed": 1},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tables"][0]["name"] == "widgets"
    assert data["tables"][0]["total_rows"] == 10
    assert data["validation"]["violations_found"] == 0


def test_connect_sql_download_format(client, monkeypatch):
    from datagen.ddl_parser import parse_ddl

    schema = parse_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY);")
    monkeypatch.setattr(service_db, "introspect_db", lambda db_url, connect_timeout=None: schema)
    monkeypatch.setattr(service_db, "assert_public_host", lambda url: url)

    resp = client.post(
        "/api/connect",
        json={"db_url": "postgresql://u:p@example.com:5432/db", "rows": 5, "format": "sql"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/sql")
    assert b"INSERT INTO" in resp.content


def test_connect_introspection_error_is_redacted_and_password_scrubbed(client, monkeypatch):
    def fake_introspect(db_url, connect_timeout=None):
        raise RuntimeError("auth failed for user with password totally-secret-pw")

    monkeypatch.setattr(service_db, "introspect_db", fake_introspect)
    monkeypatch.setattr(service_db, "assert_public_host", lambda url: url)

    resp = client.post(
        "/api/connect",
        json={"db_url": "postgresql://myuser:totally-secret-pw@example.com:5432/db"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "totally-secret-pw" not in detail
    assert "example.com" in detail


def test_connect_has_own_stricter_rate_limit(client, monkeypatch):
    from datagen.ddl_parser import parse_ddl

    schema = parse_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY);")
    monkeypatch.setattr(service_db, "introspect_db", lambda db_url, connect_timeout=None: schema)
    monkeypatch.setattr(service_db, "assert_public_host", lambda url: url)

    body = {"db_url": "postgresql://u:p@example.com:5432/db", "rows": 5}
    limit = main.db_limiter.limit
    for _ in range(limit):
        resp = client.post("/api/connect", json=body)
        assert resp.status_code == 200
    resp = client.post("/api/connect", json=body)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers

    # /api/generate's own limit is untouched by /api/connect's calls above.
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY);"
    resp = client.post("/api/generate", json={"ddl": ddl, "rows": 5})
    assert resp.status_code == 200


def test_connect_row_cap_enforced(client):
    resp = client.post(
        "/api/connect",
        json={"db_url": "postgresql://u:p@example.com:5432/db", "rows": service.MAX_TOTAL_ROWS + 1},
    )
    # Pydantic field validation (le=MAX_TOTAL_ROWS) rejects before the handler runs.
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Live Postgres end-to-end (skips cleanly without SYNTH_PG_URL)
# ---------------------------------------------------------------------------
PG_URL = os.environ.get("SYNTH_PG_URL")

pytestmark_live = pytest.mark.skipif(
    not PG_URL, reason="SYNTH_PG_URL not set; export a Postgres URL to run the live /api/connect test"
)


@pytestmark_live
def test_connect_live_end_to_end(client, monkeypatch):
    """Real network round trip: create a schema in a live Postgres, hit
    /api/connect with its connection string, and confirm generation matches
    what introspecting it directly would produce -- with zero writes back to
    the database (the whole point of this endpoint)."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine, text

    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS connect_probe CASCADE"))
        conn.execute(
            text(
                "CREATE TABLE connect_probe ("
                "id SERIAL PRIMARY KEY, label VARCHAR(40) NOT NULL, "
                "price NUMERIC(8,2) NOT NULL CHECK (price > 0))"
            )
        )

    # The scratch DB for this test is on localhost, which dbsafety correctly
    # rejects (that's its job -- see test_dbsafety.py for dedicated coverage
    # of the SSRF guard itself, including against real loopback resolution).
    # This test is about a different claim: that the introspect -> generate
    # -> validate pipeline behind the guard actually works over a real
    # network connection. So the guard is bypassed here on purpose, the same
    # way the offline tests above do it.
    monkeypatch.setattr(service_db, "assert_public_host", lambda url: url)
    plain_url = PG_URL.replace("+psycopg2", "")
    schema, order, row_counts, generated, report = service_db.generate_from_db(
        plain_url, rows=25, seed=7
    )
    assert "connect_probe" in schema.tables
    assert row_counts["connect_probe"] == 25
    assert report.violations_found == 0
    assert all(row["price"] > 0 for row in generated["connect_probe"])

    # Zero write-back: row count in the live table is untouched (still 0).
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM connect_probe")).scalar()
    assert count == 0

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE connect_probe CASCADE"))
    engine.dispose()
