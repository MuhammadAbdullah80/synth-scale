from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .ddl_parser import parse_ddl
from .dependency_graph import build_generation_plan
from .engine import EngineConfig, run_engine
from .output import load_to_db, write_csv, write_sql
from .validator import validate


def _parse_rows_arg(rows_arg: str) -> dict[str, int]:
    row_counts = {}
    for pair in rows_arg.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"Invalid --rows entry {pair!r}, expected table=count")
        table, count = pair.split("=", 1)
        row_counts[table.strip()] = int(count.strip())
    return row_counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="datagen",
        description="Generate deterministic, referentially-consistent test data from a SQL DDL schema.",
    )
    parser.add_argument("--ddl", required=True, help="Path to a .sql file containing CREATE TABLE statements")
    parser.add_argument(
        "--rows", required=True,
        help="Comma-separated table=count pairs, e.g. users=100,orders=500,order_items=1500",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic output (default: 42)")
    parser.add_argument("--format", choices=["csv", "sql", "db"], default="csv")
    parser.add_argument("--out", default="./out", help="Output directory (csv) or file (sql). Ignored for db.")
    parser.add_argument("--db-url", help="SQLAlchemy connection URL, required when --format db")
    parser.add_argument("--dialect", default="postgres", help="DDL SQL dialect for sqlglot (default: postgres)")
    parser.add_argument("--null-rate", type=float, default=0.05, help="Default null rate for nullable columns")
    parser.add_argument("--fk-null-rate", type=float, default=0.1, help="Default null rate for nullable FK columns")
    parser.add_argument(
        "--deferred-fk-null-rate", type=float, default=0.2,
        help="Null rate for self-referential/cycle-broken FKs (e.g. employees with no manager)",
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="Anchor date for default date/timestamp windows (default: a fixed built-in "
             "date, so the same seed gives identical output regardless of the run day)",
    )
    parser.add_argument(
        "--fanout", choices=["uniform", "zipfian"], default="zipfian",
        help="FK sampling distribution (zipfian = a few parents get most children, "
             "the realistic default; uniform = perfectly even fan-out)",
    )
    parser.add_argument(
        "--no-coherence", action="store_true",
        help="Disable the coherence layer (correlated pools, updated_at >= created_at, "
             "child rows newer than their parents, anchor-sorted hierarchies)",
    )
    parser.add_argument(
        "--root-fraction", type=float, default=None, metavar="FRACTION",
        help="Fraction of rows in a self-referencing hierarchy that are roots with a "
             "NULL parent (default: the --deferred-fk-null-rate value)",
    )
    parser.add_argument(
        "--pools", action="append", default=[], metavar="DIR",
        help="Extra directory of *.json correlated-value pools (repeatable). A pool "
             "whose name matches a packaged pool (geo, person, products) replaces it.",
    )

    args = parser.parse_args(argv)

    ddl_path = Path(args.ddl)
    if not ddl_path.exists():
        print(f"error: DDL file not found: {ddl_path}", file=sys.stderr)
        return 1
    ddl_text = ddl_path.read_text(encoding="utf-8")

    try:
        schema = parse_ddl(ddl_text, dialect=args.dialect)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        row_counts = _parse_rows_arg(args.rows)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    missing = set(schema.tables) - set(row_counts)
    if missing:
        print(
            f"error: missing --rows entry for table(s): {', '.join(sorted(missing))}. "
            f"Every table in the DDL needs a row count.",
            file=sys.stderr,
        )
        return 1

    config = EngineConfig(
        seed=args.seed,
        null_rate=args.null_rate,
        fk_null_rate=args.fk_null_rate,
        deferred_fk_null_rate=args.deferred_fk_null_rate,
        fanout=args.fanout,
        as_of=args.as_of,
        coherence=not args.no_coherence,
        root_fraction=args.root_fraction,
        pool_dirs=args.pools,
    )

    # Build the generation plan once and reuse it for both the engine run and
    # the output writers (previously it was computed twice).
    plan = build_generation_plan(schema)

    try:
        generated = run_engine(schema, row_counts, config, plan=plan)
    except Exception as e:
        print(f"error during generation: {e}", file=sys.stderr)
        return 1

    report = validate(schema, generated, seed=args.seed)
    print(report.summary())
    if report.errors:
        print(
            "\nGeneration completed but the validator found unresolved constraint "
            "violations (see errors above). Output was still written -- inspect "
            "before using it.",
            file=sys.stderr,
        )

    if args.format == "csv":
        written = write_csv(schema, generated, plan.order, args.out)
        print(f"\nWrote {len(written)} CSV file(s) to {args.out}")
    elif args.format == "sql":
        out_file = args.out if args.out.endswith(".sql") else str(Path(args.out) / "insert.sql")
        path = write_sql(schema, generated, plan.order, out_file)
        print(f"\nWrote SQL inserts to {path}")
    elif args.format == "db":
        if not args.db_url:
            print("error: --db-url is required when --format db", file=sys.stderr)
            return 1
        counts = load_to_db(schema, generated, plan.order, args.db_url)
        print(f"\nLoaded rows into database: {counts}")

    return 0 if not report.errors else 2


if __name__ == "__main__":
    sys.exit(main())
