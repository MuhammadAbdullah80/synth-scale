"""Synth-Scale command-line interface (Typer + Rich).

Invoked as ``synth-scale`` (console script installed by the ``synth-scale``
package) or ``python -m datagen.cli``.

Exit codes:
  0  success (generated, validated, written)
  1  usage / input error (missing files, bad --rows, parse failure, ...)
  2  generation completed but the validator found constraint violations

Errors go to stderr; the report / summary goes to stdout.
"""
from __future__ import annotations

import warnings
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table as RichTable

from .config import (
    CONFIG_FILENAME,
    ConfigError,
    FanoutSpec,
    SynthConfig,
    apply_column_overrides,
    load_config,
    parse_fanout_value,
    resolve_row_counts,
)
from .ddl_parser import parse_ddl
from .dependency_graph import build_generation_plan
from .engine import EngineConfig, run_engine
from .output import load_to_db, write_csv, write_sql
from .validator import validate

# --- integer --rows heuristic (documented in --help and the README) --------
CHILD_MULTIPLIER = 3   # a child table gets 3x the largest of its parents
CHILD_CAP_FACTOR = 10  # ...capped at 10x the base count
PREVIEW_ROWS = 10
PREVIEW_CELL_WIDTH = 24
# Spinner only kicks in for runs big enough that the wait is noticeable.
SPINNER_ROW_THRESHOLD = 2000

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# soft_wrap: never hard-wrap plain messages/paths mid-line (tables still
# render at their own measured width).
console = Console(soft_wrap=True)
err_console = Console(stderr=True, soft_wrap=True)


class OutputFormat(str, Enum):
    csv = "csv"
    sql = "sql"
    db = "db"


class Fanout(str, Enum):
    uniform = "uniform"
    zipfian = "zipfian"


def _fail(message: str, code: int = 1) -> typer.Exit:
    """Print an error to stderr (old-CLI convention: `error: ...`) and return
    a typer.Exit for the caller to raise."""
    err_console.print(f"[bold red]error:[/bold red] {message}", highlight=False)
    return typer.Exit(code)


def _parse_rows_arg(rows_arg: str) -> dict[str, "int | FanoutSpec"]:
    """Parse the per-table --rows form. Each count is either an integer or a
    fanout range ("5..20 per users" / "5..20/users", see datagen/config.py)."""
    row_counts: dict[str, int | FanoutSpec] = {}
    for pair in rows_arg.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"Invalid --rows entry {pair!r}, expected table=count")
        table, count = pair.split("=", 1)
        try:
            row_counts[table.strip()] = parse_fanout_value(count)
        except ConfigError as e:
            raise ValueError(f"Invalid --rows entry {pair!r}: {e}") from None
    return row_counts


def _load_file_config(config_path: Optional[Path], no_config: bool) -> SynthConfig:
    """Resolve the effective synthscale.toml: an explicit --config path wins;
    otherwise ./synthscale.toml is auto-discovered; --no-config disables both.
    Loader warnings (unknown keys) are surfaced on stderr."""
    if no_config:
        if config_path is not None:
            raise _fail("--config and --no-config are mutually exclusive")
        return SynthConfig()
    path = config_path
    if path is None:
        candidate = Path(CONFIG_FILENAME)
        if not candidate.exists():
            return SynthConfig()
        path = candidate
    elif not path.exists():
        raise _fail(f"config file not found: {path}")
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            file_cfg = load_config(path)
    except ConfigError as e:
        raise _fail(str(e))
    for w in caught:
        err_console.print(f"[yellow]warning:[/yellow] {w.message}", highlight=False)
    return file_cfg


def _integer_rows_plan(schema, order: list[str], base: int) -> dict[str, int]:
    """`--rows N` convenience: every table gets `base` rows, except a table
    with FK parents gets CHILD_MULTIPLIER x the largest of its parents'
    counts, capped at CHILD_CAP_FACTOR x base. Self-referencing FKs do not
    count as parents. Walks the generation plan order, so the rule is
    deterministic."""
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
    return counts


def _truncate_cell(value) -> str:
    if value is None:
        return "[dim]NULL[/dim]"
    s = str(value)
    if len(s) > PREVIEW_CELL_WIDTH:
        s = s[: PREVIEW_CELL_WIDTH - 1] + "…"
    return s


def _print_preview(schema, generated: dict[str, list[dict]], order: list[str]) -> None:
    console.print(
        "\n[bold]--preview[/bold]: showing the first "
        f"{PREVIEW_ROWS} rows of each table; [yellow]nothing is written to "
        "disk[/yellow] (--format/--out are ignored in preview mode).",
        highlight=False,
    )
    for tname in order:
        rows = generated.get(tname, [])
        col_names = [c.name for c in schema.tables[tname].columns]
        rich_table = RichTable(
            title=f"{tname} ({len(rows)} rows, first {min(PREVIEW_ROWS, len(rows))} shown)",
            title_justify="left",
            header_style="bold cyan",
        )
        for cname in col_names:
            rich_table.add_column(cname, overflow="fold")
        for row in rows[:PREVIEW_ROWS]:
            rich_table.add_row(*(_truncate_cell(row.get(c)) for c in col_names))
        console.print(rich_table)


def _print_summary(report, order: list[str]) -> None:
    per_table_errors: dict[str, int] = {t: 0 for t in report.tables_checked}
    for e in report.errors:
        for t in per_table_errors:
            if e.startswith(f"{t}.") or e.startswith(f"{t} "):
                per_table_errors[t] += 1
                break

    summary = RichTable(title="Generation summary", title_justify="left")
    summary.add_column("Table", style="bold")
    summary.add_column("Rows", justify="right")
    summary.add_column("Status")
    ordered = [t for t in order if t in report.tables_checked] + [
        t for t in report.tables_checked if t not in order
    ]
    for t in ordered:
        n_err = per_table_errors.get(t, 0)
        status = "[green]ok[/green]" if n_err == 0 else f"[red]{n_err} error(s)[/red]"
        summary.add_row(t, str(report.row_counts.get(t, 0)), status)
    console.print(summary)
    console.print(f"Constraints checked: {report.constraints_checked}")
    style = "green" if not report.violations_found else "bold red"
    console.print(f"Violations found: [{style}]{report.violations_found}[/{style}]")
    if report.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in report.warnings:
            console.print(f"  [yellow]- {w}[/yellow]", highlight=False)
    if report.errors:
        console.print("[red]Errors:[/red]")
        for e in report.errors:
            console.print(f"  [red]- {e}[/red]", highlight=False)


@app.command()
def main(
    ctx: typer.Context,
    ddl: Optional[Path] = typer.Option(
        None, "--ddl",
        help="Path to a .sql file of CREATE TABLE statements. Required unless "
             "--from-db is used.",
    ),
    rows: Optional[str] = typer.Option(
        None, "--rows",
        help="Comma-separated table=count pairs (e.g. users=100,orders=500), "
             "where a count may also be a fanout range tying a child to a "
             "direct FK parent (orders=5..20/users or \"orders=5..20 per "
             "users\": each user gets 5-20 orders, drawn deterministically); "
             "or a single integer N: every table gets N rows, except tables "
             f"with FK parents get {CHILD_MULTIPLIER}x the largest parent "
             f"count (capped at {CHILD_CAP_FACTOR}x N). The explicit per-table "
             "form always wins. May be omitted when a synthscale.toml [rows] "
             "section supplies the counts.",
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", metavar="TOML",
        help=f"Path to a synthscale.toml config file. Without this flag, "
             f"./{CONFIG_FILENAME} is auto-discovered if present. Precedence: "
             f"CLI flags > config file > built-in defaults.",
    ),
    no_config: bool = typer.Option(
        False, "--no-config",
        help=f"Ignore any ./{CONFIG_FILENAME}; run from CLI flags and "
             "defaults only.",
    ),
    seed: int = typer.Option(42, "--seed", help="Random seed; same seed = byte-identical output."),
    format: OutputFormat = typer.Option(
        OutputFormat.csv, "--format",
        help="csv (one file per table), sql (batched INSERTs), or db (direct "
             "load via SQLAlchemy, needs --db-url).",
    ),
    out: str = typer.Option("./out", "--out", help="Output directory (csv) or file (sql). Ignored for db."),
    db_url: Optional[str] = typer.Option(
        None, "--db-url",
        help="SQLAlchemy connection URL. Required for --format db, and used as "
             "the schema source with --from-db.",
    ),
    dialect: str = typer.Option("postgres", "--dialect", help="DDL SQL dialect for sqlglot."),
    null_rate: float = typer.Option(0.05, "--null-rate", help="Default null rate for nullable columns."),
    fk_null_rate: float = typer.Option(0.1, "--fk-null-rate", help="Default null rate for nullable FK columns."),
    deferred_fk_null_rate: float = typer.Option(
        0.2, "--deferred-fk-null-rate",
        help="Null rate for self-referential/cycle-broken FKs (e.g. employees with no manager).",
    ),
    as_of: Optional[datetime] = typer.Option(
        None, "--as-of", formats=["%Y-%m-%d"], metavar="YYYY-MM-DD",
        help="Anchor date for default date/timestamp windows (default: a fixed "
             "built-in date, so the same seed gives identical output regardless "
             "of the run day).",
    ),
    fanout: Fanout = typer.Option(
        Fanout.zipfian, "--fanout",
        help="FK sampling distribution (zipfian = a few parents get most "
             "children, the realistic default; uniform = perfectly even fan-out).",
    ),
    no_coherence: bool = typer.Option(
        False, "--no-coherence",
        help="Disable the coherence layer (correlated pools, updated_at >= "
             "created_at, child rows newer than their parents, anchor-sorted "
             "hierarchies).",
    ),
    root_fraction: Optional[float] = typer.Option(
        None, "--root-fraction", metavar="FRACTION",
        help="Fraction of rows in a self-referencing hierarchy that are roots "
             "with a NULL parent (default: the --deferred-fk-null-rate value).",
    ),
    pools: Optional[list[str]] = typer.Option(
        None, "--pools", metavar="DIR",
        help="Extra directory of *.json correlated-value pools (repeatable). A "
             "pool whose name matches a packaged pool (geo, person, products) "
             "replaces it.",
    ),
    preview: bool = typer.Option(
        False, "--preview",
        help=f"Generate, print the first {PREVIEW_ROWS} rows of every table, "
             "and write nothing to disk (--format/--out are ignored).",
    ),
    from_db: bool = typer.Option(
        False, "--from-db",
        help="Introspect the live database at --db-url and use it as the "
             "schema source instead of --ddl (requires the postgres extra).",
    ),
) -> None:
    """Generate deterministic, referentially-consistent test data from a SQL
    DDL schema (or a live database with --from-db)."""
    # --- config file (synthscale.toml) --------------------------------------
    # Precedence everywhere below: CLI flags > config file > built-in defaults.
    file_cfg = _load_file_config(config_path, no_config)

    def _from_cli(param_name: str) -> bool:
        # Compare by enum member name: typer >= 0.16 vendors its own click
        # internals, so its ParameterSource is a *different* enum class than
        # click.core.ParameterSource and identity/equality checks would
        # silently always be False.
        source = ctx.get_parameter_source(param_name)
        return source is not None and source.name == "COMMANDLINE"

    gen_cfg = file_cfg.generation
    eff_seed = file_cfg.seed if file_cfg.seed is not None and not _from_cli("seed") else seed
    eff_as_of = as_of.date() if as_of is not None else file_cfg.as_of
    eff_null_rate = (
        gen_cfg["null_rate"] if "null_rate" in gen_cfg and not _from_cli("null_rate") else null_rate
    )
    eff_fk_null_rate = (
        gen_cfg["fk_null_rate"]
        if "fk_null_rate" in gen_cfg and not _from_cli("fk_null_rate")
        else fk_null_rate
    )
    eff_deferred_fk_null_rate = (
        gen_cfg["deferred_fk_null_rate"]
        if "deferred_fk_null_rate" in gen_cfg and not _from_cli("deferred_fk_null_rate")
        else deferred_fk_null_rate
    )
    eff_fanout = (
        gen_cfg["fanout"] if "fanout" in gen_cfg and not _from_cli("fanout") else fanout.value
    )
    eff_coherence = (
        gen_cfg["coherence"]
        if "coherence" in gen_cfg and not _from_cli("no_coherence")
        else not no_coherence
    )
    eff_root_fraction = (
        root_fraction if root_fraction is not None else gen_cfg.get("root_fraction")
    )
    eff_pool_dirs = list(pools) if pools else list(gen_cfg.get("pool_dirs", []))

    # --- schema source ------------------------------------------------------
    if from_db:
        if not db_url:
            raise _fail("--from-db requires --db-url")
        try:
            from .introspect import introspect_db
        except ImportError:
            raise _fail(
                "introspection module not available in this build "
                "(--from-db needs datagen.introspect; install the postgres extra)"
            )
        try:
            schema = introspect_db(db_url)
        except Exception as e:
            raise _fail(f"could not introspect database: {e}")
    else:
        if ddl is None:
            raise _fail("--ddl is required (or pass --from-db with --db-url)")
        if not ddl.exists():
            raise _fail(f"DDL file not found: {ddl}")
        ddl_text = ddl.read_text(encoding="utf-8")
        try:
            schema = parse_ddl(ddl_text, dialect=dialect)
        except ValueError as e:
            raise _fail(str(e))

    # --- per-column overrides from the config file --------------------------
    try:
        apply_column_overrides(schema, file_cfg.columns)
    except ConfigError as e:
        raise _fail(str(e))

    # Build the generation plan once and reuse it for the row plan, the engine
    # run and the output writers.
    plan = build_generation_plan(schema)

    # --- row counts ---------------------------------------------------------
    # Per-table precedence: an explicit --rows table=... entry beats the same
    # table's [rows] entry; the config file fills in the rest. The single
    # integer --rows N form is a whole-plan heuristic and replaces the [rows]
    # section entirely.
    if rows is not None:
        rows_arg = rows.strip()
    else:
        rows_arg = None
    if rows_arg is not None and rows_arg.lstrip("+-").isdigit():
        base = int(rows_arg)
        if base < 0:
            raise _fail(f"--rows {base}: row count cannot be negative")
        row_specs: dict = _integer_rows_plan(schema, plan.order, base)
    elif rows_arg is not None:
        try:
            cli_specs = _parse_rows_arg(rows_arg)
        except ValueError as e:
            raise _fail(str(e))
        row_specs = {**file_cfg.rows, **cli_specs}
    else:
        if not file_cfg.rows:
            raise _fail(
                f"--rows is required (or provide a [rows] section in {CONFIG_FILENAME})"
            )
        row_specs = dict(file_cfg.rows)
    missing = set(schema.tables) - set(row_specs)
    if missing:
        raise _fail(
            f"missing --rows entry for table(s): {', '.join(sorted(missing))}. "
            f"Every table in the DDL needs a row count -- from --rows, a "
            f"[rows] config section, or a single integer to size all tables."
        )
    try:
        row_counts, fanout_allocations = resolve_row_counts(
            schema, plan.order, row_specs, seed=eff_seed, deferred_fks=plan.deferred_fks
        )
    except ConfigError as e:
        raise _fail(str(e))

    config = EngineConfig(
        seed=eff_seed,
        null_rate=eff_null_rate,
        fk_null_rate=eff_fk_null_rate,
        deferred_fk_null_rate=eff_deferred_fk_null_rate,
        fanout=eff_fanout,
        as_of=eff_as_of,
        coherence=eff_coherence,
        root_fraction=eff_root_fraction,
        pool_dirs=eff_pool_dirs,
        fanout_allocations=fanout_allocations,
        column_overrides=file_cfg.columns,
    )

    # --- generate -----------------------------------------------------------
    total_rows = sum(row_counts.values())
    try:
        if console.is_terminal and total_rows >= SPINNER_ROW_THRESHOLD:
            with console.status(f"Generating {total_rows:,} rows..."):
                generated = run_engine(schema, row_counts, config, plan=plan)
        else:
            generated = run_engine(schema, row_counts, config, plan=plan)
    except Exception as e:
        err_console.print(f"[bold red]error during generation:[/bold red] {e}", highlight=False)
        raise typer.Exit(1)

    # --- validate + report --------------------------------------------------
    report = validate(schema, generated, seed=eff_seed)
    _print_summary(report, plan.order)
    if report.errors:
        err_console.print(
            "\n[red]Generation completed but the validator found unresolved "
            "constraint violations (see errors above). Output was still "
            "written -- inspect before using it.[/red]",
            highlight=False,
        )

    # --- output -------------------------------------------------------------
    if preview:
        _print_preview(schema, generated, plan.order)
    elif format is OutputFormat.csv:
        written = write_csv(schema, generated, plan.order, out)
        console.print(f"\nWrote {len(written)} CSV file(s) to {out}", highlight=False)
    elif format is OutputFormat.sql:
        out_file = out if out.endswith(".sql") else str(Path(out) / "insert.sql")
        path = write_sql(schema, generated, plan.order, out_file)
        console.print(f"\nWrote SQL inserts to {path}", highlight=False)
    elif format is OutputFormat.db:
        if not db_url:
            raise _fail("--db-url is required when --format db")
        counts = load_to_db(schema, generated, plan.order, db_url)
        console.print(f"\nLoaded rows into database: {counts}", highlight=False)

    if report.errors:
        raise typer.Exit(2)


def app_main() -> None:
    """Console-script entry point (`synth-scale = datagen.cli:app_main`)."""
    app()


if __name__ == "__main__":
    app_main()
