from __future__ import annotations

import math
import random
import re
import uuid

from ..schema_model import Column
from .base import DomainExhaustedError


DEFAULT_INT_RANGE = 1000  # sane default span when no CHECK bound narrows it

# Boolean name-based skew (coherence quick win): flags like is_active are
# overwhelmingly True in real data, is_deleted overwhelmingly False.
_TRUE_HEAVY_RE = re.compile(
    r"is_?(active|verified|enabled|approved|visible|published|confirmed)"
    r"|^(active|enabled|verified)$"
)
_FALSE_HEAVY_RE = re.compile(
    r"is_?(deleted|banned|archived|suspended|disabled|blocked|flagged)"
    r"|^(deleted|archived|banned)$"
)
TRUE_HEAVY_P = 0.85
FALSE_HEAVY_P = 0.08


def charm_price(v: float, rng: random.Random) -> float:
    """Retail 'charm' ending: 70% x.99, 15% x.95, 15% x.00."""
    r = rng.random()
    if r < 0.70:
        ending = 0.99
    elif r < 0.85:
        ending = 0.95
    else:
        ending = 0.0
    return math.floor(v) + ending


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

    # Money-hinted columns (price/amount/cost/...): log-uniform magnitudes
    # with retail charm endings, clamped back inside [lo, hi] so CHECK bounds
    # and NUMERIC precision always win. Skipped for UNIQUE columns (charm
    # endings collapse the value space) and scales that can't hold cents.
    if column.semantic_hint == "money" and not column.is_unique and scale >= 2:
        log_lo = math.log(max(lo, 0.01))
        log_hi = math.log(max(hi, 0.02))
        values = []
        for _ in range(n):
            v = charm_price(math.exp(rng.uniform(log_lo, log_hi)), rng)
            if v < lo or v > hi:
                v = min(max(v, lo), hi)
            values.append(round(v, scale))
        return values

    return [round(rng.uniform(lo, hi), scale) for _ in range(n)]


def generate_boolean(n: int, rng: random.Random, column: Column | None = None) -> list[bool]:
    p_true = 0.5
    if column is not None:
        name = column.name.lower()
        if _TRUE_HEAVY_RE.search(name):
            p_true = TRUE_HEAVY_P
        elif _FALSE_HEAVY_RE.search(name):
            p_true = FALSE_HEAVY_P
    return [rng.random() < p_true for _ in range(n)]


def generate_uuid(n: int, rng: random.Random) -> list[str]:
    # Drawn from the seeded engine RNG so the same seed reproduces the same
    # UUIDs (still valid random-style version-4 UUIDs).
    return [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(n)]


def generate_sequential_pk(n: int, start: int = 1) -> list[int]:
    return list(range(start, start + n))
