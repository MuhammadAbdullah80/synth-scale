from __future__ import annotations

import random


def generate_enum(values: list[str], n: int, rng: random.Random, weights: list[float] | None = None) -> list[str]:
    if weights:
        return rng.choices(values, weights=weights, k=n)
    return [rng.choice(values) for _ in range(n)]
