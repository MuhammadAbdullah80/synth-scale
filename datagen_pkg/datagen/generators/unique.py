from __future__ import annotations

from typing import Callable

from .base import MAX_UNIQUE_RETRIES, DomainExhaustedError


def generate_unique(generate_one: Callable[[], object], n: int, label: str = "column") -> list:
    """Call `generate_one()` repeatedly, keeping only values not seen before,
    until `n` unique values are collected. Fails fast with a clear error if the
    domain looks exhausted (many consecutive collisions) rather than looping
    forever -- this is the correct behavior when e.g. a UNIQUE column over a
    small enum is asked to produce more rows than the domain supports.
    """
    seen: set = set()
    out: list = []
    consecutive_misses = 0

    while len(out) < n:
        candidate = generate_one()
        key = candidate if not isinstance(candidate, list) else tuple(candidate)
        if key in seen:
            consecutive_misses += 1
            if consecutive_misses > MAX_UNIQUE_RETRIES:
                raise DomainExhaustedError(
                    f"Could not generate {n} unique values for {label}: the feasible "
                    f"domain appears smaller than the requested row count (got "
                    f"{len(out)} unique values before {MAX_UNIQUE_RETRIES} consecutive "
                    f"collisions). Reduce the row count, widen the constraint, or "
                    f"supply a custom generator for this column."
                )
            continue
        consecutive_misses = 0
        seen.add(key)
        out.append(candidate)

    return out
