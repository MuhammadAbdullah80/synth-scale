"""Regression tests for the defects in REVIEW_REPORT.md, one per fix.

Each test targets a single defect number from the report so a regression
points straight at the code that reintroduced it. These deliberately use
minimal single-purpose DDL rather than the shared fixtures.
"""
from __future__ import annotations

import datetime as _dt
import uuid as _uuid
from datetime import date

import pytest

from datagen.ddl_parser import parse_ddl
from datagen.engine import EngineConfig, run_engine
from datagen.generators.base import DomainExhaustedError
from datagen.validator import validate


def _is_plain_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# --------------------------------------------------------------------------
# Defect 1: NUMERIC(p,s) precision was parsed then ignored
# --------------------------------------------------------------------------

def test_numeric_4_2_stays_below_100():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, rate NUMERIC(4,2));"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 30}, EngineConfig(seed=3, null_rate=0.0))["t"]
    for r in rows:
        assert r["rate"] is not None
        assert abs(r["rate"]) < 100, f"NUMERIC(4,2) overflow: {r['rate']}"
        assert round(r["rate"], 2) == r["rate"]


def test_numeric_precision_intersects_check_bounds():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, pct NUMERIC(4,2) CHECK (pct > 0));"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 30}, EngineConfig(seed=3, null_rate=0.0))["t"]
    for r in rows:
        assert 0 < r["pct"] < 100


def test_numeric_infeasible_precision_vs_bound_fails_fast():
    # CHECK requires >= 500 but NUMERIC(4,2) tops out at 99.99.
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, v NUMERIC(4,2) CHECK (v >= 500));"
    schema = parse_ddl(ddl)
    with pytest.raises(DomainExhaustedError):
        run_engine(schema, {"t": 5}, EngineConfig(seed=3, null_rate=0.0))


# --------------------------------------------------------------------------
# Defect 2: two-column CHECK derivation was type-blind (floats into INT
# columns) and could clobber the PK
# --------------------------------------------------------------------------

def test_int_two_column_check_yields_ints():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      min_qty INT NOT NULL,
      max_qty INT NOT NULL,
      CONSTRAINT chk CHECK (max_qty > min_qty)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 25}, EngineConfig(seed=5, null_rate=0.0))["t"]
    for r in rows:
        assert _is_plain_int(r["min_qty"]) and _is_plain_int(r["max_qty"])
        assert r["max_qty"] > r["min_qty"]


def test_pk_in_check_stays_sequential_ints():
    ddl = "CREATE TABLE t (id INT PRIMARY KEY, lo INT NOT NULL, CONSTRAINT chk CHECK (id > lo));"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 10}, EngineConfig(seed=5, null_rate=0.0))["t"]
    ids = [r["id"] for r in rows]
    assert ids == list(range(1, 11)), f"PK no longer sequential: {ids}"
    for r in rows:
        assert _is_plain_int(r["lo"])
        assert r["id"] > r["lo"]


def test_numeric_two_column_check_respects_scale():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      low_price NUMERIC(6,2) NOT NULL,
      high_price NUMERIC(6,2) NOT NULL,
      CONSTRAINT chk CHECK (high_price > low_price)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 25}, EngineConfig(seed=5, null_rate=0.0))["t"]
    for r in rows:
        assert r["high_price"] > r["low_price"]
        assert round(r["high_price"], 2) == r["high_price"]
        assert abs(r["high_price"]) < 10_000  # NUMERIC(6,2) cap


# --------------------------------------------------------------------------
# Defect 3: NOT NULL dependent column inherited NULLs from a nullable base
# --------------------------------------------------------------------------

def test_not_null_dependent_column_has_no_nulls():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      start_date DATE,
      end_date DATE NOT NULL,
      CONSTRAINT chk CHECK (end_date > start_date)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 40}, EngineConfig(seed=5, null_rate=0.3))["t"]
    assert all(r["end_date"] is not None for r in rows)
    for r in rows:
        if r["start_date"] is not None:
            assert r["end_date"] > r["start_date"]
    report = validate(schema, run_engine(schema, {"t": 40}, EngineConfig(seed=5, null_rate=0.3)), seed=5)
    assert report.violations_found == 0, report.summary()


# --------------------------------------------------------------------------
# Defect 4: date CHECK bounds (BETWEEN crash / >= silently dropped / dead
# min_date-max_date keys)
# --------------------------------------------------------------------------

def test_date_between_check_respected_without_crash():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      d DATE CHECK (d BETWEEN '2020-01-01' AND '2020-12-31')
    );
    """
    schema = parse_ddl(ddl)  # unfixed engine raised ValueError here-abouts
    rows = run_engine(schema, {"t": 20}, EngineConfig(seed=3, null_rate=0.0))["t"]
    for r in rows:
        assert date(2020, 1, 1) <= r["d"] <= date(2020, 12, 31), r["d"]


def test_date_lower_bound_check_respected():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, d DATE CHECK (d >= '2030-01-01'));"
    schema = parse_ddl(ddl)
    parsed = [c.parsed for c in schema.tables["t"].check_constraints]
    assert parsed and parsed[0] is not None, "date >= literal must be structurally parsed, not dropped"
    generated = run_engine(schema, {"t": 20}, EngineConfig(seed=3, null_rate=0.0))
    for r in generated["t"]:
        assert r["d"] >= date(2030, 1, 1), r["d"]
    report = validate(schema, generated, seed=3)
    assert report.violations_found == 0, report.summary()


def test_strict_date_inequality_respected():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, d DATE CHECK (d > '2031-06-15'));"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 20}, EngineConfig(seed=3, null_rate=0.0))["t"]
    for r in rows:
        assert r["d"] > date(2031, 6, 15), r["d"]


# --------------------------------------------------------------------------
# Defect 5: composite UNIQUE mixing FK + non-FK columns was unenforced
# --------------------------------------------------------------------------

def test_mixed_composite_unique_has_no_duplicates():
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(120) UNIQUE NOT NULL);
    CREATE TABLE posts (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id),
      slug VARCHAR(10) NOT NULL,
      UNIQUE (user_id, slug)
    );
    """
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"users": 2, "posts": 40}, EngineConfig(seed=5, null_rate=0.0))
    keys = [(r["user_id"], r["slug"]) for r in generated["posts"]]
    assert len(keys) == len(set(keys)), "duplicate (user_id, slug) pairs"
    user_ids = {r["id"] for r in generated["users"]}
    assert all(k[0] in user_ids for k in keys), "FK member of the tuple must come from the parent pool"
    report = validate(schema, generated, seed=5)
    assert report.violations_found == 0, report.summary()


def test_all_plain_composite_unique_still_enforced():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      a INT NOT NULL,
      b INT NOT NULL,
      UNIQUE (a, b)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 50}, EngineConfig(seed=5, null_rate=0.0))["t"]
    keys = [(r["a"], r["b"]) for r in rows]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------
# Defect 6: check_eval fallback was never wired into generation
# --------------------------------------------------------------------------

def test_unparsed_single_column_check_satisfied():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, score INT NOT NULL CHECK (score % 3 = 0));"
    schema = parse_ddl(ddl)
    assert schema.tables["t"].check_constraints[0].parsed is None  # really unparsed
    rows = run_engine(schema, {"t": 30}, EngineConfig(seed=5, null_rate=0.0))["t"]
    for r in rows:
        assert r["score"] % 3 == 0, r["score"]


def test_unparsed_multi_column_check_satisfied():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      a INT NOT NULL,
      b INT NOT NULL,
      CONSTRAINT chk CHECK (a + b < 1200)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 40}, EngineConfig(seed=5, null_rate=0.0))["t"]
    for r in rows:
        assert r["a"] + r["b"] < 1200, (r["a"], r["b"])


def test_unparsed_check_exhaustion_raises_clear_error():
    # a, b default to [1, 1001]; a + b < 3 is only (1, 1) -- effectively
    # infeasible, so the bounded retry loop must fail fast, not emit bad rows.
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      a INT NOT NULL,
      b INT NOT NULL,
      CONSTRAINT chk CHECK (a + b < 3)
    );
    """
    schema = parse_ddl(ddl)
    with pytest.raises(DomainExhaustedError):
        run_engine(schema, {"t": 20}, EngineConfig(seed=5, null_rate=0.0))


# --------------------------------------------------------------------------
# Defect 7: UUID PKs were not seed-deterministic
# --------------------------------------------------------------------------

def test_uuid_pk_deterministic_across_runs():
    ddl = "CREATE TABLE accounts (id UUID PRIMARY KEY, email VARCHAR(120) UNIQUE NOT NULL);"
    ids1 = [r["id"] for r in run_engine(parse_ddl(ddl), {"accounts": 5}, EngineConfig(seed=7))["accounts"]]
    ids2 = [r["id"] for r in run_engine(parse_ddl(ddl), {"accounts": 5}, EngineConfig(seed=7))["accounts"]]
    assert ids1 == ids2, "same seed must reproduce identical UUIDs"
    assert len(set(ids1)) == 5
    for v in ids1:
        assert _uuid.UUID(v).version == 4  # still a well-formed v4 UUID


# --------------------------------------------------------------------------
# Defect 8: date.today() wall-clock dependence
# --------------------------------------------------------------------------

def test_same_seed_and_config_identical_even_on_another_day(monkeypatch):
    import datagen.generators.date_time as dt_mod

    ddl = """
    CREATE TABLE evt (
      id UUID PRIMARY KEY,
      happened_on DATE NOT NULL,
      logged_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    cfg = EngineConfig(seed=13)
    before = run_engine(schema, {"evt": 10}, cfg)

    class ShiftedDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date.today() + _dt.timedelta(days=500)

    monkeypatch.setattr(dt_mod, "date", ShiftedDate, raising=False)
    after = run_engine(schema, {"evt": 10}, EngineConfig(seed=13))
    assert before == after, "output must not depend on the wall clock"


def test_as_of_moves_the_window_deterministically():
    ddl = "CREATE TABLE evt (id SERIAL PRIMARY KEY, happened_on DATE NOT NULL);"
    schema = parse_ddl(ddl)
    shifted_cfg = EngineConfig(seed=13, as_of=date(2031, 6, 1))
    run_a = run_engine(schema, {"evt": 10}, shifted_cfg)
    run_b = run_engine(schema, {"evt": 10}, EngineConfig(seed=13, as_of=date(2031, 6, 1)))
    assert run_a == run_b, "same seed + same as_of must be identical"
    default_run = run_engine(schema, {"evt": 10}, EngineConfig(seed=13))
    assert run_a != default_run, "as_of must actually move the generation window"
    for r in run_a["evt"]:
        assert r["happened_on"] <= date(2031, 6, 1)


# --------------------------------------------------------------------------
# Defect 9b: enum membership independently re-validated
# --------------------------------------------------------------------------

def test_validator_catches_bad_enum_value():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      status VARCHAR(20) CHECK (status IN ('a','b'))
    );
    """
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"t": 10}, EngineConfig(seed=5, null_rate=0.0))
    assert validate(schema, generated, seed=5).violations_found == 0
    generated["t"][3]["status"] = "zzz"  # inject a value outside the enum
    report = validate(schema, generated, seed=5)
    assert report.violations_found >= 1
    assert any("enum" in e for e in report.errors)


# --------------------------------------------------------------------------
# Defect 9c: type-conformance validation
# --------------------------------------------------------------------------

def test_validator_catches_float_in_int_column():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, qty INT NOT NULL);"
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"t": 10}, EngineConfig(seed=5))
    assert validate(schema, generated, seed=5).violations_found == 0
    generated["t"][0]["qty"] = 7.5  # inject the exact fallout of defect #2
    report = validate(schema, generated, seed=5)
    assert report.violations_found >= 1
    assert any("type mismatch" in e for e in report.errors)


def test_validator_catches_string_in_date_column_and_long_varchar():
    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, d DATE NOT NULL, name VARCHAR(5) NOT NULL);"
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"t": 10}, EngineConfig(seed=5))
    assert validate(schema, generated, seed=5).violations_found == 0
    generated["t"][1]["d"] = "2020-01-01"          # string, not a date object
    generated["t"][2]["name"] = "way too long"     # exceeds VARCHAR(5)
    report = validate(schema, generated, seed=5)
    assert report.violations_found >= 2
    assert any("type mismatch" in e for e in report.errors)


def test_validator_zero_violations_on_clean_hard_case_combo():
    """Aggregate guard: the fixed engine yields zero violations on a schema
    combining every previously-broken construct."""
    ddl = """
    CREATE TABLE orgs (id UUID PRIMARY KEY, slug VARCHAR(20) UNIQUE NOT NULL);
    CREATE TABLE items (
      id UUID PRIMARY KEY,
      org_id UUID NOT NULL REFERENCES orgs(id),
      sku VARCHAR(8) NOT NULL,
      rate NUMERIC(4,2) NOT NULL CHECK (rate >= 0),
      starts_on DATE CHECK (starts_on BETWEEN '2024-01-01' AND '2025-12-31'),
      min_qty INT NOT NULL,
      max_qty INT NOT NULL,
      UNIQUE (org_id, sku),
      CONSTRAINT chk CHECK (max_qty > min_qty)
    );
    """
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"orgs": 3, "items": 40}, EngineConfig(seed=9, null_rate=0.2))
    report = validate(schema, generated, seed=9)
    assert report.violations_found == 0, report.summary()
