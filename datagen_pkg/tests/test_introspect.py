"""Live-Postgres introspection tests.

Same gating contract as test_postgres_load.py: runs ONLY when SYNTH_PG_URL
points at a reachable scratch Postgres database; skips cleanly otherwise.

For each fixture: execute the DDL against the live DB, introspect it back
with datagen.introspect.introspect_db, and assert the resulting SchemaModel
is equivalent to parse_ddl() of the same file. Then the round-trip proof:
introspected schema -> run_engine -> load back into the same live DB with
zero errors.

WARNING: drops and re-creates the fixture tables (and any other tables in
schema 'public' are untouched, but point this at a scratch DB anyway).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text  # noqa: E402

from datagen.ddl_parser import parse_ddl  # noqa: E402
from datagen.dependency_graph import build_generation_plan  # noqa: E402
from datagen.engine import EngineConfig, run_engine  # noqa: E402
from datagen.introspect import introspect_db  # noqa: E402
from datagen.output.db_loader import load_to_db  # noqa: E402

PG_URL = os.environ.get("SYNTH_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="SYNTH_PG_URL not set; export a Postgres URL to run introspection tests"
)

FIXTURES = Path(__file__).parent / "fixtures"

ROWS = {
    "sample.sql": {
        "categories": 5,
        "users": 10,
        "employees": 8,
        "products": 20,
        "orders": 30,
        "order_items": 60,
    },
    "hard.sql": {
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
    },
}


def _drop_all(engine, table_names):
    with engine.begin() as conn:
        for t in reversed(table_names):
            conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))


def _drop_entire_public_schema(engine):
    """Introspection reads ALL of schema 'public', so these tests need a
    clean slate, not just their own tables gone. SYNTH_PG_URL is documented
    as a scratch database; dropping everything in public is in-contract."""
    with engine.begin() as conn:
        views = [r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
        ))]
        for v in views:
            conn.execute(text(f'DROP VIEW IF EXISTS "{v}" CASCADE'))
        tables = [r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ))]
        for t in tables:
            conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))


@pytest.fixture(params=["sample.sql", "hard.sql"])
def live_fixture(request):
    """Execute a fixture's DDL against the live DB; yield (ddl_text, rows).
    Tables are dropped again afterwards."""
    ddl_text = (FIXTURES / request.param).read_text(encoding="utf-8")
    parsed = parse_ddl(ddl_text)
    plan = build_generation_plan(parsed)
    engine = create_engine(PG_URL)
    try:
        _drop_entire_public_schema(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql(ddl_text)
        yield request.param, ddl_text, ROWS[request.param]
    finally:
        _drop_all(engine, plan.order)
        engine.dispose()


def _fk_edges(table):
    return {
        (tuple(fk.columns), fk.ref_table, tuple(fk.ref_columns))
        for fk in table.foreign_keys
    }


def test_introspected_schema_matches_parse_ddl(live_fixture):
    name, ddl_text, _rows = live_fixture
    expected = parse_ddl(ddl_text)
    actual = introspect_db(PG_URL)

    # Table set (order-independent).
    assert set(actual.tables) == set(expected.tables), name

    for tname, exp_table in expected.tables.items():
        act_table = actual.tables[tname]
        ctx = f"{name}:{tname}"

        # Column names, in declaration order (Postgres preserves DDL order).
        assert act_table.column_names() == exp_table.column_names(), ctx

        for exp_col in exp_table.columns:
            act_col = act_table.get_column(exp_col.name)
            cctx = f"{ctx}.{exp_col.name}"
            assert act_col.dtype == exp_col.dtype, cctx
            assert act_col.nullable == exp_col.nullable, cctx
            assert act_col.max_length == exp_col.max_length, cctx
            assert (act_col.precision, act_col.scale) == (exp_col.precision, exp_col.scale), cctx
            assert act_col.is_unique == exp_col.is_unique, cctx
            assert act_col.semantic_hint == exp_col.semantic_hint, cctx
            # Enum domains as sets (order is not semantic).
            assert (set(act_col.enum_values) if act_col.enum_values else None) == (
                set(exp_col.enum_values) if exp_col.enum_values else None
            ), cctx

        # Primary key (as a set: composite member order is not semantic here).
        assert set(act_table.primary_key) == set(exp_table.primary_key), ctx

        # FK edges: child cols -> parent(cols).
        assert _fk_edges(act_table) == _fk_edges(exp_table), ctx

        # Composite UNIQUE constraints, order-independent.
        assert {frozenset(u) for u in act_table.unique_constraints} == {
            frozenset(u) for u in exp_table.unique_constraints
        }, ctx


def test_introspection_skips_views():
    ddl_text = (FIXTURES / "sample.sql").read_text(encoding="utf-8")
    parsed = parse_ddl(ddl_text)
    plan = build_generation_plan(parsed)
    engine = create_engine(PG_URL)
    try:
        _drop_entire_public_schema(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql(ddl_text)
            conn.execute(text("CREATE OR REPLACE VIEW user_emails AS SELECT id, email FROM users"))
        schema = introspect_db(PG_URL)
        assert "user_emails" not in schema.tables
        assert set(schema.tables) == set(parsed.tables)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP VIEW IF EXISTS user_emails"))
        _drop_all(engine, plan.order)
        engine.dispose()


def test_introspect_then_generate_then_load_round_trip(live_fixture):
    """The money test: read the schema from the live DB (no DDL file at all),
    generate data from that introspected model, and load it back into the same
    live DB with zero errors."""
    name, _ddl_text, rows = live_fixture
    schema = introspect_db(PG_URL)
    plan = build_generation_plan(schema)
    generated = run_engine(schema, rows, EngineConfig(seed=7))

    inserted = load_to_db(schema, generated, plan.order, PG_URL)
    assert inserted == rows, name

    engine = create_engine(PG_URL)
    try:
        with engine.connect() as conn:
            for t, n in rows.items():
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()
                assert count == n, f"{name}:{t}: expected {n} rows, found {count}"
    finally:
        engine.dispose()
