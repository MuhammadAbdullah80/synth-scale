"""Live-Postgres load test -- the real product bar: "Postgres accepted it."

Runs ONLY when the environment variable SYNTH_PG_URL points at a reachable
Postgres database (e.g. postgresql+psycopg2://user:pass@localhost:5432/scratch).
Without it, the whole module SKIPS cleanly -- this is the CI hook for later;
no docker is required or assumed.

WARNING: the tests DROP and re-CREATE the fixture tables in the target
database. Point SYNTH_PG_URL at a scratch database only.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text  # noqa: E402

from datagen.ddl_parser import parse_ddl  # noqa: E402
from datagen.dependency_graph import build_generation_plan  # noqa: E402
from datagen.engine import EngineConfig, run_engine  # noqa: E402
from datagen.output.db_loader import load_to_db  # noqa: E402

PG_URL = os.environ.get("SYNTH_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="SYNTH_PG_URL not set; export a Postgres URL to run the live-load test"
)

FIXTURES = Path(__file__).parent / "fixtures"
HARD_SQL = (FIXTURES / "hard.sql").read_text(encoding="utf-8")
SAMPLE_SQL = (FIXTURES / "sample.sql").read_text(encoding="utf-8")

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

SAMPLE_ROWS = {
    "categories": 5,
    "users": 10,
    "employees": 8,
    "products": 20,
    "orders": 30,
    "order_items": 60,
}

# ~50k total rows across the hard fixture -- the demo-day benchmark shape.
BENCH_ROWS = {
    "organizations": 50,
    "users": 2000,
    "categories": 30,
    "products": 3000,
    "inventory": 4000,
    "coupons": 500,
    "subscriptions": 3000,
    "orders": 12000,
    "order_items": 25000,
    "audit_log": 420,
}
assert sum(BENCH_ROWS.values()) == 50_000


def _drop_all(engine, table_names):
    with engine.begin() as conn:
        for t in reversed(table_names):
            conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))


def _assert_no_fk_orphans(conn, schema):
    """For every FK edge in the schema, assert the live DB holds zero child
    rows whose (fully non-NULL) FK tuple has no matching parent row."""
    for child_name, fk in schema.all_fks():
        not_null = " AND ".join(f'c."{col}" IS NOT NULL' for col in fk.columns)
        join_on = " AND ".join(
            f'p."{ref}" = c."{col}"' for col, ref in zip(fk.columns, fk.ref_columns)
        )
        sql = (
            f'SELECT COUNT(*) FROM "{child_name}" c '
            f"WHERE {not_null} AND NOT EXISTS "
            f'(SELECT 1 FROM "{fk.ref_table}" p WHERE {join_on})'
        )
        orphans = conn.execute(text(sql)).scalar_one()
        assert orphans == 0, (
            f"{child_name}({', '.join(fk.columns)}) -> "
            f"{fk.ref_table}({', '.join(fk.ref_columns)}): {orphans} orphan rows in Postgres"
        )


def _load_and_verify(ddl_text: str, rows: dict[str, int], config: EngineConfig):
    """Shared body: parse, generate, execute DDL against the live DB, load,
    then verify with SQL run against Postgres itself (counts + FK joins)."""
    schema = parse_ddl(ddl_text)
    plan = build_generation_plan(schema)
    generated = run_engine(schema, rows, config)

    engine = create_engine(PG_URL)
    try:
        _drop_all(engine, plan.order)
        with engine.begin() as conn:
            conn.exec_driver_sql(ddl_text)  # run the exact DDL the parser saw

        # The claim we sell: zero errors on load. Any constraint violation or
        # type mismatch raises here and fails the test.
        inserted = load_to_db(schema, generated, plan.order, PG_URL)
        assert inserted == rows

        with engine.connect() as conn:
            for t, n in rows.items():
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()
                assert count == n, f"{t}: expected {n} rows in Postgres, found {count}"
            _assert_no_fk_orphans(conn, schema)
    finally:
        _drop_all(engine, plan.order)
        engine.dispose()


def test_hard_fixture_loads_into_real_postgres():
    _load_and_verify(HARD_SQL, HARD_ROWS, EngineConfig(seed=42, null_rate=0.2))


def test_sample_fixture_loads_into_real_postgres():
    _load_and_verify(SAMPLE_SQL, SAMPLE_ROWS, EngineConfig(seed=42))


def test_benchmark_50k_rows_hard_fixture():
    """Demo-day number: generate + load ~50k rows of the hard fixture into a
    real Postgres, timing each phase separately. Run pytest with -s to see the
    timings; they are also asserted sane (nonzero) so the test is not vacuous.
    """
    schema = parse_ddl(HARD_SQL)
    plan = build_generation_plan(schema)

    t0 = time.perf_counter()
    generated = run_engine(schema, BENCH_ROWS, EngineConfig(seed=42))
    generate_seconds = time.perf_counter() - t0

    engine = create_engine(PG_URL)
    try:
        _drop_all(engine, plan.order)
        with engine.begin() as conn:
            conn.exec_driver_sql(HARD_SQL)

        t0 = time.perf_counter()
        inserted = load_to_db(schema, generated, plan.order, PG_URL)
        load_seconds = time.perf_counter() - t0

        assert inserted == BENCH_ROWS
        with engine.connect() as conn:
            total = 0
            for t, n in BENCH_ROWS.items():
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()
                assert count == n, f"{t}: expected {n} rows in Postgres, found {count}"
                total += count
            assert total == 50_000
            _assert_no_fk_orphans(conn, schema)
    finally:
        _drop_all(engine, plan.order)
        engine.dispose()

    print(
        f"\n[benchmark] hard.sql 50,000 rows: "
        f"generate={generate_seconds:.2f}s load={load_seconds:.2f}s"
    )
    assert generate_seconds > 0 and load_seconds > 0
