"""API tests for the Synth-Scale web front door.

Run:  A:/comebck/datagen/datagen_pkg/.venv/Scripts/python.exe -m pytest web/tests -q
"""
from __future__ import annotations

import csv
import io
import time
import zipfile

import pytest

from app import main, service

EXAMPLE_DDL = """
CREATE TABLE categories (
  id SERIAL PRIMARY KEY,
  name VARCHAR(60) NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  email VARCHAR(120) NOT NULL UNIQUE,
  first_name VARCHAR(40) NOT NULL,
  city VARCHAR(60),
  country VARCHAR(60),
  is_active BOOLEAN NOT NULL,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  product_name VARCHAR(80) NOT NULL,
  price NUMERIC(8,2) NOT NULL CHECK (price > 0),
  stock INTEGER NOT NULL CHECK (stock >= 0),
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  status VARCHAR(12) NOT NULL
    CHECK (status IN ('pending','paid','shipped','delivered','cancelled')),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  placed_at TIMESTAMP NOT NULL,
  shipped_at TIMESTAMP,
  delivered_at TIMESTAMP,
  cancelled_at TIMESTAMP
);
"""

TINY_DDL = "CREATE TABLE t (id SERIAL PRIMARY KEY, name VARCHAR(40));"


def gen_body(**overrides):
    body = {"ddl": EXAMPLE_DDL, "rows": 50, "seed": 42, "format": "preview"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Health + static
# ---------------------------------------------------------------------------
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "synth" in r.text.lower()


# ---------------------------------------------------------------------------
# Generate: happy paths
# ---------------------------------------------------------------------------
def test_generate_preview(client):
    r = client.post("/api/generate", json=gen_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seed"] == 42
    assert set(data["row_counts"]) == {"categories", "users", "products", "orders"}
    assert data["total_rows"] == sum(data["row_counts"].values())
    assert data["total_rows"] <= service.MAX_TOTAL_ROWS
    assert data["validation"]["violations_found"] == 0
    assert data["validation"]["constraints_checked"] > 0
    tables = {t["name"]: t for t in data["tables"]}
    for name, t in tables.items():
        assert len(t["rows"]) <= 10
        assert t["total_rows"] == data["row_counts"][name]
        assert all(len(row) == len(t["columns"]) for row in t["rows"])
    # child gets 3x parents (orders = 3 * max(users, products))
    rc = data["row_counts"]
    assert rc["orders"] == min(3 * max(rc["users"], rc["products"]), 10 * 50)


def test_generate_preview_deterministic(client):
    r1 = client.post("/api/generate", json=gen_body())
    r2 = client.post("/api/generate", json=gen_body())
    assert r1.json() == r2.json()


def test_generate_sql(client):
    r = client.post("/api/generate", json=gen_body(format="sql"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/sql")
    assert "attachment" in r.headers["content-disposition"]
    text = r.text
    assert text.count("BEGIN;") == 1 and text.count("COMMIT;") == 1
    assert 'INSERT INTO "orders"' in text
    # FK-safe ordering: parents inserted before children
    assert text.index('INSERT INTO "users"') < text.index('INSERT INTO "orders"')


def test_generate_csv_zip(client):
    r = client.post("/api/generate", json=gen_body(format="csv", rows=20))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert names == {"categories.csv", "users.csv", "products.csv", "orders.csv"}
    users = list(csv.reader(io.TextIOWrapper(zf.open("users.csv"), encoding="utf-8")))
    assert users[0] == ["id", "email", "first_name", "city", "country", "is_active", "created_at"]
    assert len(users) == 21  # header + 20 rows


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
def test_cap_oversize_ddl(client):
    big = TINY_DDL + "\n-- " + "x" * (service.MAX_DDL_BYTES + 100)
    r = client.post("/api/generate", json=gen_body(ddl=big))
    assert r.status_code == 413
    assert "50 KB" in r.json()["detail"]


def test_cap_too_many_tables(client):
    ddl = "\n".join(
        f"CREATE TABLE t{i} (id SERIAL PRIMARY KEY, name VARCHAR(20));" for i in range(16)
    )
    r = client.post("/api/generate", json=gen_body(ddl=ddl))
    assert r.status_code == 422
    assert "16 tables" in r.json()["detail"]


def test_cap_too_many_columns(client):
    cols = ", ".join(f"c{i} INTEGER" for i in range(121))
    ddl = f"CREATE TABLE wide (id SERIAL PRIMARY KEY, {cols});"
    r = client.post("/api/generate", json=gen_body(ddl=ddl))
    assert r.status_code == 422
    assert "columns" in r.json()["detail"]


def test_cap_rows_over_1000(client):
    r = client.post("/api/generate", json=gen_body(rows=1001))
    assert r.status_code == 422  # pydantic rejects before the handler runs


def test_cap_rows_zero(client):
    r = client.post("/api/generate", json=gen_body(rows=0))
    assert r.status_code == 422


def test_total_rows_capped_at_1000(client):
    # rows=1000 with children at 3x would total far above 1000 -> scaled down
    r = client.post("/api/generate", json=gen_body(rows=1000))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_rows"] <= 1000
    assert all(n >= 1 for n in data["row_counts"].values())


def test_body_size_limit(client):
    r = client.post(
        "/api/generate",
        content=b"x" * (main.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------
def test_timeout_wrapper_unit():
    with pytest.raises(service.GenerationTimeout):
        service.run_with_timeout(lambda: time.sleep(2), timeout=0.05)
    assert service.run_with_timeout(lambda: 7, timeout=5) == 7


def test_generate_timeout_returns_408(client, monkeypatch):
    monkeypatch.setattr(service, "TIMEOUT_SECONDS", 0.000001)
    r = client.post("/api/generate", json=gen_body())
    assert r.status_code == 408
    assert "time budget" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Malformed / hostile DDL
# ---------------------------------------------------------------------------
def test_malformed_ddl_is_4xx_not_500(client):
    r = client.post("/api/generate", json=gen_body(ddl="CREATE TABLE oops ("))
    assert r.status_code == 422
    assert "parse" in r.json()["detail"].lower()


def test_empty_schema_is_4xx(client):
    r = client.post("/api/generate", json=gen_body(ddl="SELECT 1;"))
    assert r.status_code == 422


def test_unknown_fk_target_is_4xx(client):
    ddl = "CREATE TABLE a (id SERIAL PRIMARY KEY, b_id INTEGER REFERENCES b(id));"
    r = client.post("/api/generate", json=gen_body(ddl=ddl))
    assert r.status_code == 422
    assert "unknown table" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------
def test_waitlist_add_dedupe_count(client):
    r = client.post("/api/waitlist", json={"email": "Dev@Example.COM"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "added": True, "count": 1}

    # dedupe (case-insensitive)
    r = client.post("/api/waitlist", json={"email": "dev@example.com"})
    assert r.json() == {"ok": True, "added": False, "count": 1}

    r = client.post("/api/waitlist", json={"email": "second@example.com"})
    assert r.json()["count"] == 2

    r = client.get("/api/waitlist/count")
    assert r.json() == {"count": 2}


def test_waitlist_persisted_as_jsonl(client):
    client.post("/api/waitlist", json={"email": "persist@example.com"})
    line = main.waitlist.path.read_text(encoding="utf-8").strip()
    assert '"email": "persist@example.com"' in line
    assert '"ts":' in line and '"ua":' in line


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "x @y.com", "", "a@@b.com"])
def test_waitlist_rejects_invalid_email(client, bad):
    r = client.post("/api/waitlist", json={"email": bad})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
def test_rate_limit_429_after_10(client):
    # Every /api/generate attempt counts (even invalid ones), so use a cheap
    # malformed body to exercise the window quickly.
    for i in range(main.RATE_LIMIT):
        r = client.post("/api/generate", json=gen_body(ddl="CREATE TABLE oops ("))
        assert r.status_code == 422, f"request {i} unexpectedly {r.status_code}"
    r = client.post("/api/generate", json=gen_body())
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "Rate limit" in r.json()["detail"]


def test_rate_limit_does_not_hit_other_endpoints(client):
    for _ in range(main.RATE_LIMIT + 2):
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/waitlist/count").status_code == 200


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/api/health"])
def test_security_headers_present(client, path):
    r = client.get(path)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_no_cors_headers_emitted(client):
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
