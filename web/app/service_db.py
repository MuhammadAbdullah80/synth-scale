"""Generation service for the "Connect to Supabase" web flow: the read-only
counterpart to service.py's paste-DDL path, sourcing the schema from a live
database instead of DDL text.

Security posture (see also dbsafety.py):
- The connection string is used for exactly one request, then goes out of
  scope. It is never written to disk, never included in a log line, and
  never persisted anywhere.
- assert_public_host() runs before any socket is opened -- see dbsafety.py
  for what it blocks and its documented limitation.
- Read-only by construction: this module only ever calls introspect_db
  (SELECTs against information_schema/pg_catalog). There is no code path
  here -- or anywhere in web/ -- that writes to the caller's database.
- A short libpq-level connect timeout plus a wall-time budget around the
  whole introspection call bound how long a slow/unreachable/hostile host
  can occupy a worker thread.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from datagen import EngineConfig, run_engine, validate
from datagen.dburl import mask_secret, redact_db_url
from datagen.dependency_graph import build_generation_plan
from datagen.generators.base import DomainExhaustedError
from datagen.introspect import introspect_db

from . import service
from .dbsafety import UnsafeDatabaseURLError, assert_public_host

# libpq-level TCP connect timeout (seconds) -- how long we'll wait for the
# target host to even accept a connection.
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("SYNTH_SCALE_DB_CONNECT_TIMEOUT", "5"))

# Wall-time budget around the whole introspect_db() call (connect + the
# information_schema/pg_catalog queries), separate from the CPU-bound
# generation budget in service.TIMEOUT_SECONDS.
INTROSPECT_TIMEOUT_SECONDS = float(os.environ.get("SYNTH_SCALE_DB_TIMEOUT", "12"))


def generate_from_db(db_url: str, rows: int, seed: int):
    """Full read-only pipeline: validate -> introspect -> caps -> distribute
    -> generate -> validate. Returns (schema, order, row_counts, generated,
    report), the same shape as service.generate(). Raises service.CapError
    (4xx) for anything unsafe, over the caps, unreachable, or too slow.
    """
    if not (1 <= rows <= service.MAX_TOTAL_ROWS):
        raise service.CapError(422, f"rows must be between 1 and {service.MAX_TOTAL_ROWS}.")

    try:
        normalized = assert_public_host(db_url)
    except UnsafeDatabaseURLError as exc:
        raise service.CapError(422, str(exc)) from exc

    password = urlsplit(normalized).password
    target = redact_db_url(normalized)

    def introspect_work():
        return introspect_db(normalized, connect_timeout=CONNECT_TIMEOUT_SECONDS)

    try:
        schema = service.run_with_timeout(introspect_work, timeout=INTROSPECT_TIMEOUT_SECONDS)
    except service.GenerationTimeout as exc:
        raise service.CapError(
            408,
            f"could not read the schema from {target} within "
            f"{INTROSPECT_TIMEOUT_SECONDS:.0f}s -- check the connection string "
            "and that the database is reachable from the public internet.",
        ) from exc
    except Exception as exc:  # untrusted target: any connection/query failure is a 422
        detail = mask_secret(str(exc), password)
        raise service.CapError(422, f"could not read schema from {target}: {detail}") from exc

    service.check_schema_caps(schema)
    plan = build_generation_plan(schema)
    row_counts = service.distribute_rows(schema, plan.order, rows)

    def work():
        generated = run_engine(schema, row_counts, EngineConfig(seed=seed), plan=plan)
        report = validate(schema, generated, seed=seed)
        return generated, report

    try:
        generated, report = service.run_with_timeout(work)
    except service.GenerationTimeout as exc:
        raise service.CapError(408, str(exc)) from exc
    except DomainExhaustedError as exc:
        raise service.CapError(
            422,
            f"Generation gave up: {exc} Try fewer rows (this usually means a "
            f"UNIQUE/CHECK domain in your schema is smaller than the requested "
            f"row count).",
        ) from exc

    return schema, plan.order, row_counts, generated, report
