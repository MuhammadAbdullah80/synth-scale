"""Adversarial hardening suite: measures the ENGINE, not the fixture.

Every test in this file encodes the product guarantee:

    "Generated data loads into Postgres with zero constraint violations,
     zero type mismatches, and the same seed+config produces byte-identical
     output every run, any day."

Tests are written against CORRECT behaviour. Several are expected to FAIL
against the unfixed engine -- deliberately, with no xfail/skip markers. Each
test carries a `Review defect #N` note mapping it to REVIEW_REPORT.md.

Fixture: tests/fixtures/hard.sql -- a ~10-table Supabase-style schema that
contains every hard case the original sample.sql avoided (UUID PKs,
NUMERIC(4,2)/(6,2), date CHECKs incl. BETWEEN, mixed composite UNIQUE,
INT two-column CHECK, NOT-NULL-dependent CHECK column, enum CHECK IN,
junction table, self-referencing FK, tight VARCHARs, exotic CHECK).
"""
from __future__ import annotations

import csv
import re
import uuid as uuid_module
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from datagen.ddl_parser import parse_ddl
from datagen.dependency_graph import build_generation_plan
from datagen.engine import EngineConfig, run_engine
from datagen.generators.base import DomainExhaustedError
from datagen.schema_model import DataType
from datagen.output.csv_writer import write_csv
from datagen.output.sql_writer import write_sql
from datagen.validator import validate

FIXTURES_DIR = Path(__file__).parent / "fixtures"
HARD_SQL = (FIXTURES_DIR / "hard.sql").read_text(encoding="utf-8")

INT_TYPES = (DataType.INTEGER, DataType.BIGINT, DataType.SMALLINT)

HARD_ROWS = {
    "organizations": 4,
    "users": 30,
    "categories": 6,
    "products": 30,
    "inventory": 40,
    "coupons": 12,
    "subscriptions": 40,
    "orders": 40,
    "order_items": 80,
    "audit_log": 50,
}

# Smaller counts for the multi-seed property test.
SMALL_ROWS = {
    "organizations": 3,
    "users": 10,
    "categories": 4,
    "products": 10,
    "inventory": 12,
    "coupons": 6,
    "subscriptions": 12,
    "orders": 12,
    "order_items": 20,
    "audit_log": 12,
}

HARD_SEED = 42
# null_rate raised above the 0.05 default so nullable partners of NOT NULL
# CHECK columns (subscriptions.trial_start) reliably contain NULLs, and so the
# SQL/CSV NULL-rendering assertions actually see NULLs.
HARD_CONFIG = EngineConfig(seed=HARD_SEED, null_rate=0.2)


def _is_plain_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _decimal_places(v) -> int:
    exp = Decimal(str(v)).normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


# ---------------------------------------------------------------------------
# Module fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hard_schema():
    return parse_ddl(HARD_SQL)


@pytest.fixture(scope="module")
def hard_generated(hard_schema):
    # NOTE: on the unfixed engine this raises ValueError (float('2024-01-01'))
    # from the date-BETWEEN CHECK -- review defect #4a. Every test depending on
    # this fixture then errors; that IS the red bar until #4 is fixed.
    return run_engine(hard_schema, HARD_ROWS, HARD_CONFIG)


# ---------------------------------------------------------------------------
# 1. End-to-end: parse + generate + validate the hard fixture
# ---------------------------------------------------------------------------

def test_hard_fixture_parses_all_tables(hard_schema):
    assert set(hard_schema.tables) == set(HARD_ROWS)


def test_hard_fixture_row_counts(hard_generated):
    for t, n in HARD_ROWS.items():
        assert len(hard_generated[t]) == n


def test_hard_fixture_validator_zero_violations(hard_schema, hard_generated):
    """Product guarantee: zero constraint violations on the hard schema.
    Exposes review defects #1-#6 wherever the validator can see them."""
    report = validate(hard_schema, hard_generated, seed=HARD_SEED)
    assert report.violations_found == 0, report.summary()


# ---------------------------------------------------------------------------
# 2. Type conformance, asserted directly (never trusting the validator)
# ---------------------------------------------------------------------------

def test_int_columns_contain_only_ints(hard_schema, hard_generated):
    """Review defect #2: two-column CHECK derivation writes floats into INT
    columns. An INT column must contain Python ints (or None if nullable)."""
    bad = []
    for tname, rows in hard_generated.items():
        for col in hard_schema.tables[tname].columns:
            if col.dtype not in INT_TYPES:
                continue
            for i, row in enumerate(rows):
                v = row[col.name]
                if v is None:
                    continue
                if not _is_plain_int(v):
                    bad.append(f"{tname}.{col.name} row {i}: {v!r} ({type(v).__name__})")
    assert not bad, "non-int values in INT columns:\n" + "\n".join(bad[:20])


def test_numeric_columns_fit_precision_and_scale(hard_schema, hard_generated):
    """Review defect #1: NUMERIC(p,s) must satisfy abs(v) < 10**(p-s) and have
    at most s decimal places, or Postgres raises 'numeric field overflow'."""
    bad = []
    for tname, rows in hard_generated.items():
        for col in hard_schema.tables[tname].columns:
            if col.dtype != DataType.NUMERIC or col.precision is None:
                continue
            scale = col.scale or 0
            limit = 10 ** (col.precision - scale)
            for i, row in enumerate(rows):
                v = row[col.name]
                if v is None:
                    continue
                if abs(float(v)) >= limit:
                    bad.append(f"{tname}.{col.name} row {i}: {v!r} overflows NUMERIC({col.precision},{scale})")
                elif _decimal_places(v) > scale:
                    bad.append(f"{tname}.{col.name} row {i}: {v!r} has more than {scale} decimal places")
    assert not bad, "NUMERIC precision/scale violations:\n" + "\n".join(bad[:20])


def test_varchar_lengths_respected(hard_schema, hard_generated):
    bad = []
    for tname, rows in hard_generated.items():
        for col in hard_schema.tables[tname].columns:
            if col.dtype != DataType.VARCHAR or not col.max_length:
                continue
            for i, row in enumerate(rows):
                v = row[col.name]
                if v is not None and len(str(v)) > col.max_length:
                    bad.append(f"{tname}.{col.name} row {i}: len {len(str(v))} > {col.max_length}")
    assert not bad, "VARCHAR length violations:\n" + "\n".join(bad[:20])


def test_date_columns_contain_dates(hard_schema, hard_generated):
    for tname, rows in hard_generated.items():
        for col in hard_schema.tables[tname].columns:
            if col.dtype != DataType.DATE:
                continue
            for i, row in enumerate(rows):
                v = row[col.name]
                if v is None:
                    continue
                assert isinstance(v, date) and not isinstance(v, datetime), (
                    f"{tname}.{col.name} row {i}: {v!r} is not a datetime.date"
                )


def test_timestamp_columns_contain_datetimes(hard_schema, hard_generated):
    for tname, rows in hard_generated.items():
        for col in hard_schema.tables[tname].columns:
            if col.dtype != DataType.TIMESTAMP:
                continue
            for i, row in enumerate(rows):
                v = row[col.name]
                if v is None:
                    continue
                assert isinstance(v, datetime), (
                    f"{tname}.{col.name} row {i}: {v!r} is not a datetime.datetime"
                )


def test_uuid_columns_parse_as_uuids(hard_schema, hard_generated):
    for tname, rows in hard_generated.items():
        for col in hard_schema.tables[tname].columns:
            if col.dtype != DataType.UUID:
                continue
            for i, row in enumerate(rows):
                v = row[col.name]
                if v is None:
                    continue
                try:
                    uuid_module.UUID(str(v))
                except ValueError:
                    pytest.fail(f"{tname}.{col.name} row {i}: {v!r} is not a valid UUID")


# ---------------------------------------------------------------------------
# 3. CHECK-constraint truth, re-evaluated manually from the fixture's DDL
# ---------------------------------------------------------------------------

def test_date_checks_hold(hard_generated):
    """Review defect #4: `>=` date CHECKs silently dropped, BETWEEN crashes."""
    for r in hard_generated["coupons"]:
        assert r["valid_from"] >= date(2024, 1, 1), f"coupons.valid_from {r['valid_from']} < 2024-01-01"
        assert date(2024, 1, 1) <= r["valid_until"] <= date(2026, 12, 31), (
            f"coupons.valid_until {r['valid_until']} outside BETWEEN window"
        )
    for r in hard_generated["orders"]:
        assert r["placed_on"] >= date(2023, 1, 1), f"orders.placed_on {r['placed_on']} < 2023-01-01"


def test_int_two_column_check_holds(hard_generated):
    """Review defect #2: CHECK (max_qty > min_qty) on INT columns."""
    for i, r in enumerate(hard_generated["inventory"]):
        assert r["min_qty"] >= 0, f"inventory row {i}: min_qty {r['min_qty']} < 0"
        assert r["max_qty"] > r["min_qty"], (
            f"inventory row {i}: max_qty {r['max_qty']!r} <= min_qty {r['min_qty']!r}"
        )


def test_not_null_dependent_check_column(hard_generated):
    """Review defect #3: trial_end is NOT NULL but is derived from nullable
    trial_start; NULL must never propagate into it."""
    nulls = [i for i, r in enumerate(hard_generated["subscriptions"]) if r["trial_end"] is None]
    assert not nulls, f"NULL in NOT NULL subscriptions.trial_end at rows {nulls}"
    for i, r in enumerate(hard_generated["subscriptions"]):
        if r["trial_start"] is not None and r["trial_end"] is not None:
            assert r["trial_end"] > r["trial_start"], f"subscriptions row {i}: trial_end <= trial_start"


def test_enum_checks_hold(hard_generated):
    """Review defect #10 territory: enum membership re-checked independently."""
    assert all(r["status"] in {"draft", "active", "archived"} for r in hard_generated["products"])
    assert all(r["plan"] in {"free", "pro", "team"} for r in hard_generated["subscriptions"])
    assert all(r["status"] in {"pending", "paid", "shipped", "refunded"} for r in hard_generated["orders"])


def test_numeric_bound_checks_hold(hard_generated):
    for r in hard_generated["products"]:
        assert r["price"] > 0
        assert r["tax_rate"] >= 0
    for r in hard_generated["coupons"]:
        assert r["percent_off"] > 0
    for r in hard_generated["orders"]:
        assert r["subtotal"] >= 0
        assert r["tax"] >= 0
    for r in hard_generated["subscriptions"]:
        assert r["seats"] > 0
    for r in hard_generated["order_items"]:
        assert r["quantity"] > 0
        assert r["unit_price"] > 0


def test_exotic_check_holds(hard_generated):
    """Review defect #6: the generate-and-filter fallback for CHECKs the
    structural parser can't recognize (quantity * unit_price < 2000000) is
    documented but never wired into generation."""
    bad = [
        i for i, r in enumerate(hard_generated["order_items"])
        if not (r["quantity"] * float(r["unit_price"]) < 2000000)
    ]
    assert not bad, f"order_items CHECK (quantity * unit_price < 2000000) violated at rows {bad}"


def test_mixed_composite_unique_on_hard_fixture(hard_generated):
    """Review defect #5: UNIQUE (org_id, sku) -- FK column + plain column."""
    pairs = [(r["org_id"], r["sku"]) for r in hard_generated["products"]]
    dupes = {p for p in pairs if pairs.count(p) > 1}
    assert not dupes, f"duplicate (org_id, sku) pairs in products: {sorted(map(str, dupes))[:5]}"


def test_self_referencing_fk_valid(hard_generated):
    user_ids = {r["id"] for r in hard_generated["users"]}
    for i, r in enumerate(hard_generated["users"]):
        if r["referred_by"] is not None:
            assert r["referred_by"] in user_ids, f"users row {i}: dangling referred_by"
            assert r["referred_by"] != r["id"], f"users row {i}: refers to itself"


def test_junction_composite_pk_unique_and_valid(hard_generated):
    keys = [(r["order_id"], r["product_id"]) for r in hard_generated["order_items"]]
    assert len(keys) == len(set(keys)), "duplicate composite PK in order_items"
    order_ids = {r["id"] for r in hard_generated["orders"]}
    product_ids = {r["id"] for r in hard_generated["products"]}
    for r in hard_generated["order_items"]:
        assert r["order_id"] in order_ids
        assert r["product_id"] in product_ids


# ---------------------------------------------------------------------------
# 4. Determinism -- same seed, same bytes; different seed, different data
# ---------------------------------------------------------------------------

def test_same_seed_identical_output_including_uuids(hard_schema):
    """Review defect #7: UUID PKs come from unseeded uuid.uuid4(), breaking
    'same seed => byte-identical output'. Deep equality covers every column,
    UUIDs included."""
    run1 = run_engine(hard_schema, HARD_ROWS, EngineConfig(seed=7, null_rate=0.2))
    run2 = run_engine(hard_schema, HARD_ROWS, EngineConfig(seed=7, null_rate=0.2))
    assert run1 == run2, (
        "same seed produced different output; first differing table: "
        + next(t for t in run1 if run1[t] != run2[t])
    )


def test_different_seeds_differ(hard_schema):
    run1 = run_engine(hard_schema, HARD_ROWS, EngineConfig(seed=1, null_rate=0.2))
    run2 = run_engine(hard_schema, HARD_ROWS, EngineConfig(seed=2, null_rate=0.2))
    assert run1 != run2, "two different seeds produced identical output"


def test_uuid_pk_deterministic_small_schema():
    """Targeted probe for review defect #7, isolated from the hard fixture so
    it stays meaningful even while other defects block hard.sql generation."""
    ddl = """
    CREATE TABLE gadgets (
      id UUID PRIMARY KEY,
      name VARCHAR(40) NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    ids1 = [r["id"] for r in run_engine(schema, {"gadgets": 5}, EngineConfig(seed=11))["gadgets"]]
    ids2 = [r["id"] for r in run_engine(schema, {"gadgets": 5}, EngineConfig(seed=11))["gadgets"]]
    assert ids1 == ids2, f"UUID PKs are not seed-deterministic:\n{ids1}\nvs\n{ids2}"


def test_same_seed_stable_across_days(monkeypatch):
    """Review defect #8: date generation anchors to date.today(), so the same
    seed gives different data on a different day. Simulate 'a different day'
    by shifting the clock the date generator sees."""
    import datagen.generators.date_time as dt_mod

    ddl = """
    CREATE TABLE events (
      id SERIAL PRIMARY KEY,
      name VARCHAR(40) NOT NULL,
      happened_on DATE NOT NULL,
      created_at TIMESTAMP NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    before = run_engine(schema, {"events": 10}, EngineConfig(seed=13))

    real_today = date.today()

    class ShiftedDate(date):
        @classmethod
        def today(cls):
            return real_today + timedelta(days=400)

    monkeypatch.setattr(dt_mod, "date", ShiftedDate, raising=False)
    after = run_engine(schema, {"events": 10}, EngineConfig(seed=13))
    assert before == after, "same seed produced different data when run 'on a different day'"


# ---------------------------------------------------------------------------
# 5. Property test across seeds 1..10
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(1, 11))
def test_property_zero_violations_and_no_dangling_fks(hard_schema, seed):
    """For every seed: validator must report zero violations AND every FK must
    resolve against the parent's actual key set, checked manually (the
    validator is not trusted to be the only net)."""
    generated = run_engine(hard_schema, SMALL_ROWS, EngineConfig(seed=seed))
    report = validate(hard_schema, generated, seed=seed)
    assert report.violations_found == 0, f"seed {seed}: {report.summary()}"

    for tname, table in hard_schema.tables.items():
        rows = generated[tname]
        for fk in table.foreign_keys:
            parent_keys = {
                tuple(pr[c] for c in fk.ref_columns) for pr in generated[fk.ref_table]
            }
            for i, row in enumerate(rows):
                key = tuple(row[c] for c in fk.columns)
                if any(v is None for v in key):
                    continue
                assert key in parent_keys, (
                    f"seed {seed}: {tname}.{fk.columns} row {i} -> {key!r} "
                    f"dangling (not in {fk.ref_table}.{fk.ref_columns})"
                )


# ---------------------------------------------------------------------------
# 6. Mixed composite UNIQUE under a deliberately tight domain
# ---------------------------------------------------------------------------

def test_tight_mixed_composite_unique_never_silently_duplicates():
    """Review defect #5. 2 parents x 60 children over a VARCHAR(6) slug:
    the engine must either produce all-unique (author_id, slug) pairs or fail
    fast with DomainExhaustedError. Silent duplicates are never acceptable."""
    ddl = """
    CREATE TABLE authors (
      id SERIAL PRIMARY KEY,
      name VARCHAR(40) NOT NULL
    );
    CREATE TABLE posts (
      id SERIAL PRIMARY KEY,
      author_id INT NOT NULL REFERENCES authors(id),
      slug VARCHAR(6) NOT NULL,
      UNIQUE (author_id, slug)
    );
    """
    schema = parse_ddl(ddl)
    try:
        generated = run_engine(schema, {"authors": 2, "posts": 60}, EngineConfig(seed=5))
    except DomainExhaustedError:
        return  # clean, explicit failure is acceptable
    pairs = [(r["author_id"], r["slug"]) for r in generated["posts"]]
    dupes = sorted({p for p in pairs if pairs.count(p) > 1})
    assert not dupes, f"silent duplicate (author_id, slug) pairs: {dupes[:5]}"


# ---------------------------------------------------------------------------
# 7. Targeted single-defect probes (independent of the hard fixture, so each
#    review defect keeps its own red/green signal)
# ---------------------------------------------------------------------------

def test_numeric_4_2_stays_within_precision():
    """Review defect #1 in isolation: NUMERIC(4,2) => every |v| < 100."""
    ddl = "CREATE TABLE rates (id SERIAL PRIMARY KEY, rate NUMERIC(4,2) NOT NULL);"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"rates": 20}, EngineConfig(seed=3))["rates"]
    bad = [r["rate"] for r in rows if abs(float(r["rate"])) >= 100 or _decimal_places(r["rate"]) > 2]
    assert not bad, f"NUMERIC(4,2) values Postgres would reject: {bad[:8]}"


def test_int_two_column_check_stays_int():
    """Review defect #2 in isolation: derived max_qty must be an int and > min_qty."""
    ddl = """
    CREATE TABLE stock (
      id SERIAL PRIMARY KEY,
      min_qty INT NOT NULL,
      max_qty INT NOT NULL,
      CHECK (max_qty > min_qty)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"stock": 20}, EngineConfig(seed=3))["stock"]
    for i, r in enumerate(rows):
        assert _is_plain_int(r["max_qty"]), f"row {i}: max_qty {r['max_qty']!r} is not an int"
        assert _is_plain_int(r["min_qty"]), f"row {i}: min_qty {r['min_qty']!r} is not an int"
        assert r["max_qty"] > r["min_qty"], f"row {i}: CHECK (max_qty > min_qty) violated"


def test_pk_not_clobbered_by_check_group():
    """Review defect #2 (probe P6): a PK that appears in a two-column CHECK
    must remain a unique INT key, never a derived float."""
    ddl = "CREATE TABLE t (id INT PRIMARY KEY, lo INT NOT NULL, CHECK (id > lo));"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"t": 20}, EngineConfig(seed=3))["t"]
    ids = [r["id"] for r in rows]
    assert all(_is_plain_int(v) for v in ids), f"PK contains non-ints: {ids[:5]}"
    assert len(ids) == len(set(ids)), "PK values are not unique"
    for i, r in enumerate(rows):
        assert r["id"] > r["lo"], f"row {i}: CHECK (id > lo) violated"


def test_not_null_dependent_column_never_null():
    """Review defect #3 in isolation: end_date NOT NULL derived from nullable
    start_date must never inherit NULL."""
    ddl = """
    CREATE TABLE trips (
      id SERIAL PRIMARY KEY,
      start_date DATE,
      end_date DATE NOT NULL,
      CHECK (end_date > start_date)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"trips": 40}, EngineConfig(seed=3, null_rate=0.5))["trips"]
    nulls = [i for i, r in enumerate(rows) if r["end_date"] is None]
    assert not nulls, f"NULL in NOT NULL trips.end_date at rows {nulls}"


def test_date_check_ge_literal_enforced():
    """Review defect #4b in isolation: CHECK (d >= '2030-01-01') must bound
    generation, not silently vanish."""
    ddl = "CREATE TABLE promos (id SERIAL PRIMARY KEY, starts_on DATE NOT NULL CHECK (starts_on >= '2030-01-01'));"
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"promos": 15}, EngineConfig(seed=3))["promos"]
    bad = [str(r["starts_on"]) for r in rows if r["starts_on"] < date(2030, 1, 1)]
    assert not bad, f"starts_on values before 2030-01-01: {bad[:8]}"


def test_date_check_between_generates_in_window():
    """Review defect #4a in isolation: date BETWEEN must not crash and must
    bound generation to the window."""
    ddl = """
    CREATE TABLE seasons (
      id SERIAL PRIMARY KEY,
      day DATE NOT NULL CHECK (day BETWEEN '2020-01-01' AND '2020-12-31')
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"seasons": 15}, EngineConfig(seed=3))["seasons"]  # unfixed: ValueError
    bad = [str(r["day"]) for r in rows if not (date(2020, 1, 1) <= r["day"] <= date(2020, 12, 31))]
    assert not bad, f"day values outside BETWEEN window: {bad[:8]}"


def test_exotic_check_generate_and_filter():
    """Review defect #6 in isolation: an unrecognized CHECK (a + b < 1200)
    must still be honoured (the documented generate-and-filter fallback)."""
    ddl = """
    CREATE TABLE pairs (
      id SERIAL PRIMARY KEY,
      a INT NOT NULL,
      b INT NOT NULL,
      CHECK (a + b < 1200)
    );
    """
    schema = parse_ddl(ddl)
    rows = run_engine(schema, {"pairs": 40}, EngineConfig(seed=3))["pairs"]
    bad = [(r["a"], r["b"]) for r in rows if not (r["a"] + r["b"] < 1200)]
    assert not bad, f"CHECK (a + b < 1200) violated on {len(bad)}/40 rows, e.g. {bad[:5]}"


def test_validator_independently_catches_corrupted_enum():
    """Review defect #10: the validator must re-check enum membership
    independently. Corrupt one cell after generation; the validator must see it."""
    ddl = """
    CREATE TABLE tickets (
      id SERIAL PRIMARY KEY,
      status VARCHAR(10) NOT NULL CHECK (status IN ('open', 'closed'))
    );
    """
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"tickets": 10}, EngineConfig(seed=3))
    generated["tickets"][0]["status"] = "corrupted"
    report = validate(schema, generated, seed=3)
    assert report.violations_found >= 1, (
        "validator accepted an enum value outside CHECK (status IN ('open','closed'))"
    )


# ---------------------------------------------------------------------------
# 8. SQL output sanity
# ---------------------------------------------------------------------------

def _split_sql_tuple(body: str) -> list[str]:
    """Split one `(...)` VALUES tuple body on top-level commas, honouring
    single-quoted strings with '' escapes."""
    parts: list[str] = []
    cur: list[str] = []
    in_str = False
    i = 0
    while i < len(body):
        ch = body[i]
        if in_str:
            cur.append(ch)
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    cur.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur).strip())
    return parts


def _iter_sql_value_rows(sql_text: str):
    """Yield (table, columns, literals) for every VALUES tuple emitted by write_sql."""
    current_table = None
    current_cols: list[str] | None = None
    for line in sql_text.splitlines():
        m = re.match(r'INSERT INTO "(\w+)" \((.*)\) VALUES$', line.strip())
        if m:
            current_table = m.group(1)
            current_cols = [c.strip().strip('"') for c in m.group(2).split(",")]
            continue
        m = re.match(r"^\s*\((.*)\)[,;]\s*$", line)
        if m and current_cols:
            yield current_table, current_cols, _split_sql_tuple(m.group(1))


def test_sql_output_int_columns_have_no_floats(hard_schema, hard_generated, tmp_path):
    """Review defect #2 at the emitted-SQL level: an INT column position must
    contain only integer literals or NULL -- never 700.449769..."""
    order = build_generation_plan(hard_schema).order
    out = tmp_path / "hard_out.sql"
    write_sql(hard_schema, hard_generated, order, str(out))
    sql_text = out.read_text(encoding="utf-8")

    int_cols = {
        (t.name, c.name)
        for t in hard_schema.tables.values()
        for c in t.columns
        if c.dtype in INT_TYPES
    }
    rows_seen = 0
    bad = []
    for table, cols, literals in _iter_sql_value_rows(sql_text):
        rows_seen += 1
        assert len(cols) == len(literals), f"tuple arity mismatch in table {table}"
        for col, lit in zip(cols, literals):
            if (table, col) in int_cols and not re.fullmatch(r"-?\d+|NULL", lit):
                bad.append(f"{table}.{col}: {lit}")
    assert rows_seen == sum(HARD_ROWS.values()), "did not parse every emitted VALUES row"
    assert not bad, "non-integer literals in INT column positions:\n" + "\n".join(bad[:20])


def test_sql_output_null_rendering(hard_schema, hard_generated, tmp_path):
    """NULL must be emitted as the SQL keyword NULL, never the string 'None'."""
    order = build_generation_plan(hard_schema).order
    out = tmp_path / "hard_null.sql"
    write_sql(hard_schema, hard_generated, order, str(out))
    sql_text = out.read_text(encoding="utf-8")
    assert "'None'" not in sql_text, "Python None leaked into SQL as the string 'None'"
    # With null_rate=0.2 and several nullable columns, real NULLs must exist.
    assert re.search(r"(?<=[(,\s])NULL(?=[,)])", sql_text), "expected at least one NULL literal in output"


# ---------------------------------------------------------------------------
# 9. CSV round-trip
# ---------------------------------------------------------------------------

def test_csv_round_trip_int_columns(hard_schema, hard_generated, tmp_path):
    """write_csv then read back. NULL representation: csv.DictWriter renders
    None as an EMPTY STRING ("") -- review #14 notes Postgres COPY then needs
    `NULL ''` to read it back; we treat "" as NULL here and assert every
    non-null INT cell round-trips through int()."""
    order = build_generation_plan(hard_schema).order
    out_dir = tmp_path / "csv_out"
    written = write_csv(hard_schema, hard_generated, order, str(out_dir))
    assert len(written) == len(HARD_ROWS)

    for tname, table in hard_schema.tables.items():
        int_col_names = [c.name for c in table.columns if c.dtype in INT_TYPES]
        path = out_dir / f"{tname}.csv"
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == HARD_ROWS[tname], f"{tname}: row count changed in CSV round-trip"
        for i, row in enumerate(rows):
            for cname in int_col_names:
                cell = row[cname]
                if cell == "":  # empty string == NULL (see docstring)
                    continue
                try:
                    int(cell)
                except ValueError:
                    pytest.fail(
                        f"{tname}.{cname} row {i}: CSV cell {cell!r} is not a parseable int "
                        f"(floats in INT columns survive into the CSV -- review defect #2)"
                    )
