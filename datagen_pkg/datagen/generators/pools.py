"""Correlated value pools (coherence layer, engine step 4.5).

A pool is a small packaged JSON file of internally-consistent records
(city<->state<->country<->zipcode, first_name<->gender, product<->category<->
tier<->price band). Columns whose semantic hints appear in a pool's `match`
list are generated *as a group*: one record is picked per row and every
matched column takes its field from that record, so a row can never say
"Karachi, Bavaria, Peru".

Pool files live in `datagen/pools/*.json` and are loaded via
importlib.resources. Users can supply extra pool directories (CLI `--pools`);
a user pool whose `name` matches a packaged pool replaces it wholesale.
"""
from __future__ import annotations

import json
import random
import warnings
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from faker import Faker

from ..schema_model import Column, DataType, Table
from .base import apply_single_column_bounds
from .numeric import charm_price, numeric_precision_cap

_TEXT_TYPES = (DataType.VARCHAR, DataType.TEXT, DataType.UNKNOWN)
_MONEY_TYPES = (DataType.NUMERIC, DataType.FLOAT)


@dataclass
class Pool:
    name: str
    match: list[str]  # semantic hints (= record field names) this pool binds to
    records: list[dict]


_packaged_cache: dict[str, Pool] | None = None


def _parse_pool(text: str, origin: str) -> Pool:
    data = json.loads(text)
    if not isinstance(data.get("records"), list) or not data["records"]:
        raise ValueError(f"Pool file {origin} has no 'records' list.")
    return Pool(
        name=str(data.get("name") or origin),
        match=[str(m) for m in data.get("match", [])],
        records=data["records"],
    )


def _load_packaged() -> dict[str, Pool]:
    global _packaged_cache
    if _packaged_cache is None:
        pools: dict[str, Pool] = {}
        pool_dir = resources.files("datagen") / "pools"
        for entry in sorted(pool_dir.iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".json"):
                pool = _parse_pool(entry.read_text(encoding="utf-8"), entry.name)
                pools[pool.name] = pool
        _packaged_cache = pools
    return _packaged_cache


def load_pools(extra_dirs: list[str] | None = None) -> dict[str, Pool]:
    """Packaged pools, optionally overlaid with user-supplied pool directories
    (later directories win; a user pool named like a packaged one replaces it)."""
    pools = dict(_load_packaged())
    for d in extra_dirs or []:
        base = Path(d)
        if not base.is_dir():
            raise ValueError(f"--pools directory not found: {d}")
        for f in sorted(base.glob("*.json")):
            pool = _parse_pool(f.read_text(encoding="utf-8"), str(f))
            pools[pool.name] = pool
    return pools


def detect_pool_groups(
    table: Table,
    pools: dict[str, Pool],
    generated_cols: set[str],
    single_col_checks: list[dict],
) -> list[tuple[Pool, list[tuple[str, str]]]]:
    """Return [(pool, [(column_name, record_field), ...]), ...] for columns not
    yet generated whose semantic hint appears in a pool's `match` list.
    Enum-valued columns (native or CHECK IN (...)) are never pool-bound --
    the DDL's own domain always wins."""
    groups: list[tuple[Pool, list[tuple[str, str]]]] = []
    claimed: set[str] = set()
    for pool_name in sorted(pools):
        pool = pools[pool_name]
        matched: list[tuple[str, str]] = []
        for col in table.columns:
            if col.name in generated_cols or col.name in claimed:
                continue
            if col.dtype not in _TEXT_TYPES:
                continue
            if col.enum_values:
                continue
            if apply_single_column_bounds(single_col_checks, col.name).get("in_list"):
                continue
            if col.semantic_hint in pool.match:
                matched.append((col.name, col.semantic_hint))
        if matched:
            claimed.update(c for c, _ in matched)
            groups.append((pool, matched))
    return groups


def _fallback_value(field: str, fake: Faker) -> str:
    from .text import _HINT_DISPATCH  # local import to avoid a cycle at module load

    gen = _HINT_DISPATCH.get(field)
    return gen(fake) if gen else fake.word()


def generate_pool_groups(
    table: Table,
    row_data: dict[str, list],
    generated_cols: set[str],
    n: int,
    rng: random.Random,
    fake: Faker,
    pools: dict[str, Pool],
    single_col_checks: list[dict],
    null_rate: float,
) -> None:
    """Engine step 4.5: generate every pool-bound column group. Mutates
    row_data / generated_cols in place. All randomness comes from `rng`."""
    for pool, matched in detect_pool_groups(table, pools, generated_cols, single_col_checks):
        records = pool.records
        unique_cols = [c for c, _ in matched if table.get_column(c).is_unique]
        if unique_cols:
            if n > len(records):
                # A 50-record pool cannot fill a UNIQUE column with n > 50 rows.
                # Drop the group so the normal Faker + uniqueness path handles it.
                warnings.warn(
                    f"Pool {pool.name!r} skipped for table {table.name!r}: UNIQUE "
                    f"column(s) {unique_cols} need {n} distinct values but the pool "
                    f"has only {len(records)} records. Falling back to Faker."
                )
                continue
            record_idx = rng.sample(range(len(records)), n)
        else:
            record_idx = [rng.randrange(len(records)) for _ in range(n)]

        for col_name, field in matched:
            column = table.get_column(col_name)
            values = []
            for idx in record_idx:
                v = records[idx].get(field)
                if v is None:
                    v = _fallback_value(field, fake)
                values.append(str(v))
            if column.max_length:
                values = [v[: column.max_length] for v in values]
            if column.nullable and null_rate > 0:
                values = [None if rng.random() < null_rate else v for v in values]
            row_data[col_name] = values
            generated_cols.add(col_name)

        # Price coupling: a money-hinted NUMERIC column in the same table as a
        # products-style group draws from the chosen record's price band.
        if any("price_min" in r for r in records):
            _couple_price(table, row_data, generated_cols, record_idx, records,
                          rng, single_col_checks, null_rate)


def _couple_price(
    table: Table,
    row_data: dict[str, list],
    generated_cols: set[str],
    record_idx: list[int],
    records: list[dict],
    rng: random.Random,
    single_col_checks: list[dict],
    null_rate: float,
) -> None:
    money_col: Column | None = None
    for col in table.columns:
        if col.name in generated_cols:
            continue
        if col.semantic_hint == "money" and col.dtype in _MONEY_TYPES and not col.is_unique:
            money_col = col
            break
    if money_col is None:
        return

    bounds = apply_single_column_bounds(single_col_checks, money_col.name)
    scale = money_col.scale if money_col.scale is not None else 2
    cap = numeric_precision_cap(money_col)
    lo_bound = bounds.get("min")
    hi_bound = bounds.get("max")
    if cap is not None:
        hi_bound = min(hi_bound, cap) if hi_bound is not None else cap

    values = []
    for idx in record_idx:
        rec = records[idx]
        lo = float(rec.get("price_min", 1))
        hi = float(rec.get("price_max", max(lo, 1) * 2))
        v = rng.uniform(lo, hi)
        if scale >= 2:
            v = charm_price(v, rng)
            v = min(max(v, lo), hi)  # charm ending must not leave the record's band
        v = round(v, scale)
        if lo_bound is not None and v < float(lo_bound):
            v = round(float(lo_bound), scale)
        if hi_bound is not None and v > float(hi_bound):
            v = round(float(hi_bound), scale)
        values.append(v)
    if money_col.nullable and null_rate > 0:
        values = [None if rng.random() < null_rate else v for v in values]
    row_data[money_col.name] = values
    generated_cols.add(money_col.name)
