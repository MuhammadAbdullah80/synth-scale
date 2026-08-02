"""Live-database schema introspection: Postgres -> SchemaModel.

Produces the SAME SchemaModel that ddl_parser.parse_ddl produces from DDL
text, but by querying a running Postgres database (information_schema +
pg_catalog) instead of parsing SQL files. This lets datagen point at an
existing database and generate data for it without needing the original DDL.

Postgres-only by design. Connect via SQLAlchemy (any DBAPI it supports for
Postgres works, e.g. psycopg2):

    from datagen.introspect import introspect_db
    schema = introspect_db("postgresql+psycopg2://user:pass@localhost:5432/db")

Notes on fidelity vs the DDL parser:
  * Only ordinary tables in schema 'public' are read; views, materialized
    views and foreign tables are skipped.
  * SERIAL/BIGSERIAL/SMALLSERIAL columns come back from Postgres as plain
    int types with a nextval(...) default; the nextval default is dropped so
    the model matches what parse_ddl produces for SERIAL (an integer column
    with no default).
  * Postgres normalizes CHECK expressions (adds casts, rewrites BETWEEN as
    two comparisons, rewrites IN as `= ANY (ARRAY[...])`). We undo those
    rewrites before handing the expression to ddl_parser's structural check
    parser, so `CHECK (col IN (...))` round-trips to enum_values and
    `CHECK (col BETWEEN a AND b)` round-trips to a range parse, exactly as
    they would from the original DDL.
"""
from __future__ import annotations

import math

import sqlglot
from sqlglot import exp
from sqlalchemy import create_engine, text

from .ddl_parser import (
    _extract_check_columns,
    _infer_semantic_hint,
    _structural_parse_check,
)
from .dburl import normalize_db_url
from .schema_model import CheckConstraint, Column, DataType, ForeignKey, SchemaModel, Table

# information_schema.columns.data_type -> DataType
_PG_TYPE_MAP = {
    "integer": DataType.INTEGER,
    "bigint": DataType.BIGINT,
    "smallint": DataType.SMALLINT,
    "numeric": DataType.NUMERIC,
    "real": DataType.FLOAT,
    "double precision": DataType.FLOAT,
    "character varying": DataType.VARCHAR,
    "character": DataType.VARCHAR,
    "text": DataType.TEXT,
    "boolean": DataType.BOOLEAN,
    "date": DataType.DATE,
    "timestamp without time zone": DataType.TIMESTAMP,
    "timestamp with time zone": DataType.TIMESTAMP,
    "uuid": DataType.UUID,
}

_TABLES_SQL = """
SELECT c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT table_name, column_name, data_type, character_maximum_length,
       numeric_precision, numeric_scale, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""

# One row per constraint (PK / UNIQUE / FK / CHECK) on a public-schema table,
# with its member columns in declaration order and, for FKs, the referred
# table + columns. pg_get_constraintdef gives the raw text for CHECKs.
_CONSTRAINTS_SQL = """
SELECT rel.relname AS table_name,
       con.conname AS name,
       con.contype AS contype,
       pg_get_constraintdef(con.oid) AS definition,
       (SELECT array_agg(att.attname ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = u.attnum) AS cols,
       frel.relname AS ref_table,
       (SELECT array_agg(att.attname ORDER BY u.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.confrelid AND att.attnum = u.attnum) AS ref_cols
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = rel.relnamespace
LEFT JOIN pg_catalog.pg_class frel ON frel.oid = con.confrelid
WHERE n.nspname = 'public' AND con.contype IN ('p', 'u', 'f', 'c')
ORDER BY rel.relname, con.conname
"""


def _strip_casts_and_parens(node: exp.Expression) -> exp.Expression:
    """Recursively remove the ::type casts and grouping parens Postgres
    injects into stored CHECK expressions, e.g.
    `((price > (0)::numeric))` -> `price > 0`. Explicit recursion (rather
    than Expression.transform) so replacements inside list args -- like the
    items of `ARRAY['a'::character varying, ...]` -- are guaranteed to apply.
    """
    while isinstance(node, (exp.Cast, exp.TryCast, exp.Paren)):
        node = node.this
    for key, value in list(node.args.items()):
        if isinstance(value, exp.Expression):
            node.set(key, _strip_casts_and_parens(value))
        elif isinstance(value, list):
            node.set(
                key,
                [
                    _strip_casts_and_parens(v) if isinstance(v, exp.Expression) else v
                    for v in value
                ],
            )
    return node


def _normalize_check_expr(expr: exp.Expression) -> exp.Expression:
    """Undo Postgres's storage rewrites so the expression looks like the DDL
    the user originally wrote (the shape ddl_parser's structural parser and
    the generate-and-filter fallback both expect)."""
    expr = _strip_casts_and_parens(expr)

    # `col = ANY (ARRAY['a', 'b'])`  ->  `col IN ('a', 'b')`
    if isinstance(expr, exp.EQ):
        left, right = expr.this, expr.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Any):
            arr = right.this
            if isinstance(arr, exp.Array):
                return exp.In(this=left, expressions=list(arr.expressions))

    # `col >= a AND col <= b`  ->  `col BETWEEN a AND b`
    # (Postgres stores BETWEEN as the two comparisons.)
    if isinstance(expr, exp.And):
        left, right = expr.this, expr.expression
        if isinstance(left, exp.GTE) and isinstance(right, exp.LTE):
            lcol, rcol = left.this, right.this
            if (
                isinstance(lcol, exp.Column)
                and isinstance(rcol, exp.Column)
                and lcol.name == rcol.name
                and not isinstance(left.expression, exp.Column)
                and not isinstance(right.expression, exp.Column)
            ):
                return exp.Between(this=lcol, low=left.expression, high=right.expression)

    return expr


def _parse_check_definition(definition: str) -> exp.Expression | None:
    """`pg_get_constraintdef` output looks like `CHECK ((expr))`. Pull out and
    normalize the inner expression; None if sqlglot can't parse it."""
    body = definition.strip()
    if body.upper().startswith("CHECK"):
        body = body[5:].strip()
    try:
        parsed = sqlglot.parse_one(body, read="postgres")
    except Exception:
        return None
    return _normalize_check_expr(parsed)


def introspect_db(db_url: str, connect_timeout: float | None = None) -> SchemaModel:
    """Introspect a live Postgres database into a SchemaModel equivalent to
    what parse_ddl() would produce from the original DDL.

    connect_timeout (seconds) is passed through to the DBAPI as libpq's
    `connect_timeout` -- unset by default (trusted local/CLI use just waits
    on the OS default), but callers that dial arbitrary caller-supplied
    hosts (the web playground's Connect-to-Supabase flow) should always pass
    one so a slow/unreachable host can't tie up a worker indefinitely.
    """
    db_url = normalize_db_url(db_url)
    # libpq's connect_timeout is documented as an integer number of seconds;
    # psycopg2 rejects a float verbatim ("invalid integer value ... for
    # connection option 'connect_timeout'"). Round up so a sub-second budget
    # still applies rather than silently truncating to 0 (which libpq
    # treats as "no timeout").
    connect_args = {}
    if connect_timeout:
        connect_args["connect_timeout"] = max(1, math.ceil(connect_timeout))
    engine = create_engine(db_url, connect_args=connect_args)
    try:
        with engine.connect() as conn:
            table_names = [r[0] for r in conn.execute(text(_TABLES_SQL))]
            column_rows = conn.execute(text(_COLUMNS_SQL)).mappings().all()
            constraint_rows = conn.execute(text(_CONSTRAINTS_SQL)).mappings().all()
    finally:
        engine.dispose()

    schema = SchemaModel()
    tables: dict[str, Table] = {}
    for name in table_names:
        tables[name] = Table(name=name)

    for row in column_rows:
        table = tables.get(row["table_name"])
        if table is None:  # column of a view or non-public relation
            continue
        dtype = _PG_TYPE_MAP.get(row["data_type"], DataType.UNKNOWN)
        default = row["column_default"]
        if default is not None and "nextval(" in default:
            # SERIAL family: parse_ddl models these as plain int columns with
            # no default (the engine assigns sequential PKs itself).
            default = None
        precision = scale = max_length = None
        if dtype == DataType.NUMERIC:
            precision = row["numeric_precision"]
            scale = row["numeric_scale"]
        elif dtype == DataType.VARCHAR:
            max_length = row["character_maximum_length"]
        table.columns.append(
            Column(
                name=row["column_name"],
                dtype=dtype,
                max_length=max_length,
                precision=precision,
                scale=scale,
                nullable=(row["is_nullable"] == "YES"),
                default=default,
                semantic_hint=_infer_semantic_hint(row["column_name"], table.name),
            )
        )

    for row in constraint_rows:
        table = tables.get(row["table_name"])
        if table is None:
            continue
        contype = row["contype"]
        cols = list(row["cols"] or [])

        if contype == "p":
            table.primary_key = cols

        elif contype == "u":
            if len(cols) == 1:
                table.get_column(cols[0]).is_unique = True
            elif cols:
                table.unique_constraints.append(cols)

        elif contype == "f":
            nullable = any(
                table.get_column(c).nullable for c in cols if c in table.column_names()
            )
            table.foreign_keys.append(
                ForeignKey(
                    columns=cols,
                    ref_table=row["ref_table"],
                    ref_columns=list(row["ref_cols"] or []),
                    nullable=nullable,
                )
            )

        elif contype == "c":
            check_expr = _parse_check_definition(row["definition"])
            if check_expr is None:
                # Unparseable by sqlglot: keep the raw text so the validator
                # can at least surface it; no structural parse possible.
                table.check_constraints.append(
                    CheckConstraint(raw_sql=row["definition"], columns_involved=cols, parsed=None)
                )
                continue
            # Single-column IN check -> enum_values, mirroring how parse_ddl
            # treats an inline `CHECK (col IN (...))` on that column.
            if (
                isinstance(check_expr, exp.In)
                and isinstance(check_expr.this, exp.Column)
                and check_expr.this.name in table.column_names()
            ):
                table.get_column(check_expr.this.name).enum_values = [
                    lit.name for lit in check_expr.expressions if isinstance(lit, exp.Literal)
                ]
                continue
            table.check_constraints.append(
                CheckConstraint(
                    raw_sql=check_expr.sql(dialect="postgres"),
                    columns_involved=_extract_check_columns(check_expr),
                    parsed=_structural_parse_check(check_expr),
                )
            )

    for table in tables.values():
        # PK membership implies NOT NULL (Postgres enforces this; keep the
        # model consistent with parse_ddl's post-processing).
        for pk_col in table.primary_key:
            if pk_col in table.column_names():
                table.get_column(pk_col).nullable = False
        for fk in table.foreign_keys:
            fk.nullable = any(
                table.get_column(c).nullable for c in fk.columns if c in table.column_names()
            )
        schema.add_table(table)

    if not schema.tables:
        raise ValueError("No tables found in schema 'public' of the target database.")

    return schema
