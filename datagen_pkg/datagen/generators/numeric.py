from __future__ import annotations

import random
import uuid

from ..schema_model import Column
from .base import DomainExhaustedError


DEFAULT_INT_RANGE = 1000  # sane default span when no CHECK bound narrows it


def generate_integer(column: Column, n: int, rng: random.Random, bounds: dict) -> list[int]:
    lo = int(bounds.get("min", 1))
    hi = int(bounds.get("max", lo + DEFAULT_INT_RANGE))
    if hi <= lo:
        hi = lo + 1
    return [rng.randint(lo, hi) for _ in range(n)]


def numeric_precision_cap(column: Column) -> float | None:
    """Largest absolute value a NUMERIC(p,s) column can hold, e.g.
    NUMERIC(4,2) -> 99.99. None when the column has no declared precision."""
    if column.precision is None:
        return None
    scale = column.scale if column.scale is not None else 0
    return 10 ** (column.precision - scale) - 10 ** (-scale)


def generate_numeric(column: Column, n: int, rng: random.Random, bounds: dict) -> list[float]:
    scale = column.scale if column.scale is not None else 2
    lo = float(bounds.get("min", 0.01))
    hi = float(bounds.get("max", 10_000.0))
    cap = numeric_precision_cap(column)
    if cap is not None:
        # NUMERIC(p,s) can hold at most 10**(p-s) - 10**-s in absolute value;
        # intersect that with any CHECK bounds.
        hi = min(hi, cap)
        lo = max(lo, -cap)
        if lo > hi:
            raise DomainExhaustedError(
                f"Column {column.name!r}: CHECK bounds require values >= {lo}, but "
                f"NUMERIC({column.precision},{column.scale}) can hold at most {cap}."
            )
    if hi <= lo:
        hi = lo + 1.0
        if cap is not None:
            hi = min(hi, cap)
    return [round(rng.uniform(lo, hi), scale) for _ in range(n)]


def generate_boolean(n: int, rng: random.Random) -> list[bool]:
    return [rng.choice([True, False]) for _ in range(n)]


def generate_uuid(n: int, rng: random.Random) -> list[str]:
    # Drawn from the seeded engine RNG so the same seed reproduces the same
    # UUIDs (still valid random-style version-4 UUIDs).
    return [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(n)]


def generate_sequential_pk(n: int, start: int = 1) -> list[int]:
    return list(range(start, start + n))
