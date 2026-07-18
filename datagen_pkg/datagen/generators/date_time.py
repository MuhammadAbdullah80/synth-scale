from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from ..schema_model import Column

DEFAULT_DATE_WINDOW_DAYS = 730  # last 2 years, used when no bound is derivable

# Hour-of-day weights for timestamp generation (consumer profile): activity
# peaks 9:00-21:00, troughs 01:00-06:00. Indexed by hour 0-23.
HOUR_WEIGHTS = [
    2, 1, 1, 1, 1, 1,      # 00-05  night trough
    2, 4, 6, 8, 9, 9,      # 06-11  morning ramp
    10, 10, 9, 8, 8, 8,    # 12-17  daytime plateau
    9, 10, 9, 8, 6, 4,     # 18-23  evening peak, wind-down
]

WEEKEND_REROLL_P = 0.6  # chance a Sat/Sun timestamp is re-rolled once

# Fixed reference date used as the default window anchor. Deliberately a
# constant (NOT date.today()): the reproducibility contract is "same seed,
# same config -> byte-identical output", and a wall-clock anchor breaks that
# the moment the same command is re-run on a later day. Pass
# EngineConfig.as_of to move the window explicitly.
DEFAULT_AS_OF = date(2026, 1, 1)


def _to_date(value) -> date | None:
    """Coerce a bound value (date, datetime, ISO string, or None) to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _to_datetime(value) -> datetime | None:
    """Coerce a bound value (date, datetime, ISO string, or None) to a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def generate_date(
    column: Column, n: int, rng: random.Random, bounds: dict, as_of: date | None = None
) -> list[date]:
    anchor = as_of if as_of is not None else DEFAULT_AS_OF
    lo = _to_date(bounds.get("min"))
    hi = _to_date(bounds.get("max"))
    if lo is None:
        lo = (hi if hi is not None else anchor) - timedelta(days=DEFAULT_DATE_WINDOW_DAYS)
    if hi is None:
        # Anchor the upper end of the window; if the lower bound is already
        # past the anchor (e.g. CHECK (d >= '2030-01-01')), spread forward a
        # year so values aren't all squeezed onto one day.
        hi = anchor if anchor > lo else lo + timedelta(days=365)
    span = (hi - lo).days
    if span <= 0:
        span = 1
    # Quadratic mass toward the recent end of the window (a growing app has
    # more recent rows than old ones). Stays within [lo, hi] by construction.
    return [hi - timedelta(days=int(span * rng.random() ** 2)) for _ in range(n)]


def generate_timestamp(
    column: Column, n: int, rng: random.Random, bounds: dict, as_of: date | None = None
) -> list[datetime]:
    anchor = as_of if as_of is not None else DEFAULT_AS_OF
    lo = _to_datetime(bounds.get("min"))
    hi = _to_datetime(bounds.get("max"))
    if lo is None:
        base = hi if hi is not None else datetime(anchor.year, anchor.month, anchor.day)
        lo = base - timedelta(days=DEFAULT_DATE_WINDOW_DAYS)
    if hi is None:
        anchor_dt = datetime(anchor.year, anchor.month, anchor.day)
        hi = anchor_dt if anchor_dt > lo else lo + timedelta(days=365)
    span_days = max((hi - lo).days, 1)
    hours = range(24)
    values: list[datetime] = []
    for _ in range(n):
        # Recent-past growth curve: quadratic mass toward the upper end.
        day = hi - timedelta(days=int(span_days * rng.random() ** 2))
        # Weekday dip: a Sat/Sun pick is re-rolled once with 60% probability.
        if day.weekday() >= 5 and rng.random() < WEEKEND_REROLL_P:
            day = hi - timedelta(days=int(span_days * rng.random() ** 2))
        # Business/evening-hours clustering for the time of day.
        hour = rng.choices(hours, weights=HOUR_WEIGHTS)[0]
        ts = datetime(day.year, day.month, day.day, hour, rng.randint(0, 59), rng.randint(0, 59))
        if ts < lo:
            ts = lo
        elif ts > hi:
            ts = hi
        values.append(ts)
    return values


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
