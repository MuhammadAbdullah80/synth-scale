from __future__ import annotations

import datetime
import random

from faker import Faker

from ..schema_model import Column, DataType
from . import date_time, enum_gen, numeric, text
from .base import apply_single_column_bounds
from .unique import generate_unique

NULL_RATE_DEFAULT = 0.0  # nullable independent columns: fraction set to None


def generate_independent_column(
    column: Column,
    n: int,
    rng: random.Random,
    fake: Faker,
    check_parsed_list: list[dict],
    null_rate: float = NULL_RATE_DEFAULT,
    as_of: datetime.date | None = None,
) -> list:
    """Generate n values for a single, constraint-independent column.

    Dispatch order: enum values (native ENUM or CHECK IN (...)) > type-based
    generator using any single-column CHECK bounds found for this column.
    Wraps in the uniqueness retry loop if column.is_unique. `as_of` anchors
    the default date/timestamp window (see generators/date_time.py).
    """
    bounds = apply_single_column_bounds(check_parsed_list, column.name)

    def _one_batch(count: int) -> list:
        if column.enum_values:
            return enum_gen.generate_enum(column.enum_values, count, rng)
        if bounds.get("in_list"):
            return enum_gen.generate_enum(bounds["in_list"], count, rng)

        if column.dtype in (DataType.INTEGER, DataType.BIGINT, DataType.SMALLINT):
            return numeric.generate_integer(column, count, rng, bounds)
        if column.dtype == DataType.NUMERIC or column.dtype == DataType.FLOAT:
            return numeric.generate_numeric(column, count, rng, bounds)
        if column.dtype == DataType.BOOLEAN:
            return numeric.generate_boolean(count, rng, column)
        if column.dtype == DataType.UUID:
            return numeric.generate_uuid(count, rng)
        if column.dtype == DataType.DATE:
            return date_time.generate_date(column, count, rng, bounds, as_of=as_of)
        if column.dtype == DataType.TIMESTAMP:
            return date_time.generate_timestamp(column, count, rng, bounds, as_of=as_of)
        # VARCHAR / TEXT / UNKNOWN fallback
        return text.generate_text(column, count, rng, fake)

    if column.is_unique:
        batch_cache: list = []

        def _one():
            if not batch_cache:
                batch_cache.extend(_one_batch(max(n, 16)))
            return batch_cache.pop()

        values = generate_unique(_one, n, label=f"{column.name} (UNIQUE)")
    else:
        values = _one_batch(n)

    if column.nullable and null_rate > 0:
        values = [None if rng.random() < null_rate else v for v in values]

    return values
