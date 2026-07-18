"""Stage 3 orchestrator: walks the generation plan in dependency order and
produces fully-populated rows for every table.

Column generation order per table (see the design write-up, section 5):
  1. Composite PK/UNIQUE groups made entirely of FK columns (junction tables).
  2. Remaining FK columns (single or composite), sampled from parent pools.
  3. Multi-column CHECK groups (e.g. `end_date > start_date`) -- construct the
     dependent column from the already-generated base column.
  4. The table's own single-column PK, if not already covered by (1)/(2)
     (sequential integers, or UUIDs).
  5. Everything else: independent columns, generated as flat per-column lists.

This ordering matters: FK and PK values must exist before anything that
references them, and CHECK-linked columns must be derived together rather
than generated as independent lists (that's the fix to the naive "just zip
column lists together" approach).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from faker import Faker

from .dependency_graph import DeferredFK, build_generation_plan
from .generators import generate_independent_column
from .generators.fk import generate_fk_column
from .generators.linked_group import derive_dependent_column, generate_composite_unique_tuples
from .generators.numeric import generate_sequential_pk, generate_uuid
from .schema_model import DataType, ForeignKey, SchemaModel, Table


@dataclass
class EngineConfig:
    seed: int = 42
    null_rate: float = 0.05          # default null rate for nullable, non-FK columns
    fk_null_rate: float = 0.1        # default null rate for nullable FK columns
    deferred_fk_null_rate: float = 0.2  # e.g. fraction of employees with no manager
    fanout: str = "uniform"          # "uniform" | "zipfian", see generators/fk.py
    pk_start: dict[str, int] = field(default_factory=dict)  # per-table starting PK int


GeneratedTables = dict[str, list[dict]]


def _ref_pool_scalar(parent_rows: list[dict], ref_columns: list[str]) -> list:
    return [row[ref_columns[0]] for row in parent_rows]


def _ref_pool_tuples(parent_rows: list[dict], ref_columns: list[str]) -> list[tuple]:
    return [tuple(row[c] for c in ref_columns) for row in parent_rows]


def generate_table(
    table: Table,
    n: int,
    rng: random.Random,
    fake: Faker,
    generated_tables: GeneratedTables,
    config: EngineConfig,
    deferred_cols: set[str],
) -> list[dict]:
    row_data: dict[str, list] = {}
    generated_cols: set[str] = set()

    # Deferred FK columns (self-ref / cycle-broken) are left null in this pass
    # and backfilled in the second pass after every table exists.
    for col_name in deferred_cols:
        row_data[col_name] = [None] * n
        generated_cols.add(col_name)

    fk_by_col: dict[str, ForeignKey] = {}
    for fk in table.foreign_keys:
        for c in fk.columns:
            if c not in deferred_cols:
                fk_by_col[c] = fk

    composite_groups = list(table.unique_constraints)
    if len(table.primary_key) > 1:
        composite_groups.append(table.primary_key)

    # 1. Composite PK/UNIQUE groups made entirely of (non-deferred) FK columns.
    for group in composite_groups:
        if any(c in generated_cols for c in group):
            continue
        if group and all(c in fk_by_col for c in group):
            gens = {}
            for c in group:
                fk = fk_by_col[c]
                pool = _ref_pool_scalar(generated_tables[fk.ref_table], fk.ref_columns)
                gens[c] = (lambda pool=pool: rng.choice(pool))
            tuples = generate_composite_unique_tuples(
                gens, n, label=f"{table.name}({','.join(group)})"
            )
            for c, vals in tuples.items():
                row_data[c] = vals
                generated_cols.add(c)

    # 2. Remaining FK columns not already covered above.
    seen_fks = set()
    for fk in table.foreign_keys:
        fk_id = (tuple(fk.columns), fk.ref_table, tuple(fk.ref_columns))
        if fk_id in seen_fks or all(c in generated_cols for c in fk.columns):
            continue
        seen_fks.add(fk_id)
        if len(fk.columns) == 1:
            c = fk.columns[0]
            pool = _ref_pool_scalar(generated_tables[fk.ref_table], fk.ref_columns)
            values = generate_fk_column(
                fk, n, pool, rng, null_rate=config.fk_null_rate if fk.nullable else 0.0, fanout=config.fanout
            )
            row_data[c] = values
            generated_cols.add(c)
        else:
            pool = _ref_pool_tuples(generated_tables[fk.ref_table], fk.ref_columns)
            picks = [rng.choice(pool) for _ in range(n)]
            for i, c in enumerate(fk.columns):
                row_data[c] = [p[i] for p in picks]
                generated_cols.add(c)

    # 3. Multi-column CHECK groups: construct dependent column from base column.
    single_col_checks = [c.parsed for c in table.check_constraints if len(c.columns_involved) == 1]
    for chk in table.check_constraints:
        if not chk.parsed or chk.parsed.get("kind") != "col_compare":
            continue
        left, right, op = chk.parsed["left"], chk.parsed["right"], chk.parsed["op"]
        if left in generated_cols and right in generated_cols:
            continue
        if right not in generated_cols:
            right_col = table.get_column(right)
            row_data[right] = generate_independent_column(
                right_col, n, rng, fake, single_col_checks,
                null_rate=config.null_rate if right_col.nullable else 0.0,
            )
            generated_cols.add(right)
        if left not in generated_cols:
            row_data[left] = derive_dependent_column(row_data[right], op, rng)
            generated_cols.add(left)

    # 4. Single-column PK, if not already produced by steps 1-3.
    if len(table.primary_key) == 1:
        c = table.primary_key[0]
        if c not in generated_cols:
            col = table.get_column(c)
            if col.dtype == DataType.UUID:
                row_data[c] = generate_uuid(n)
            else:
                row_data[c] = generate_sequential_pk(n, start=config.pk_start.get(table.name, 1))
            generated_cols.add(c)

    # 5. Everything else: independent columns.
    for col in table.columns:
        if col.name in generated_cols:
            continue
        null_rate = config.null_rate if col.nullable else 0.0
        row_data[col.name] = generate_independent_column(
            col, n, rng, fake, single_col_checks, null_rate=null_rate
        )
        generated_cols.add(col.name)

    return [{col.name: row_data[col.name][i] for col in table.columns} for i in range(n)]


def _backfill_deferred_fks(
    generated: GeneratedTables, deferred_fks: list[DeferredFK], rng: random.Random, config: EngineConfig
) -> None:
    for d in deferred_fks:
        fk = d.fk
        child_rows = generated[d.child_table]
        parent_rows = generated[fk.ref_table]
        pool = _ref_pool_scalar(parent_rows, fk.ref_columns)
        same_table = d.child_table == fk.ref_table
        col_name = fk.columns[0]
        pk_col = fk.ref_columns[0] if same_table else None

        for row in child_rows:
            if rng.random() < config.deferred_fk_null_rate:
                row[col_name] = None
                continue
            candidate = rng.choice(pool)
            # Avoid a row pointing directly at itself when the FK is
            # self-referential and there's more than one candidate available.
            if same_table and pk_col is not None and len(pool) > 1:
                own_pk_value = row.get(pk_col)
                attempts = 0
                while candidate == own_pk_value and attempts < 10:
                    candidate = rng.choice(pool)
                    attempts += 1
            row[col_name] = candidate


def run_engine(schema: SchemaModel, row_counts: dict[str, int], config: EngineConfig | None = None) -> GeneratedTables:
    config = config or EngineConfig()
    rng = random.Random(config.seed)
    Faker.seed(config.seed)
    fake = Faker()

    plan = build_generation_plan(schema)

    deferred_by_table: dict[str, set[str]] = {}
    for d in plan.deferred_fks:
        deferred_by_table.setdefault(d.child_table, set()).update(d.fk.columns)

    generated: GeneratedTables = {}
    for table_name in plan.order:
        table = schema.tables[table_name]
        n = row_counts.get(table_name)
        if n is None:
            raise ValueError(
                f"No row count supplied for table {table_name!r}. "
                f"Pass --rows {table_name}=<N> or include it in the rows config."
            )
        deferred_cols = deferred_by_table.get(table_name, set())
        generated[table_name] = generate_table(table, n, rng, fake, generated, config, deferred_cols)

    _backfill_deferred_fks(generated, plan.deferred_fks, rng, config)

    return generated
