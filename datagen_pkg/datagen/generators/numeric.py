from __future__ import annotations

import random
import uuid

from ..schema_model import Column, DataType


DEFAULT_INT_RANGE = 1000  # sane default span when no CHECK bound narrows it


def generate_integer(column: Column, n: int, rng: random.Random, bounds: dict) -> list[int]:
    lo = int(bounds.get("min", 1))
    hi = int(bounds.get("max", lo + DEFAULT_INT_RANGE))
    if hi <= lo:
        hi = lo + 1
    return [rng.randint(lo, hi) for _ in range(n)]


def generate_numeric(column: Column, n: int, rng: random.Random, bounds: dict) -> list[float]:
    lo = float(bounds.get("min", 0.01))
    hi = float(bounds.get("max", 10_000.0))
    if hi <= lo:
        hi = lo + 1.0
    scale = column.scale if column.scale is not None else 2
    return [round(rng.uniform(lo, hi), scale) for _ in range(n)]


def generate_boolean(n: int, rng: random.Random) -> list[bool]:
    return [rng.choice([True, False]) for _ in range(n)]


def generate_uuid(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def generate_sequential_pk(n: int, start: int = 1) -> list[int]:
    return list(range(start, start + n))
