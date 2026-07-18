from __future__ import annotations

import random

from faker import Faker

from ..schema_model import Column

# Maps a semantic_hint (see ddl_parser._SEMANTIC_PATTERNS) to a zero-arg
# callable on a Faker instance. Extend this dict to add more realism without
# touching any other part of the engine.
_HINT_DISPATCH = {
    "email": lambda f: f.unique.email(),
    "phone": lambda f: f.phone_number(),
    "first_name": lambda f: f.first_name(),
    "last_name": lambda f: f.last_name(),
    "full_name": lambda f: f.name(),
    "company": lambda f: f.company(),
    "job_title": lambda f: f.job(),
    "street_address": lambda f: f.street_address(),
    "city": lambda f: f.city(),
    "state": lambda f: f.state(),
    "country": lambda f: f.country(),
    "zipcode": lambda f: f.postcode(),
    "url": lambda f: f.url(),
    "ipv4": lambda f: f.ipv4(),
    "paragraph": lambda f: f.paragraph(nb_sentences=2),
}


def generate_text(column: Column, n: int, rng: random.Random, fake: Faker) -> list[str]:
    if column.semantic_hint and column.semantic_hint in _HINT_DISPATCH:
        gen = _HINT_DISPATCH[column.semantic_hint]
        values = [gen(fake) for _ in range(n)]
    elif column.semantic_hint == "money":
        # 'money' hint on a text/varchar column (rare) -- fall back to generic.
        values = [f"{rng.uniform(1, 1000):.2f}" for _ in range(n)]
    else:
        # No semantic hint recognized: generic fallback. Uses a couple of
        # realistic-ish word combinations rather than pure gibberish so output
        # is at least readable.
        values = [fake.catch_phrase() for _ in range(n)]

    max_len = column.max_length
    if max_len:
        values = [v[:max_len] for v in values]
    return values
