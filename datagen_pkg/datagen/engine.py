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

from .coherence import TableContext, apply_coherence
from .config import ColumnOverride, FanoutAllocation
from .dependency_graph import DeferredFK, GenerationPlan, build_generation_plan
from .generators import generate_independent_column
from .generators.base import MAX_UNIQUE_RETRIES, DomainExhaustedError
from .generators.check_eval import evaluate_check
from .generators.fk import expand_allocation, generate_fk_column
from .generators.linked_group import derive_dependent_column, generate_composite_unique_tuples
from .generators.numeric import generate_sequential_pk, generate_uuid
from .generators.pools import Pool, generate_pool_groups, load_pools
from .schema_model import Column, DataType, ForeignKey, SchemaModel, Table

MAX_CHECK_FILTER_RETRIES = 50  # per-row cap for the unparsed-CHECK retry loop

_INVERSE_OP = {">": "<", ">=": "<=", "<": ">", "<=": ">="}


@dataclass
class EngineConfig:
    seed: int = 42
    null_rate: float = 0.05          # default null rate for nullable, non-FK columns
    fk_null_rate: float = 0.1        # default null rate for nullable FK columns
    deferred_fk_null_rate: float = 0.2  # e.g. fraction of employees with no manager
    fanout: str = "zipfian"          # "zipfian" | "uniform", see generators/fk.py
    pk_start: dict[str, int] = field(default_factory=dict)  # per-table starting PK int
    # Anchor for default date/timestamp windows. Fixed by default (see
    # generators/date_time.DEFAULT_AS_OF) so the same seed produces identical
    # data regardless of the day the command runs. Set to move the window.
    as_of: date | None = None
    # Coherence layer (see datagen/coherence.py): correlated pools, cross-column
    # and cross-table chronology, anchor-sorted self-referencing hierarchies.
    coherence: bool = True
    # Fraction of rows in a self-referencing hierarchy that are roots (NULL
    # parent). None = reuse deferred_fk_null_rate.
    root_fraction: float | None = None
    # Extra pool directories (user-supplied *.json pools; a pool named like a
    # packaged one replaces it). See generators/pools.py.
    pool_dirs: list[str] = field(default_factory=list)
    # Exact per-parent fanout allocations from a "5..20 per users" rows spec
    # (see datagen/config.py resolve_row_counts), keyed by child table name.
    # When a child table has an allocation for one of its FKs, that FK's
    # values are assigned so parent i gets exactly per_parent[i] children,
    # overriding the uniform/zipfian sampling for that FK.
    fanout_allocations: dict[str, FanoutAllocation] = field(default_factory=dict)
    # Per-column overrides from [columns] in synthscale.toml, keyed
    # "table.column". Closed value lists are applied to the schema itself
    # (config.apply_column_overrides rewrites column.enum_values / injects
    # bound CHECKs); the engine additionally honors explicit `weights` for
    # value lists in step 4.4 below.
    column_overrides: dict[str, ColumnOverride] = field(default_factory=dict)


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
    ctx: dict[str, TableContext] | None = None,
    pools: dict[str, Pool] | None = None,
) -> list[dict]:
    if ctx is None:
        ctx = {}
    if pools is None:
        pools = load_pools(config.pool_dirs) if config.coherence else {}
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

    # Exact per-parent fanout allocation for this table, if a "LO..HI per
    # parent" rows spec was resolved for it (see datagen/config.py).
    alloc = config.fanout_allocations.get(table.name)

    # 1. Composite PK/UNIQUE groups: generate as unique tuples. FK members
    #    sample from parent pools; non-FK members use their own single-value
    #    generator. A group containing the table's single-column PK is skipped:
    #    the PK is unique on its own (which makes the tuple unique) and must
    #    keep sequential/UUID generation in stage 4.
    #    If one group member is the FK carrying a fanout allocation (junction
    #    tables like order_items with PK (order_id, product_id)), that member
    #    is fixed to the expanded allocation and only the OTHER members are
    #    re-drawn on uniqueness collisions, so the per-parent counts stay exact.
    for group in composite_groups:
        if not group or any(c in generated_cols for c in group):
            continue
        if single_pk is not None and single_pk in group:
            continue
        alloc_col = None
        if alloc is not None:
            for c in group:
                fk = fk_by_col.get(c)
                if fk is not None and len(fk.columns) == 1 and fk.ref_table == alloc.parent_table:
                    alloc_col = c
                    break
        gens = {}
        for c in group:
            if c == alloc_col:
                continue
            if c in fk_by_col:
                fk = fk_by_col[c]
                pool = _ref_pool_scalar(generated_tables[fk.ref_table], fk.ref_columns)
                gens[c] = (lambda pool=pool: rng.choice(pool))
            else:
                gens[c] = _one_value_callable(
                    table.get_column(c), n, rng, fake, single_col_checks, config.as_of
                )
        label = f"{table.name}({','.join(group)})"
        if alloc_col is not None:
            fk = fk_by_col[alloc_col]
            pool = _ref_pool_scalar(generated_tables[fk.ref_table], fk.ref_columns)
            alloc_values = expand_allocation(pool, alloc.per_parent, rng)
            if len(alloc_values) != n:
                raise ValueError(
                    f"Fanout allocation for {table.name!r} sums to {len(alloc_values)} "
                    f"rows but {n} were requested; the child row count must equal "
                    f"the allocation total."
                )
            tuples = _composite_tuples_with_allocation(
                list(group), alloc_col, alloc_values, gens, n, label
            )
        else:
            tuples = generate_composite_unique_tuples(gens, n, label=label)
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
            allocation = None
            if alloc is not None and fk.ref_table == alloc.parent_table:
                allocation = alloc.per_parent
            values = generate_fk_column(
                fk, n, pool, rng,
                null_rate=config.fk_null_rate if fk.nullable else 0.0,
                fanout=config.fanout,
                allocation=allocation,
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

    # 4.4. Weighted closed value lists from [columns] overrides. Unweighted
    #      lists ride the normal enum machinery (config.apply_column_overrides
    #      rewrote column.enum_values); explicit weights need a weighted draw,
    #      done here. UNIQUE columns are skipped (weights would force
    #      duplicates) and fall through to the enum + uniqueness path.
    for ov in config.column_overrides.values():
        if ov.table != table.name or ov.values is None or not ov.weights:
            continue
        try:
            column = table.get_column(ov.column)
        except KeyError:
            raise ValueError(
                f"[columns] override targets unknown column {ov.table}.{ov.column}"
            ) from None
        if column.name in generated_cols or column.is_unique:
            continue
        values = rng.choices(ov.values, weights=ov.weights, k=n)
        if column.nullable and config.null_rate > 0:
            values = [None if rng.random() < config.null_rate else v for v in values]
        row_data[column.name] = values
        generated_cols.add(column.name)

    # 4.5. Correlated pools (coherence layer): columns bound to the same pool
    #      generate as a group from one record per row, so city/state/country,
    #      first_name/gender, product/category/tier/price stay consistent.
    if config.coherence and pools:
        generate_pool_groups(
            table, row_data, generated_cols, n, rng, fake, pools,
            single_col_checks, config.null_rate,
        )

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

    # 7. Coherence post-pass (see datagen/coherence.py): rewrite timestamp
    #    columns for cross-column and cross-table chronology and capture this
    #    table's anchor context for its future children.
    if config.coherence:
        apply_coherence(table, row_data, n, rng, ctx, config, generated_tables, deferred_cols)

    return [{col.name: row_data[col.name][i] for col in table.columns} for i in range(n)]


def _composite_tuples_with_allocation(
    group: list[str],
    alloc_col: str,
    alloc_values: list,
    gens: dict,
    n: int,
    label: str,
) -> dict[str, list]:
    """Composite-unique tuple generation where one member (`alloc_col`) is
    pinned row-by-row to an exact fanout allocation. Only the OTHER members
    are re-drawn on a collision, so every parent keeps exactly its drawn
    number of child rows."""
    seen: set = set()
    out: dict[str, list] = {c: [] for c in group}
    for i in range(n):
        fixed = alloc_values[i]
        for _attempt in range(MAX_UNIQUE_RETRIES + 1):
            candidate = tuple(
                fixed if c == alloc_col else gens[c]() for c in group
            )
            if candidate not in seen:
                break
        else:
            raise DomainExhaustedError(
                f"Could not generate {n} unique combinations for {label} while "
                f"honoring the fanout allocation on {alloc_col!r}: a parent's drawn "
                f"child count exceeds the feasible combination space (e.g. more "
                f"items per order than distinct products). Lower the fanout range "
                f"or raise the other parent table's row count."
            )
        seen.add(candidate)
        for c, v in zip(group, candidate):
            out[c].append(v)
    return out


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


def _anchors_for(ctx: dict[str, TableContext], table_name: str, expected_len: int) -> list | None:
    """Index-aligned anchor timestamps for a table, or None if unavailable."""
    tctx = ctx.get(table_name)
    if tctx is None or tctx.anchor_col is None or len(tctx.anchors) != expected_len:
        return None
    return tctx.anchors


def _backfill_deferred_fks(
    generated: GeneratedTables,
    deferred_fks: list[DeferredFK],
    rng: random.Random,
    config: EngineConfig,
    ctx: dict[str, TableContext] | None = None,
) -> None:
    """Second pass for self-referential / cycle-broken FKs.

    Self-referencing FKs (employees.manager_id, categories.parent_id, ...):
    row i may only reference a row with index < i, so cycles are impossible
    by construction -- the structure is always a forest. Row 0 is always a
    root; every other row is a root with probability `root_fraction`
    (default: deferred_fk_null_rate).

    Cross-table cycle-broken FKs: when both sides carry anchor timestamps
    (coherence layer), candidates are filtered to parents whose anchor is
    <= the child's anchor -- a documented approximation, with NULL as the
    fallback when no candidate qualifies.
    """
    ctx = ctx or {}
    root_fraction = (
        config.root_fraction if config.root_fraction is not None else config.deferred_fk_null_rate
    )
    for d in deferred_fks:
        fk = d.fk
        child_rows = generated[d.child_table]
        parent_rows = generated[fk.ref_table]
        pool = _ref_pool_scalar(parent_rows, fk.ref_columns)
        same_table = d.child_table == fk.ref_table
        col_name = fk.columns[0]

        child_anchors = _anchors_for(ctx, d.child_table, len(child_rows))
        parent_anchors = _anchors_for(ctx, fk.ref_table, len(parent_rows))

        for i, row in enumerate(child_rows):
            if same_table:
                # Roots: row 0 always; others with probability root_fraction.
                if i == 0 or rng.random() < root_fraction:
                    row[col_name] = None
                    continue
                candidates = range(i)  # strictly earlier rows only -> acyclic
            else:
                if rng.random() < config.deferred_fk_null_rate:
                    row[col_name] = None
                    continue
                candidates = range(len(pool))

            if child_anchors is not None and parent_anchors is not None:
                ca = child_anchors[i]
                idxs = [
                    j for j in candidates
                    if ca is None or parent_anchors[j] is None or parent_anchors[j] <= ca
                ]
            else:
                idxs = list(candidates)

            if not idxs:
                # No chronology-compatible candidate. The FK is nullable by
                # construction (dependency_graph rejects NOT NULL cycles).
                row[col_name] = None
                continue
            row[col_name] = pool[rng.choice(idxs)]


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

    ctx: dict[str, TableContext] = {}
    pools = load_pools(config.pool_dirs) if config.coherence else {}

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
        generated[table_name] = generate_table(
            table, n, rng, fake, generated, config, deferred_cols, ctx=ctx, pools=pools
        )

    _backfill_deferred_fks(generated, plan.deferred_fks, rng, config, ctx=ctx)

    return generated
