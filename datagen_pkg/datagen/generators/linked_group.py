"""Generation for columns that cannot be treated as independent per-column
lists: composite PK/UNIQUE tuples, and multi-column CHECK constraints
(`start_date < end_date`, `min_price <= max_price`, etc.).

The core idea (see the design write-up): construct the dependent column
FROM the independent one so the constraint is satisfied by construction,
instead of generating both independently and retrying on failure. Retry is
still used as a fallback for composite-uniqueness collisions, where
construction doesn't apply.
"""
from __future__ import annotations

import random
from datetime import date, datetime

from .base import MAX_UNIQUE_RETRIES, DomainExhaustedError
from .date_time import derive_dependent_date


def generate_composite_unique_tuples(
    column_generators: dict[str, "callable"], n: int, label: str = "composite key"
) -> dict[str, list]:
    """column_generators: {col_name: zero-arg callable returning one value}.
    Generates n row-tuples such that the (col_a, col_b, ...) combination is
    unique across all n rows, retrying whole tuples on collision.
    """
    seen: set = set()
    columns = list(column_generators.keys())
    out: dict[str, list] = {c: [] for c in columns}
    consecutive_misses = 0

    while len(out[columns[0]]) < n:
        candidate = tuple(column_generators[c]() for c in columns)
        if candidate in seen:
            consecutive_misses += 1
            if consecutive_misses > MAX_UNIQUE_RETRIES:
                raise DomainExhaustedError(
                    f"Could not generate {n} unique combinations for {label} "
                    f"({', '.join(columns)}): feasible combination space appears "
                    f"smaller than requested row count."
                )
            continue
        consecutive_misses = 0
        seen.add(candidate)
        for c, v in zip(columns, candidate):
            out[c].append(v)

    return out


def derive_dependent_column(
    base_values: list, op: str, rng: random.Random, min_delta: float | int = 1, max_delta: float | int = 90
) -> list:
    """Construct-to-satisfy for a two-column CHECK (`left <op> right`), called
    with base_values = the already-generated *right-hand* column's values, so
    the returned list is a valid left-hand column: left <op> right holds for
    every row by construction.
    """
    if not base_values:
        return []
    if isinstance(base_values[0], (date, datetime)):
        return derive_dependent_date(base_values, op, rng, int(min_delta), int(max_delta))

    sign = 1 if op in (">", ">=") else -1
    return [b + sign * rng.uniform(min_delta, max_delta) if b is not None else None for b in base_values]
