from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from ..schema_model import Column

DEFAULT_DATE_WINDOW_DAYS = 730  # last 2 years, used when no bound is derivable


def generate_date(column: Column, n: int, rng: random.Random, bounds: dict) -> list[date]:
    today = date.today()
    lo = bounds.get("min_date", today - timedelta(days=DEFAULT_DATE_WINDOW_DAYS))
    hi = bounds.get("max_date", today)
    span = (hi - lo).days
    if span <= 0:
        span = 1
    return [lo + timedelta(days=rng.randint(0, span)) for _ in range(n)]


def generate_timestamp(column: Column, n: int, rng: random.Random, bounds: dict) -> list[datetime]:
    dates = generate_date(column, n, rng, bounds)
    return [
        datetime(d.year, d.month, d.day, rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
        for d in dates
    ]


def derive_dependent_date(
    base_values: list[date], op: str, rng: random.Random, min_delta_days: int = 1, max_delta_days: int = 90
) -> list[date]:
    """Construct-to-satisfy for a two-column CHECK like `end_date > start_date`:
    derive the dependent column from the independent one plus a random delta,
    instead of generating both independently and hoping the check passes.
    """
    sign = 1 if op in (">", ">=") else -1
    return [
        b + sign * timedelta(days=rng.randint(min_delta_days, max_delta_days)) if b is not None else None
        for b in base_values
    ]
