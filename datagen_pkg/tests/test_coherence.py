"""Coherence-layer tests (see datagen/coherence.py and COHERENCE_DESIGN.md).

Covers the review-report realism defects P11 (updated_at < created_at),
P12 (child rows older than their parent), P16 (self-referencing FK cycles),
plus correlated pools, determinism of the whole coherence output, and a
clean validator run on the hard fixture with coherence active.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from datagen.ddl_parser import parse_ddl
from datagen.engine import EngineConfig, run_engine
from datagen.validator import validate

FIXTURES_DIR = Path(__file__).parent / "fixtures"
POOLS_DIR = Path(__file__).parent.parent / "datagen" / "pools"


def _dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime(v.year, v.month, v.day)


# ---------------------------------------------------------------------------
# a. Cross-column chronology (P11)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(1, 6))
def test_updated_at_never_before_created_at(seed):
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      created_at TIMESTAMP NOT NULL,
      updated_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 50}, EngineConfig(seed=seed, null_rate=0.0))["t"]
    bad = [i for i, r in enumerate(rows) if r["updated_at"] < r["created_at"]]
    assert not bad, f"seed {seed}: updated_at < created_at at rows {bad}"


def test_some_rows_are_never_updated():
    # Real apps have untouched rows: a fraction must have updated == created.
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL);"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 100}, EngineConfig(seed=1, null_rate=0.0))["t"]
    untouched = sum(1 for r in rows if r["updated_at"] == r["created_at"])
    assert untouched > 0, "expected some rows with updated_at == created_at"
    assert untouched < 100, "expected some rows with updated_at > created_at"


# ---------------------------------------------------------------------------
# b. Cross-table chronology (P12)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(1, 6))
def test_child_created_after_parent(seed):
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL);
    CREATE TABLE orders (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id),
      created_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    g = run_engine(schema, {"users": 8, "orders": 60}, EngineConfig(seed=seed, null_rate=0.0))
    ucreated = {r["id"]: r["created_at"] for r in g["users"]}
    bad = [i for i, r in enumerate(g["orders"]) if r["created_at"] < ucreated[r["user_id"]]]
    assert not bad, f"seed {seed}: orders created before their user at rows {bad}"


def test_multi_fk_child_respects_max_of_parent_floors():
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL);
    CREATE TABLE listings (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL);
    CREATE TABLE bids (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id),
      listing_id INT NOT NULL REFERENCES listings(id),
      created_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    g = run_engine(schema, {"users": 6, "listings": 6, "bids": 60}, EngineConfig(seed=7, null_rate=0.0))
    ucreated = {r["id"]: r["created_at"] for r in g["users"]}
    lcreated = {r["id"]: r["created_at"] for r in g["listings"]}
    for i, r in enumerate(g["bids"]):
        floor = max(ucreated[r["user_id"]], lcreated[r["listing_id"]])
        assert r["created_at"] >= floor, (
            f"bid {i} created {r['created_at']} before max(parent floors) {floor}"
        )


def test_nullable_fk_contributes_no_floor_and_stays_valid():
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL);
    CREATE TABLE notes (
      id SERIAL PRIMARY KEY,
      user_id INT REFERENCES users(id),
      created_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    g = run_engine(
        schema, {"users": 5, "notes": 60},
        EngineConfig(seed=3, null_rate=0.0, fk_null_rate=0.5),
    )
    ucreated = {r["id"]: r["created_at"] for r in g["users"]}
    with_null = [r for r in g["notes"] if r["user_id"] is None]
    assert with_null, "expected some NULL FK picks at fk_null_rate=0.5"
    for i, r in enumerate(g["notes"]):
        if r["user_id"] is not None:
            assert r["created_at"] >= ucreated[r["user_id"]], f"notes row {i} older than its user"
    assert validate(schema, g, seed=3).violations_found == 0


def test_deferred_cycle_broken_fk_respects_parent_anchor():
    # departments.head_employee_id is the cycle-broken (deferred) FK; the
    # backfill must only pick staff whose anchor is <= the department's.
    ddl = """
    CREATE TABLE departments (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      head_employee_id INT,
      created_at TIMESTAMP NOT NULL
    );
    CREATE TABLE staff (
      id SERIAL PRIMARY KEY,
      full_name VARCHAR(100) NOT NULL,
      department_id INT,
      created_at TIMESTAMP NOT NULL,
      CONSTRAINT fk_dept FOREIGN KEY (department_id) REFERENCES departments(id)
    );
    ALTER TABLE departments ADD CONSTRAINT fk_head FOREIGN KEY (head_employee_id) REFERENCES staff(id);
    """
    schema = parse_ddl(ddl)
    for seed in range(1, 6):
        g = run_engine(schema, {"departments": 6, "staff": 40}, EngineConfig(seed=seed, null_rate=0.0))
        screated = {r["id"]: r["created_at"] for r in g["staff"]}
        for i, r in enumerate(g["departments"]):
            if r["head_employee_id"] is not None:
                assert screated[r["head_employee_id"]] <= r["created_at"], (
                    f"seed {seed}: department {i} headed by staff hired after the department's anchor"
                )
        assert validate(schema, g, seed=seed).violations_found == 0


# ---------------------------------------------------------------------------
# c. Hierarchy realism (P16): self-referencing FKs form a forest
# ---------------------------------------------------------------------------

def _find_cycles(rows, pk="id", parent_col="manager_id"):
    parent = {r[pk]: r[parent_col] for r in rows}
    cycles = set()
    for start in parent:
        seen = []
        cur = start
        while cur is not None and cur not in seen:
            seen.append(cur)
            cur = parent.get(cur)
        if cur is not None:
            cycles.add(tuple(sorted(seen[seen.index(cur):])))
    return cycles


@pytest.mark.parametrize("seed", range(1, 11))
def test_self_referencing_fk_is_a_forest(seed):
    ddl = """
    CREATE TABLE employees (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      manager_id INT REFERENCES employees(id)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"employees": 200}, EngineConfig(seed=seed))["employees"]
    cycles = _find_cycles(rows)
    assert not cycles, f"seed {seed}: manager cycles found: {sorted(cycles)[:3]}"
    ids = {r["id"] for r in rows}
    for r in rows:
        if r["manager_id"] is not None:
            assert r["manager_id"] in ids and r["manager_id"] != r["id"]


def test_category_tree_parent_is_a_forest_and_chronological():
    ddl = """
    CREATE TABLE categories (
      id SERIAL PRIMARY KEY,
      name VARCHAR(50) NOT NULL,
      parent_id INT REFERENCES categories(id),
      created_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"categories": 80}, EngineConfig(seed=4, null_rate=0.0))["categories"]
    assert not _find_cycles(rows, parent_col="parent_id")
    created = {r["id"]: r["created_at"] for r in rows}
    for i, r in enumerate(rows):
        if r["parent_id"] is not None:
            assert created[r["parent_id"]] <= r["created_at"], (
                f"category {i} exists before its parent category"
            )


def test_root_fraction_knob():
    ddl = """
    CREATE TABLE employees (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      manager_id INT REFERENCES employees(id)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"employees": 50}, EngineConfig(seed=2, root_fraction=1.0))["employees"]
    assert all(r["manager_id"] is None for r in rows), "root_fraction=1.0 must make every row a root"
    rows = run_engine(schema, {"employees": 50}, EngineConfig(seed=2, root_fraction=0.0))["employees"]
    assert rows[0]["manager_id"] is None, "row 0 is always a root"
    assert any(r["manager_id"] is not None for r in rows[1:])


# ---------------------------------------------------------------------------
# d. Correlated pools
# ---------------------------------------------------------------------------

def _pool_records(name):
    return json.loads((POOLS_DIR / f"{name}.json").read_text(encoding="utf-8"))["records"]


def test_geo_columns_are_mutually_consistent():
    ddl = """
    CREATE TABLE addresses (
      id SERIAL PRIMARY KEY,
      city VARCHAR(60) NOT NULL,
      state VARCHAR(60) NOT NULL,
      country VARCHAR(60) NOT NULL,
      zip VARCHAR(20) NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"addresses": 60}, EngineConfig(seed=6, null_rate=0.0))["addresses"]
    allowed = {
        (r["city"], r["state"], r["country"], r["zipcode"]) for r in _pool_records("geo")
    }
    for i, r in enumerate(rows):
        assert (r["city"], r["state"], r["country"], r["zip"]) in allowed, (
            f"addresses row {i}: ({r['city']}, {r['state']}, {r['country']}, {r['zip']}) "
            f"is not an internally-consistent geo record"
        )


def test_first_name_matches_gender():
    ddl = """
    CREATE TABLE people (
      id SERIAL PRIMARY KEY,
      first_name VARCHAR(40) NOT NULL,
      gender VARCHAR(20) NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"people": 60}, EngineConfig(seed=8, null_rate=0.0))["people"]
    allowed = {(r["first_name"], r["gender"]) for r in _pool_records("person")}
    for i, r in enumerate(rows):
        assert (r["first_name"], r["gender"]) in allowed, (
            f"people row {i}: name {r['first_name']!r} does not match gender {r['gender']!r}"
        )


def test_product_name_matches_category_tier_and_price_band():
    ddl = """
    CREATE TABLE catalog (
      id SERIAL PRIMARY KEY,
      product_name VARCHAR(80) NOT NULL,
      category VARCHAR(40) NOT NULL,
      tier VARCHAR(10) NOT NULL,
      price NUMERIC(8,2) NOT NULL CHECK (price > 0)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"catalog": 50}, EngineConfig(seed=9, null_rate=0.0))["catalog"]
    by_name = {r["product_name"]: r for r in _pool_records("products")}
    for i, r in enumerate(rows):
        rec = by_name.get(r["product_name"])
        assert rec is not None, f"catalog row {i}: unknown product {r['product_name']!r}"
        assert r["category"] == rec["category"], f"catalog row {i}: category mismatch"
        assert r["tier"] == rec["tier"], f"catalog row {i}: tier mismatch"
        assert rec["price_min"] <= float(r["price"]) <= rec["price_max"], (
            f"catalog row {i}: price {r['price']} outside {rec['price_min']}..{rec['price_max']}"
        )
    assert validate(schema, run_engine(schema, {"catalog": 50}, EngineConfig(seed=9, null_rate=0.0)), seed=9).violations_found == 0


def test_unique_pool_column_falls_back_when_pool_too_small():
    # 80 rows > 50 geo records on a UNIQUE city column: the pool group must be
    # dropped (Faker + uniqueness instead), never DomainExhaustedError.
    ddl = """
    CREATE TABLE cities (
      id SERIAL PRIMARY KEY,
      city VARCHAR(60) UNIQUE NOT NULL,
      country VARCHAR(60) NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    with pytest.warns(UserWarning, match="Pool 'geo' skipped"):
        g = run_engine(schema, {"cities": 80}, EngineConfig(seed=2, null_rate=0.0))
    names = [r["city"] for r in g["cities"]]
    assert len(set(names)) == 80
    assert validate(schema, g, seed=2).violations_found == 0


def test_user_pool_directory_replaces_packaged_pool(tmp_path):
    custom = {
        "name": "geo",
        "match": ["city", "state", "country", "zipcode"],
        "records": [
            {"city": "Gotham", "state": "New Jersey", "country": "United States", "zipcode": "07001"},
            {"city": "Metropolis", "state": "Kansas", "country": "United States", "zipcode": "66002"},
        ],
    }
    (tmp_path / "geo.json").write_text(json.dumps(custom), encoding="utf-8")
    ddl = """
    CREATE TABLE offices (
      id SERIAL PRIMARY KEY,
      city VARCHAR(60) NOT NULL,
      state VARCHAR(60) NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    cfg = EngineConfig(seed=4, null_rate=0.0, pool_dirs=[str(tmp_path)])
    rows = run_engine(schema, {"offices": 20}, cfg)["offices"]
    allowed = {(r["city"], r["state"]) for r in custom["records"]}
    for r in rows:
        assert (r["city"], r["state"]) in allowed, f"user pool not used: {r}"


def test_enum_column_is_never_pool_bound():
    # `plan` matches the tier hint, but the CHECK IN (...) domain must win.
    ddl = """
    CREATE TABLE subs (
      id SERIAL PRIMARY KEY,
      plan VARCHAR(10) NOT NULL CHECK (plan IN ('free', 'pro', 'team'))
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"subs": 30}, EngineConfig(seed=5, null_rate=0.0))["subs"]
    assert all(r["plan"] in {"free", "pro", "team"} for r in rows)


# ---------------------------------------------------------------------------
# Determinism of the whole coherence output
# ---------------------------------------------------------------------------

COHERENT_SCHEMA = """
CREATE TABLE organizations (
  id UUID PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  city VARCHAR(60),
  state VARCHAR(60),
  country VARCHAR(60),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);
CREATE TABLE employees (
  id SERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  first_name VARCHAR(40) NOT NULL,
  gender VARCHAR(20),
  manager_id INT REFERENCES employees(id),
  is_active BOOLEAN NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);
CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  product_name VARCHAR(80) NOT NULL,
  category VARCHAR(40) NOT NULL,
  price NUMERIC(8,2) NOT NULL CHECK (price > 0),
  created_at TIMESTAMP NOT NULL
);
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  employee_id INT NOT NULL REFERENCES employees(id),
  product_id INT NOT NULL REFERENCES products(id),
  status VARCHAR(12) NOT NULL CHECK (status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled')),
  shipped_at TIMESTAMP,
  delivered_at TIMESTAMP,
  cancelled_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP
);
"""

COHERENT_ROWS = {"organizations": 5, "employees": 25, "products": 20, "orders": 60}


@pytest.mark.parametrize("seed", [1, 42])
def test_coherence_output_is_deterministic(seed):
    schema = parse_ddl(COHERENT_SCHEMA)
    run1 = run_engine(schema, COHERENT_ROWS, EngineConfig(seed=seed))
    run2 = run_engine(parse_ddl(COHERENT_SCHEMA), COHERENT_ROWS, EngineConfig(seed=seed))
    assert run1 == run2, "same seed + config must produce deep-equal output with coherence on"


def test_coherent_schema_full_stack():
    schema = parse_ddl(COHERENT_SCHEMA)
    g = run_engine(schema, COHERENT_ROWS, EngineConfig(seed=11, null_rate=0.0))
    report = validate(schema, g, seed=11)
    assert report.violations_found == 0, report.summary()
    assert not [w for w in report.warnings if w.startswith("coherence:")], report.warnings

    # Status gates: lifecycle cells beyond the row's status stay NULL, filled
    # ones form an ordered chain after created_at.
    for i, r in enumerate(g["orders"]):
        chain = [r["created_at"]]
        for col in ("shipped_at", "delivered_at", "cancelled_at"):
            if r[col] is not None:
                chain.append(r[col])
        assert chain == sorted(chain), f"orders row {i}: lifecycle chain out of order"
        if r["status"] == "pending":
            assert r["shipped_at"] is None and r["delivered_at"] is None
        if r["status"] == "shipped":
            assert r["shipped_at"] is not None and r["delivered_at"] is None
        if r["status"] == "delivered":
            assert r["shipped_at"] is not None and r["delivered_at"] is not None
        if r["status"] == "cancelled":
            assert r["cancelled_at"] is not None
            assert r["shipped_at"] is None and r["delivered_at"] is None


def test_no_coherence_flag_restores_plain_behaviour():
    schema = parse_ddl(COHERENT_SCHEMA)
    g = run_engine(schema, COHERENT_ROWS, EngineConfig(seed=3, coherence=False))
    report = validate(schema, g, seed=3)
    assert report.violations_found == 0, report.summary()
    run2 = run_engine(parse_ddl(COHERENT_SCHEMA), COHERENT_ROWS, EngineConfig(seed=3, coherence=False))
    assert g == run2, "coherence=False must still be deterministic"


# ---------------------------------------------------------------------------
# The hard fixture stays constraint-clean with coherence active
# ---------------------------------------------------------------------------

def test_hard_fixture_validates_clean_with_coherence():
    schema = parse_ddl((FIXTURES_DIR / "hard.sql").read_text(encoding="utf-8"))
    rows = {
        "organizations": 4, "users": 30, "categories": 6, "products": 30,
        "inventory": 40, "coupons": 12, "subscriptions": 40, "orders": 40,
        "order_items": 80, "audit_log": 50,
    }
    g = run_engine(schema, rows, EngineConfig(seed=42, null_rate=0.2))
    report = validate(schema, g, seed=42)
    assert report.violations_found == 0, report.summary()
    assert not [w for w in report.warnings if w.startswith("coherence:")], report.warnings

    # Spot-check the headline properties on the hard fixture itself.
    org_created = {r["id"]: r["created_at"] for r in g["organizations"]}
    for r in g["users"]:
        assert r["created_at"] >= org_created[r["org_id"]]
        if r["updated_at"] is not None:
            assert r["updated_at"] >= r["created_at"]
    assert not _find_cycles(g["users"], parent_col="referred_by")
