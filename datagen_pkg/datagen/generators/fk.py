from __future__ import annotations

import random

from ..schema_model import ForeignKey

# Zipf exponent for fanout="zipfian". Pure 1/rank (s=1.0) is harsher than
# real order data; 0.7 keeps a clear power-user head with a longer tail.
ZIPF_EXPONENT = 0.7


def expand_allocation(parent_pk_pool: list, per_parent: list[int], rng: random.Random) -> list:
    """Expand an exact per-parent allocation (per_parent[i] children for
    parent i) into a flat list of parent-key picks, then shuffle it with the
    seeded rng so children are not clustered by row index. Length checks are
    strict: the allocation must be index-aligned with the parent pool."""
    if len(per_parent) != len(parent_pk_pool):
        raise ValueError(
            f"Fanout allocation has {len(per_parent)} entries but the parent pool "
            f"has {len(parent_pk_pool)} rows; the allocation must be index-aligned "
            f"with the parent table's generated rows."
        )
    picks: list = []
    for value, count in zip(parent_pk_pool, per_parent):
        if count < 0:
            raise ValueError(f"Fanout allocation contains a negative count ({count}).")
        picks.extend([value] * count)
    rng.shuffle(picks)
    return picks


def generate_fk_column(
    fk: ForeignKey,
    n: int,
    parent_pk_pool: list,
    rng: random.Random,
    null_rate: float = 0.0,
    fanout: str = "zipfian",
    allocation: list[int] | None = None,
) -> list:
    """Generate n FK values by sampling from the parent table's already
    generated primary-key pool.

    fanout="zipfian" (default): a minority of parents get most of the children
        (a few power users own most orders) -- a perfectly even fan-out is
        itself a fake-data tell. Which parents form the "head" is decided by
        a seeded shuffle, so output stays deterministic.
    fanout="uniform": each child row independently picks a uniformly random
        parent.

    `allocation` (from a config/`--rows` fanout range like "5..20 per users"):
        an exact per-parent child count, index-aligned with the parent pool.
        When present it *overrides* the fanout distribution and any null rate
        -- parent i receives exactly allocation[i] children (assignment order
        is shuffled deterministically), so the drawn relationship is honored
        to the row.

    A nullable FK applies `null_rate` before sampling (default 0 -- MVP is
    conservative and only nulls FK values if the schema explicitly allows it
    and the caller opts in).
    """
    if not parent_pk_pool:
        raise ValueError(
            f"Cannot generate FK {fk.columns} -> {fk.ref_table}.{fk.ref_columns}: "
            f"parent table's primary key pool is empty. Check generation order."
        )

    if allocation is not None:
        picks = expand_allocation(parent_pk_pool, allocation, rng)
        if len(picks) != n:
            raise ValueError(
                f"Fanout allocation for FK {fk.columns} -> {fk.ref_table} sums to "
                f"{len(picks)} children but {n} rows were requested; the child "
                f"table's row count must equal the allocation total."
            )
        return picks

    if fanout == "zipfian":
        weights = [1.0 / (i + 1) ** ZIPF_EXPONENT for i in range(len(parent_pk_pool))]
        pool_order = list(parent_pk_pool)
        rng.shuffle(pool_order)
        picks = rng.choices(pool_order, weights=weights, k=n)
    else:
        picks = [rng.choice(parent_pk_pool) for _ in range(n)]

    if fk.nullable and null_rate > 0:
        picks = [None if rng.random() < null_rate else v for v in picks]

    return picks
