"""Live-Postgres load test -- the real product bar: "Postgres accepted it."

Runs ONLY when the environment variable SYNTH_PG_URL points at a reachable
Postgres database (e.g. postgresql+psycopg2://user:pass@localhost:5432/scratch).
Without it, the whole module SKIPS cleanly -- this is the CI hook for later;
no docker is required or assumed.

WARNING: the test DROPs and re-CREATEs the hard.sql tables in the target
database. Point SYNTH_PG_URL at a scratch database only.
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
from datagen.output.db_loader import load_to_db  # noqa: E402

PG_URL = os.environ.get("SYNTH_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="SYNTH_PG_URL not set; export a Postgres URL to run the live-load test"
)

HARD_SQL = (Path(__file__).parent / "fixtures" / "hard.sql").read_text(encoding="utf-8")

ROWS = {
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


def _drop_all(engine, table_names):
    with engine.begin() as conn:
        for t in reversed(table_names):
            conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))


def test_hard_fixture_loads_into_real_postgres():
    schema = parse_ddl(HARD_SQL)
    plan = build_generation_plan(schema)
    generated = run_engine(schema, ROWS, EngineConfig(seed=42, null_rate=0.2))

    engine = create_engine(PG_URL)
    try:
        _drop_all(engine, plan.order)
        with engine.begin() as conn:
            conn.exec_driver_sql(HARD_SQL)  # run the exact DDL the parser saw

        # The claim we sell: zero errors on load. Any constraint violation or
        # type mismatch raises here and fails the test.
        inserted = load_to_db(schema, generated, plan.order, PG_URL)
        assert inserted == ROWS

        with engine.connect() as conn:
            for t, n in ROWS.items():
                count = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()
                assert count == n, f"{t}: expected {n} rows in Postgres, found {count}"
    finally:
        _drop_all(engine, plan.order)
        engine.dispose()
