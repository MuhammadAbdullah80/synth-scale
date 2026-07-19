"""Generation service for the Synth-Scale web playground.

Wraps the datagen engine (`parse_ddl` -> caps -> `run_engine` -> `validate`)
behind hard server-side caps and a wall-time budget. The DDL is untrusted
input handed to sqlglot only -- it is NEVER executed against any database and
no DB code path exists in this app.

All output serialization (SQL text, CSV zip) happens in memory; nothing is
written to disk.
"""
from __future__ import annotations

import csv
import io
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, datetime
from typing import Any, Callable

from datagen import EngineConfig, parse_ddl, run_engine, validate
from datagen.dependency_graph import build_generation_plan
from datagen.generators.base import DomainExhaustedError

# ---------------------------------------------------------------------------
# Hard caps (non-negotiable, enforced before generation)
# ---------------------------------------------------------------------------
MAX_DDL_BYTES = 50 * 1024      # 50 KB of DDL text
MAX_TABLES = 15
MAX_TOTAL_COLUMNS = 120
MAX_TOTAL_ROWS = 1_000         # total generated rows across all tables
PREVIEW_ROWS = 10

# Wall-time budget for generate+validate, seconds. Module-level so tests can
# monkeypatch it; env override for deploys.
TIMEOUT_SECONDS = float(os.environ.get("SYNTH_SCALE_TIMEOUT", "10"))

# Mirrors the CLI's integer --rows heuristic (datagen/cli.py).
CHILD_MULTIPLIER = 3
CHILD_CAP_FACTOR = 10

SQL_BATCH_SIZE = 500

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="synthgen")


class CapError(Exception):
    """A request exceeded a hard cap. Maps to a 4xx JSON error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class GenerationTimeout(Exception):
    pass


def run_with_timeout(fn: Callable[[], Any], timeout: float | None = None) -> Any:
    """Run `fn` on a worker thread, raising GenerationTimeout if it exceeds
    the budget. Python threads can't be killed, so a timed-out generation
    keeps running to completion in the background -- the caps above keep that
    bounded; the timeout exists so the *request* never hangs."""
    if timeout is None:
        timeout = TIMEOUT_SECONDS
    future = _EXECUTOR.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeout:
        future.cancel()
        raise GenerationTimeout(
            f"generation exceeded the {timeout:.0f}s time budget for the web "
            f"playground; use the CLI (pip install synth-scale) for bigger runs"
        ) from None


# ---------------------------------------------------------------------------
# Row distribution
# ---------------------------------------------------------------------------
def distribute_rows(schema, order: list[str], base: int) -> dict[str, int]:
    """Per-table row counts from a single base number.

    Same heuristic as the CLI's integer `--rows` form: every table gets
    `base` rows, except a table with FK parents gets 3x the largest of its
    parents' counts, capped at 10x base (self-referencing FKs don't count as
    parents). Then, because the playground caps TOTAL rows at
    MAX_TOTAL_ROWS, the whole plan is scaled down proportionally (floor,
    minimum 1 row per table) until the total fits.
    """
    counts: dict[str, int] = {}
    cap = CHILD_CAP_FACTOR * base
    for tname in order:
        table = schema.tables[tname]
        parent_counts = [
            counts[fk.ref_table]
            for fk in table.foreign_keys
            if fk.ref_table != tname and fk.ref_table in counts
        ]
        if parent_counts:
            counts[tname] = min(CHILD_MULTIPLIER * max(parent_counts), cap)
        else:
            counts[tname] = base

    total = sum(counts.values())
    if total > MAX_TOTAL_ROWS:
        scale = MAX_TOTAL_ROWS / total
        counts = {t: max(1, int(n * scale)) for t, n in counts.items()}
        # Flooring plus the min-1 bump can leave us a hair over; trim the
        # largest tables until the total fits.
        while sum(counts.values()) > MAX_TOTAL_ROWS:
            biggest = max(counts, key=lambda t: counts[t])
            if counts[biggest] <= 1:
                break
            counts[biggest] -= 1
    return counts


# ---------------------------------------------------------------------------
# Cap checks + pipeline
# ---------------------------------------------------------------------------
def check_ddl_size(ddl: str) -> None:
    size = len(ddl.encode("utf-8"))
    if size > MAX_DDL_BYTES:
        raise CapError(
            413,
            f"DDL is {size:,} bytes; the playground cap is {MAX_DDL_BYTES:,} "
            f"bytes (50 KB). Use the CLI for bigger schemas.",
        )


def check_schema_caps(schema) -> None:
    n_tables = len(schema.tables)
    if n_tables == 0:
        raise CapError(422, "No CREATE TABLE statements found in the DDL.")
    if n_tables > MAX_TABLES:
        raise CapError(
            422,
            f"Schema has {n_tables} tables; the playground cap is "
            f"{MAX_TABLES}. Use the CLI for bigger schemas.",
        )
    n_columns = sum(len(t.columns) for t in schema.tables.values())
    if n_columns > MAX_TOTAL_COLUMNS:
        raise CapError(
            422,
            f"Schema has {n_columns} columns in total; the playground cap is "
            f"{MAX_TOTAL_COLUMNS}. Use the CLI for bigger schemas.",
        )


def generate(ddl: str, rows: int, seed: int):
    """Full pipeline: parse -> caps -> distribute -> generate -> validate.

    Returns (schema, order, row_counts, generated, report).
    Raises CapError (4xx) for anything over the caps, malformed DDL, an
    exhausted value domain, or a blown time budget.
    """
    check_ddl_size(ddl)
    if not (1 <= rows <= MAX_TOTAL_ROWS):
        raise CapError(422, f"rows must be between 1 and {MAX_TOTAL_ROWS}.")

    try:
        schema = parse_ddl(ddl)
        plan = build_generation_plan(schema)
    except Exception as exc:  # untrusted input: any parse failure is a 422
        raise CapError(422, f"Could not parse DDL: {exc}") from exc

    check_schema_caps(schema)
    row_counts = distribute_rows(schema, plan.order, rows)

    def work():
        generated = run_engine(schema, row_counts, EngineConfig(seed=seed), plan=plan)
        report = validate(schema, generated, seed=seed)
        return generated, report

    try:
        generated, report = run_with_timeout(work)
    except GenerationTimeout as exc:
        raise CapError(408, str(exc)) from exc
    except DomainExhaustedError as exc:
        raise CapError(
            422,
            f"Generation gave up: {exc} Try fewer rows or wider column "
            f"domains (this usually means a UNIQUE/CHECK domain is smaller "
            f"than the requested row count).",
        ) from exc

    return schema, plan.order, row_counts, generated, report


# ---------------------------------------------------------------------------
# Serialization (all in memory)
# ---------------------------------------------------------------------------
def _json_value(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (date, datetime)):
        return str(v)  # 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS'
    return str(v)


def preview_payload(schema, order, row_counts, generated, report, seed: int, requested_rows: int) -> dict:
    tables = []
    for name in order:
        table = schema.tables[name]
        cols = table.column_names()
        rows = generated[name]
        tables.append(
            {
                "name": name,
                "columns": cols,
                "rows": [[_json_value(r[c]) for c in cols] for r in rows[:PREVIEW_ROWS]],
                "total_rows": len(rows),
            }
        )
    return {
        "seed": seed,
        "requested_rows": requested_rows,
        "order": order,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
        "tables": tables,
        "validation": {
            "constraints_checked": report.constraints_checked,
            "violations_found": report.violations_found,
            "warnings": report.warnings,
            "errors": report.errors,
        },
    }


def _sql_literal(value) -> str:
    # Kept in sync with datagen/output/sql_writer.py (which writes to disk;
    # the playground never touches disk, hence this in-memory twin).
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (date, datetime)):
        return f"'{value.isoformat()}'"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def sql_text(schema, generated, order: list[str], seed: int) -> str:
    lines = [
        f"-- synth-scale playground export (seed={seed}, deterministic)",
        "-- Tables are ordered FK-safe; the whole load is one transaction.",
        "BEGIN;",
        "",
    ]
    for table_name in order:
        table = schema.tables[table_name]
        rows = generated[table_name]
        cols = table.column_names()
        col_list = ", ".join(f'"{c}"' for c in cols)
        for i in range(0, len(rows), SQL_BATCH_SIZE):
            batch = rows[i : i + SQL_BATCH_SIZE]
            values_sql = ",\n".join(
                "  (" + ", ".join(_sql_literal(row[c]) for c in cols) + ")"
                for row in batch
            )
            lines.append(f'INSERT INTO "{table_name}" ({col_list}) VALUES\n{values_sql};')
            lines.append("")
    lines.append("COMMIT;")
    return "\n".join(lines)


def csv_zip_bytes(schema, generated, order: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for table_name in order:
            table = schema.tables[table_name]
            rows = generated[table_name]
            sio = io.StringIO(newline="")
            writer = csv.DictWriter(sio, fieldnames=table.column_names(), lineterminator="\r\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            zf.writestr(f"{table_name}.csv", sio.getvalue())
    return buf.getvalue()
