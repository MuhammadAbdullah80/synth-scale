"""Coherence layer (engine step 7): a deterministic post-pass that rewrites
timestamp columns so generated data respects cause-and-effect.

What it fixes (see COHERENCE_DESIGN.md and REVIEW_REPORT.md P11/P12/P16):
  a. Cross-column chronology -- within a row, `updated_at >= created_at`,
     lifecycle stages (`shipped_at`, `delivered_at`, ...) form an ordered
     chain after the row's anchor, `last_login`-style columns land between
     the anchor and `as_of`, and an enum status column gates which lifecycle
     cells are filled.
  b. Cross-table chronology -- a child row's anchor is floored at the anchor
     of every parent row it references (max over all FKs; NULL FK picks and
     anchorless parents contribute no floor).
  c. Hierarchy realism -- self-referencing FKs are backfilled from
     strictly-earlier row indices (see engine._backfill_deferred_fks), which
     makes cycles impossible by construction; for tables with no cross-table
     floors of their own the anchor column is sorted ascending first, so an
     earlier index also means an earlier timestamp.

Hard rules the pass never breaks:
  * Determinism: every random draw comes from the engine's seeded rng; the
    time anchor is EngineConfig.as_of / DEFAULT_AS_OF, never the wall clock.
  * Constraint-correctness outranks realism: columns governed by a parsed
    two-column CHECK, an unparsed CHECK, UNIQUE, PK, FK, or a composite
    UNIQUE/PK group are left untouched; single-column CHECK bounds are
    clamped, not violated; existing NULL/NOT NULL patterns are preserved
    (a NULL cell is never resurrected, a NOT NULL cell never nulled).
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .generators.base import apply_single_column_bounds
from .generators.date_time import DEFAULT_AS_OF
from .schema_model import DataType, Table

_TIME_TYPES = (DataType.DATE, DataType.TIMESTAMP)

# Anchor detection, in priority order: the created_at/inserted_at semantic
# hint (already produced by ddl_parser), then event-style names.
_ANCHOR_NAME_RE = re.compile(r"^(order|purchase|signup|placed|opened|start)_?(date|at|time)$")

_LIFECYCLE_RE = re.compile(
    r"^(shipped|delivered|cancelled|completed|paid|refunded|approved"
    r"|published|archived|closed|resolved|confirmed|deleted)_?(at|date|on)$"
)
_LAST_RE = re.compile(r"^last_(login|seen|active|used)(_?(at|on|date))?$")
_STATUS_NAME_RE = re.compile(r"^(status|state)$|_status$")

# Canonical progression rank for lifecycle stems and status values. A row's
# status fills lifecycle stages with rank <= the status's rank; terminal
# branch statuses (cancelled/refunded/deleted) fill only their own column.
_STAGE_RANK = {
    "pending": 0, "draft": 0, "open": 0, "new": 0,
    "processing": 1, "in_progress": 1,
    "confirmed": 2, "approved": 2,
    "paid": 3, "active": 3,
    "shipped": 4, "published": 4, "resolved": 4,
    "delivered": 5, "completed": 5, "closed": 5,
    "archived": 6,
}
_TERMINAL_STAGES = {"cancelled", "refunded", "deleted"}

_NEVER_UPDATED_RATE = 0.3   # fraction of rows where updated_at == created_at
_UPDATE_WINDOW_DAYS = 90    # updated_at lands within this window after the anchor
_STAGE_MIN_HOURS, _STAGE_MAX_HOURS = 2, 96  # lifecycle stage-to-stage offset


@dataclass
class TableContext:
    """Per-table anchor timestamps captured after coherence, index-aligned
    with the table's generated rows, so later tables (and the deferred-FK
    backfill) can use them as chronology floors."""
    anchor_col: str | None
    anchors: list = field(default_factory=list)


@dataclass
class TableCoherencePlan:
    anchor_col: str | None = None
    anchor_rewritable: bool = False
    updated_cols: list[str] = field(default_factory=list)
    lifecycle_cols: list[str] = field(default_factory=list)  # in chain order
    last_cols: list[str] = field(default_factory=list)
    status_col: str | None = None
    protected: set[str] = field(default_factory=set)


def _as_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


def _cast(v: datetime, dtype: DataType):
    return v.date() if dtype == DataType.DATE else v


def _stem(col_name: str) -> str:
    m = _LIFECYCLE_RE.match(col_name.lower())
    return m.group(1) if m else col_name.lower()


def _protected_columns(table: Table, deferred_cols: set[str]) -> set[str]:
    protected: set[str] = set(table.primary_key) | set(deferred_cols)
    for fk in table.foreign_keys:
        protected.update(fk.columns)
    protected.update(c.name for c in table.columns if c.is_unique)
    for group in table.unique_constraints:
        protected.update(group)
    for chk in table.check_constraints:
        if chk.parsed is None or chk.parsed.get("kind") == "col_compare":
            # Parsed two-column CHECKs are handled construct-to-satisfy by
            # engine step 3; unparsed CHECKs by the step-6 filter. Rewriting
            # either would risk breaking a constraint -- CHECK wins.
            protected.update(chk.columns_involved)
    return protected


def detect_plan(table: Table, deferred_cols: set[str] | None = None) -> TableCoherencePlan:
    plan = TableCoherencePlan(protected=_protected_columns(table, deferred_cols or set()))

    time_cols = [c for c in table.columns if c.dtype in _TIME_TYPES]

    anchor = next((c for c in time_cols if c.semantic_hint == "created_at"), None)
    if anchor is None:
        anchor = next((c for c in time_cols if _ANCHOR_NAME_RE.match(c.name.lower())), None)
    if anchor is not None:
        plan.anchor_col = anchor.name
        plan.anchor_rewritable = anchor.name not in plan.protected

    lifecycle = []
    for c in time_cols:
        name = c.name.lower()
        if c.name == plan.anchor_col:
            continue
        if c.semantic_hint == "updated_at":
            plan.updated_cols.append(c.name)
        elif _LAST_RE.match(name):
            plan.last_cols.append(c.name)
        elif _LIFECYCLE_RE.match(name):
            lifecycle.append(c.name)
    lifecycle.sort(key=lambda n: (_STAGE_RANK.get(_stem(n), 50), _stem(n) in _TERMINAL_STAGES))
    plan.lifecycle_cols = lifecycle

    if lifecycle:
        stems = {_stem(n) for n in lifecycle}
        for c in table.columns:
            if c.enum_values and _STATUS_NAME_RE.search(c.name.lower()):
                values = {str(v).lower() for v in c.enum_values}
                if values & (stems | set(_STAGE_RANK) | _TERMINAL_STAGES):
                    plan.status_col = c.name
                    break

    return plan


def _column_bounds_dt(table: Table, col_name: str, single_col_checks: list[dict]):
    bounds = apply_single_column_bounds(single_col_checks, col_name)
    return _as_dt(bounds.get("min")), _as_dt(bounds.get("max"))


def _clamp(v: datetime, lo: datetime | None, hi: datetime | None) -> datetime:
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def apply_coherence(
    table: Table,
    row_data: dict[str, list],
    n: int,
    rng: random.Random,
    ctx: dict[str, "TableContext"],
    config,
    generated_tables: dict[str, list[dict]],
    deferred_cols: set[str],
) -> None:
    """Engine step 7. Mutates row_data in place, then records this table's
    TableContext in `ctx` so it can act as a parent for later tables."""
    plan = detect_plan(table, deferred_cols)
    single_col_checks = [c.parsed for c in table.check_constraints if len(c.columns_involved) == 1]
    as_of = config.as_of if config.as_of is not None else DEFAULT_AS_OF
    cap = datetime(as_of.year, as_of.month, as_of.day)

    if plan.anchor_col is not None:
        anchor_dtype = table.get_column(plan.anchor_col).dtype
        anchors = row_data[plan.anchor_col]

        # --- b. cross-table floors from non-deferred FK picks -------------
        floors: list[datetime | None] = [None] * n
        has_floor_source = False
        if plan.anchor_rewritable:
            for fk in table.foreign_keys:
                if any(c in deferred_cols for c in fk.columns) or fk.ref_table == table.name:
                    continue
                pctx = ctx.get(fk.ref_table)
                if pctx is None or pctx.anchor_col is None:
                    continue
                parent_rows = generated_tables.get(fk.ref_table, [])
                if len(parent_rows) != len(pctx.anchors):
                    continue
                has_floor_source = True
                amap = {}
                for prow, a in zip(parent_rows, pctx.anchors):
                    key = tuple(prow.get(c) for c in fk.ref_columns)
                    amap[key] = _as_dt(a)
                for i in range(n):
                    key = tuple(row_data[c][i] for c in fk.columns)
                    if any(v is None for v in key):
                        continue  # NULL FK pick: no parent, no floor
                    a_dt = amap.get(key)
                    if a_dt is None:
                        continue  # parent has no usable anchor
                    if floors[i] is None or a_dt > floors[i]:
                        floors[i] = a_dt  # multi-FK rows take the max floor

        # --- c. chronology tie-in for self-referencing tables --------------
        # Sorting the anchor ascending makes "parent has an earlier index"
        # imply "parent is older" for the deferred self-FK backfill. Only
        # safe when the table has no cross-table floors of its own.
        has_self_ref = any(
            fk.ref_table == table.name and any(c in deferred_cols for c in fk.columns)
            for fk in table.foreign_keys
        )
        if has_self_ref and plan.anchor_rewritable and not has_floor_source:
            present = sorted((v for v in anchors if v is not None))
            it = iter(present)
            row_data[plan.anchor_col] = anchors = [
                next(it) if v is not None else None for v in anchors
            ]

        # --- b. anchor rewrite: floor + recency-skewed delta, capped at as_of
        if plan.anchor_rewritable:
            lo_b, hi_b = _column_bounds_dt(table, plan.anchor_col, single_col_checks)
            for i in range(n):
                if anchors[i] is None or floors[i] is None:
                    continue  # no parent floor: keep the engine's value
                f = floors[i]
                if f >= cap:
                    # Parent is already at/past as_of; accept slight forward
                    # drift over violating causality (documented approximation).
                    nv = f + timedelta(seconds=rng.randint(60, 3600))
                else:
                    span_s = int((cap - f).total_seconds())
                    nv = cap - timedelta(seconds=int(span_s * (rng.random() ** 2)))
                nv = _clamp(nv, lo_b, hi_b)
                anchors[i] = _cast(nv, anchor_dtype)

        # --- a. cross-column chronology, derived from the (corrected) anchor
        _apply_row_chronology(table, row_data, n, rng, plan, single_col_checks, cap)

    # Capture context for children / deferred backfill (even when the anchor
    # itself was CHECK-protected: its values still floor the children).
    ctx[table.name] = TableContext(
        anchor_col=plan.anchor_col,
        anchors=list(row_data[plan.anchor_col]) if plan.anchor_col else [],
    )


def _apply_row_chronology(
    table: Table,
    row_data: dict[str, list],
    n: int,
    rng: random.Random,
    plan: TableCoherencePlan,
    single_col_checks: list[dict],
    cap: datetime,
) -> None:
    anchors = row_data[plan.anchor_col]

    # updated_at / modified_at: anchor + U(0, min(90d, as_of - anchor)),
    # with a fixed fraction of rows never touched (updated == created).
    for col_name in plan.updated_cols:
        if col_name in plan.protected:
            continue
        column = table.get_column(col_name)
        lo_b, hi_b = _column_bounds_dt(table, col_name, single_col_checks)
        vals = row_data[col_name]
        for i in range(n):
            a_dt = _as_dt(anchors[i])
            if vals[i] is None or a_dt is None:
                continue  # preserve NULL cells; anchorless rows keep engine value
            if rng.random() < _NEVER_UPDATED_RATE:
                nv = a_dt
            else:
                span_s = int(min(timedelta(days=_UPDATE_WINDOW_DAYS), cap - a_dt).total_seconds())
                nv = a_dt + timedelta(seconds=int(span_s * rng.random())) if span_s > 0 else a_dt
            vals[i] = _cast(_clamp(nv, lo_b, hi_b), column.dtype)

    # last_login / last_seen / ...: anywhere between the anchor and as_of.
    for col_name in plan.last_cols:
        if col_name in plan.protected:
            continue
        column = table.get_column(col_name)
        lo_b, hi_b = _column_bounds_dt(table, col_name, single_col_checks)
        vals = row_data[col_name]
        for i in range(n):
            a_dt = _as_dt(anchors[i])
            if vals[i] is None or a_dt is None:
                continue
            span_s = int((cap - a_dt).total_seconds())
            nv = a_dt + timedelta(seconds=int(span_s * rng.random())) if span_s > 0 else a_dt
            vals[i] = _cast(_clamp(nv, lo_b, hi_b), column.dtype)

    # Lifecycle chain, optionally gated by the row's status value.
    live_lifecycle = [c for c in plan.lifecycle_cols if c not in plan.protected]
    if not live_lifecycle:
        return
    status_vals = row_data.get(plan.status_col) if plan.status_col else None
    col_meta = []
    for col_name in live_lifecycle:
        column = table.get_column(col_name)
        col_meta.append((
            col_name, column, _stem(col_name),
            _column_bounds_dt(table, col_name, single_col_checks),
        ))
    for i in range(n):
        a_dt = _as_dt(anchors[i])
        if a_dt is None:
            continue
        status = str(status_vals[i]).lower() if status_vals and status_vals[i] is not None else None
        status_rank = _STAGE_RANK.get(status) if status is not None else None
        status_terminal = status in _TERMINAL_STAGES if status is not None else False
        gated = status is not None and (status_rank is not None or status_terminal)

        prev = a_dt
        for col_name, column, stem, (lo_b, hi_b) in col_meta:
            vals = row_data[col_name]
            if gated:
                if status_terminal:
                    fill = stem == status
                elif stem in _TERMINAL_STAGES:
                    fill = False
                else:
                    fill = _STAGE_RANK.get(stem, 99) <= status_rank
            else:
                fill = vals[i] is not None  # no gate: keep the existing NULL pattern
            if not fill and not column.nullable:
                fill = True  # NOT NULL: fill anyway, the chain order still holds
            if fill:
                prev = prev + timedelta(
                    seconds=rng.randint(_STAGE_MIN_HOURS * 3600, _STAGE_MAX_HOURS * 3600)
                )
                vals[i] = _cast(_clamp(prev, lo_b, hi_b), column.dtype)
            else:
                vals[i] = None
