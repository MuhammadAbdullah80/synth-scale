from __future__ import annotations

import random

from ..schema_model import ForeignKey


def generate_fk_column(
    fk: ForeignKey,
    n: int,
    parent_pk_pool: list,
    rng: random.Random,
    null_rate: float = 0.0,
    fanout: str = "uniform",
) -> list:
    """Generate n FK values by sampling from the parent table's already
    generated primary-key pool.

    fanout="uniform": each child row independently picks a uniformly random
        parent -> natural many-to-one distribution, most common case.
    fanout="zipfian": a minority of parents get most of the children (e.g.
        a few power users own most orders) -- more realistic for some domains,
        available for a future iteration without changing the call site.

    A nullable FK applies `null_rate` before sampling (default 0 -- MVP is
    conservative and only nulls FK values if the schema explicitly allows it
    and the caller opts in).
    """
    if not parent_pk_pool:
        raise ValueError(
            f"Cannot generate FK {fk.columns} -> {fk.ref_table}.{fk.ref_columns}: "
            f"parent table's primary key pool is empty. Check generation order."
        )

    if fanout == "zipfian":
        weights = [1.0 / (i + 1) for i in range(len(parent_pk_pool))]
        pool_order = list(parent_pk_pool)
        rng.shuffle(pool_order)
        picks = rng.choices(pool_order, weights=weights, k=n)
    else:
        picks = [rng.choice(parent_pk_pool) for _ in range(n)]

    if fk.nullable and null_rate > 0:
        picks = [None if rng.random() < null_rate else v for v in picks]

    return picks
