"""Stage 3 orchestrator: walks the generation plan in dependency order and
produces fully-populated rows for every table.

Column generation order per table (see the design write-up, section 5):
  1. Composite PK/UNIQUE groups (junction tables, or any mix of FK and
     non-FK members) -- generated as unique tuples: FK members sample from
     parent pools, non-FK members use their own single-value generator.
  2. Remaining FK columns (single or composite), sampled from parent pools.
  3. Multi-column CHECK groups (e.g. `end_date > start_date`) -- construct the
     dependent column from the already-generated base column. The table's PK
     is never derived: if the PK appears on the left of the comparison it is
     generated first (sequential/UUID) and the *other* column is derived with
     the inverse operator.
  4. The table's own single-column PK, if not already covered by (1)-(3)
     (sequential integers, or seeded UUIDs).
  5. Everything else: independent columns, generated as flat per-column lists.
  6. Unparsed CHECK constraints: a bounded generate-and-filter retry loop
     using the safe fallback evaluator (generators/check_eval.py), for
     constraints whose columns can be regenerated safely.

This ordering matters: FK and PK values must exist before anything that
references them, and CHECK-linked columns must be derived together rather
than generated as independent lists (that's the fix to the naive "just zip
column lists together" approach).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date

from faker import Faker

from .dependency_graph import DeferredFK, GenerationPlan, build_generation_plan
from .generators import generate_independent_column
from .generators.base import DomainExhaustedError
from .generators.check_eval import evaluate_check
from .generators.fk import generate_fk_column
from .generators.linked_group import derive_dependent_column, generate_composite_unique_tuples
from .generators.numeric import generate_sequential_pk, generate_uuid
from .schema_model import Column, DataType, ForeignKey, SchemaModel, Table

MAX_CHECK_FILTER_RETRIES = 50  # per-row cap for the unparsed-CHECK retry loop

_INVERSE_OP = {">": "<", ">=": "<=", "<": ">", "<=": ">="}


@dataclass
class EngineConfig:
    seed: int = 42
    null_rate: float = 0.05          # default null rate for nullable, non-FK columns
    fk_null_rate: float = 0.1        # default null rate for nullable FK columns
    deferred_fk_null_rate: float = 0.2  # e.g. fraction of employees with no manager
    fanout: str = "uniform"          # "uniform" | "zipfian", see generators/fk.py
    pk_start: dict[str, int] = field(default_factory=dict)  # per-table starting PK int
    # Anchor for default date/timestamp windows. Fixed by default (see
    # generators/date_time.DEFAULT_AS_OF) so the same seed produces identical
    # data regardless of the day the command runs. Set to move the window.
    as_of: date | None = None


GeneratedTables = dict[str, list[dict]]


def _ref_pool_scalar(parent_rows: list[dict], ref_columns: list[str]) -> list:
    return [row[ref_columns[0]] for row in parent_rows]


def _ref_pool_tuples(parent_rows: list[dict], ref_columns: list[str]) -> list[tuple]:
    return [tuple(row[c] for c in ref_columns) for row in parent_rows]


def _one_value_callable(
    column: Column,
    n: int,
    rng: random.Random,
    fake: Faker,
    single_col_checks: list[dict],
    as_of: date | None,
):
    """Zero-arg callable producing one value at a time for `column`, batching
    through generate_independent_column so all type/bound/enum logic is reused.
    Always generates non-null values; null rates are the caller's concern."""
    cache: list = []

    def _one():
        if not cache:
            cache.extend(
                generate_independent_column(
                    column, max(n, 16), rng, fake, single_col_checks, null_rate=0.0, as_of=as_of
                )
            )
        return cache.pop()

    return _one


def _generate_single_pk(column: Column, table: Table, n: int, rng: random.Random, config: EngineConfig) -> list:
    if column.dtype == DataType.UUID:
        return generate_uuid(n, rng)
    return generate_sequential_pk(n, start=config.pk_start.get(table.name, 1))


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

    single_col_checks = [c.parsed for c in table.check_constraints if len(c.columns_involved) == 1]
    single_pk = table.primary_key[0] if len(table.primary_key) == 1 else None

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

    # 1. Composite PK/UNIQUE groups: generate as unique tuples. FK members
    #    sample from parent pools; non-FK members use their own single-value
    #    generator. A group containing the table's single-column PK is skipped:
    #    the PK is unique on its own (which makes the tuple unique) and must
    #    keep sequential/UUID generation in stage 4.
    for group in composite_groups:
        if not group or any(c in generated_cols for c in group):
            continue
        if single_pk is not None and single_pk in group:
            continue
        gens = {}
        for c in group:
            if c in fk_by_col:
                fk = fk_by_col[c]
                pool = _ref_pool_scalar(generated_tables[fk.ref_table], fk.ref_columns)
                gens[c] = (lambda pool=pool: rng.choice(pool))
            else:
                gens[c] = _one_value_callable(
                    table.get_column(c), n, rng, fake, single_col_checks, config.as_of
                )
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
    for chk in table.check_constraints:
        if not chk.parsed or chk.parsed.get("kind") != "col_compare":
            continue
        left, right, op = chk.parsed["left"], chk.parsed["right"], chk.parsed["op"]
        if left in generated_cols and right in generated_cols:
            continue
        left_col = table.get_column(left)
        right_col = table.get_column(right)

        if left == single_pk and left not in generated_cols:
            # Never derive the PK from another column -- generate it first
            # (sequential / UUID) and derive the OTHER side with the inverse
            # operator instead (id > lo  =>  lo < id).
            row_data[left] = _generate_single_pk(left_col, table, n, rng, config)
            generated_cols.add(left)
            if right not in generated_cols and left_col.dtype != DataType.UUID:
                derived = derive_dependent_column(row_data[left], _INVERSE_OP[op], rng, right_col)
                if right_col.nullable and config.null_rate > 0:
                    derived = [None if rng.random() < config.null_rate else v for v in derived]
                row_data[right] = derived
                generated_cols.add(right)
            continue

        right_was_generated_here = False
        if right not in generated_cols:
            # Generate the base column WITHOUT nulls so a NOT NULL dependent
            # column never inherits a None; the base's own null rate is
            # re-applied after derivation.
            row_data[right] = generate_independent_column(
                right_col, n, rng, fake, single_col_checks, null_rate=0.0, as_of=config.as_of
            )
            generated_cols.add(right)
            right_was_generated_here = True

        if left not in generated_cols and left != single_pk:
            derived = derive_dependent_column(row_data[right], op, rng, left_col)
            if not left_col.nullable and any(v is None for v in derived):
                # Base was already generated elsewhere (FK, earlier check) and
                # contains NULLs. The CHECK is vacuous for those rows in SQL,
                # so synthesize standalone values to honor NOT NULL.
                fillers = iter(
                    generate_independent_column(
                        left_col, sum(1 for v in derived if v is None), rng, fake,
                        single_col_checks, null_rate=0.0, as_of=config.as_of,
                    )
                )
                derived = [next(fillers) if v is None else v for v in derived]
            elif left_col.nullable and config.null_rate > 0:
                derived = [None if rng.random() < config.null_rate else v for v in derived]
            row_data[left] = derived
            generated_cols.add(left)

        if right_was_generated_here and right_col.nullable and config.null_rate > 0:
            row_data[right] = [None if rng.random() < config.null_rate else v for v in row_data[right]]

    # 4. Single-column PK, if not already produced by steps 1-3.
    if single_pk is not None and single_pk not in generated_cols:
        row_data[single_pk] = _generate_single_pk(table.get_column(single_pk), table, n, rng, config)
        generated_cols.add(single_pk)

    # 5. Everything else: independent columns.
    for col in table.columns:
        if col.name in generated_cols:
            continue
        null_rate = config.null_rate if col.nullable else 0.0
        row_data[col.name] = generate_independent_column(
            col, n, rng, fake, single_col_checks, null_rate=null_rate, as_of=config.as_of
        )
        generated_cols.add(col.name)

    # 6. Unparsed CHECK constraints: bounded generate-and-filter using the safe
    #    fallback evaluator. Only columns that can be regenerated without
    #    breaking other guarantees (not PK/FK/UNIQUE/composite-group/deferred
    #    members) are retried; anything else is left for the validator to
    #    report (see README "Known limitations").
    _apply_unparsed_check_filter(table, row_data, n, rng, fake, single_col_checks, config,
                                 composite_groups, fk_by_col, deferred_cols)

    return [{col.name: row_data[col.name][i] for col in table.columns} for i in range(n)]


def _apply_unparsed_check_filter(
    table: Table,
    row_data: dict[str, list],
    n: int,
    rng: random.Random,
    fake: Faker,
    single_col_checks: list[dict],
    config: EngineConfig,
    composite_groups: list[list[str]],
    fk_by_col: dict[str, ForeignKey],
    deferred_cols: set[str],
) -> None:
    protected: set[str] = set(table.primary_key) | set(fk_by_col) | set(deferred_cols)
    for group in composite_groups:
        protected.update(group)
    protected.update(c.name for c in table.columns if c.is_unique)

    for chk in table.check_constraints:
        if chk.parsed is not None:
            continue
        cols = chk.columns_involved
        if not cols or any(c not in row_data for c in cols):
            continue  # references something we didn't generate (exotic DDL)
        if any(c in protected for c in cols):
            continue  # cannot regenerate safely; validator will report it
        regen = {
            c: _one_value_callable(table.get_column(c), n, rng, fake, single_col_checks, config.as_of)
            for c in cols
        }
        for i in range(n):
            values = {c: row_data[c][i] for c in cols}
            if any(v is None for v in values.values()):
                continue  # SQL: NULL comparisons don't fail a CHECK
            try:
                ok = evaluate_check(chk.raw_sql, values)
            except Exception:
                break  # unverifiable expression; validator will warn
            retries = 0
            while not ok:
                retries += 1
                if retries > MAX_CHECK_FILTER_RETRIES:
                    raise DomainExhaustedError(
                        f"Table {table.name!r}, row {i}: could not satisfy CHECK "
                        f"({chk.raw_sql}) after {MAX_CHECK_FILTER_RETRIES} "
                        f"generate-and-filter retries. The constraint is too "
                        f"selective for the column value ranges in play; add "
                        f"explicit bounds to the columns or widen the constraint."
                    )
                for c in cols:
                    row_data[c][i] = regen[c]()
                values = {c: row_data[c][i] for c in cols}
                ok = evaluate_check(chk.raw_sql, values)


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


def run_engine(
    schema: SchemaModel,
    row_counts: dict[str, int],
    config: EngineConfig | None = None,
    plan: GenerationPlan | None = None,
) -> GeneratedTables:
    """Generate data for every table. A pre-built `plan` (from
    build_generation_plan) can be passed to avoid recomputing it; the CLI
    does this so the plan is only built once per invocation."""
    config = config or EngineConfig()
    rng = random.Random(config.seed)
    Faker.seed(config.seed)
    fake = Faker()

    if plan is None:
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
