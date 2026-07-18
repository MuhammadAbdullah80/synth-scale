"""Stage 1: parse DDL text into a SchemaModel.

Uses sqlglot to get a structured, constraint-aware AST instead of hand-rolling
SQL parsing. Walks CREATE TABLE statements and pulls out:
  - columns (name, type, length/precision, nullability, default)
  - primary keys (inline `PRIMARY KEY` on a column, or table-level `PRIMARY KEY (...)`)
  - foreign keys (inline `REFERENCES`, or table-level `CONSTRAINT ... FOREIGN KEY`)
  - unique constraints (inline `UNIQUE`, or table-level `UNIQUE (...)`)
  - check constraints (inline or table-level), with best-effort structural
    parsing of common shapes (range, IN-list, two-column comparison) so the
    generator can construct-to-satisfy instead of retry-and-hope.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .generators.base import coerce_literal
from .schema_model import CheckConstraint, Column, DataType, ForeignKey, SchemaModel, Table

# Column-name substrings -> semantic hint used by generators/text.py to pick a
# Faker provider. Order matters: more specific patterns should be checked first
# by the caller (see _infer_semantic_hint).
_SEMANTIC_PATTERNS: list[tuple[str, str]] = [
    (r"email", "email"),
    (r"phone|mobile|tel_?no", "phone"),
    (r"first_?name|given_?name|fname", "first_name"),
    (r"last_?name|surname|lname", "last_name"),
    (r"full_?name|^name$|display_?name", "full_name"),
    (r"company|employer|organization|org_name", "company"),
    (r"job_?title|position|role_title", "job_title"),
    (r"street|address_?line|^address$", "street_address"),
    (r"city", "city"),
    (r"state|province", "state"),
    (r"country", "country"),
    (r"zip|postal", "zipcode"),
    (r"url|website|homepage", "url"),
    (r"ip_?address|^ip$", "ipv4"),
    (r"price|amount|cost|total|balance|salary", "money"),
    (r"created_?at|inserted_?at", "created_at"),
    (r"updated_?at|modified_?at", "updated_at"),
    (r"description|bio|summary|notes?$", "paragraph"),
]

_DTYPE_MAP = {
    exp.DataType.Type.INT: DataType.INTEGER,
    exp.DataType.Type.SMALLINT: DataType.SMALLINT,
    exp.DataType.Type.BIGINT: DataType.BIGINT,
    exp.DataType.Type.SERIAL: DataType.INTEGER,
    exp.DataType.Type.BIGSERIAL: DataType.BIGINT,
    exp.DataType.Type.SMALLSERIAL: DataType.SMALLINT,
    exp.DataType.Type.DECIMAL: DataType.NUMERIC,
    exp.DataType.Type.FLOAT: DataType.FLOAT,
    exp.DataType.Type.DOUBLE: DataType.FLOAT,
    exp.DataType.Type.VARCHAR: DataType.VARCHAR,
    exp.DataType.Type.CHAR: DataType.VARCHAR,
    exp.DataType.Type.NVARCHAR: DataType.VARCHAR,
    exp.DataType.Type.TEXT: DataType.TEXT,
    exp.DataType.Type.BOOLEAN: DataType.BOOLEAN,
    exp.DataType.Type.DATE: DataType.DATE,
    exp.DataType.Type.TIMESTAMP: DataType.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPTZ: DataType.TIMESTAMP,
    exp.DataType.Type.UUID: DataType.UUID,
}


_PERSON_LIKE_TABLE = re.compile(r"user|employee|customer|person|contact|staff|member|author|owner|patient|student")


def _infer_semantic_hint(column_name: str, table_name: str = "") -> str | None:
    lname = column_name.lower()
    for pattern, hint in _SEMANTIC_PATTERNS:
        if re.search(pattern, lname):
            if hint == "full_name" and pattern == r"full_?name|^name$|display_?name":
                # A bare "name" column only means "person's name" if the table
                # itself looks like it holds people (users, employees, ...).
                # Otherwise (categories.name, products.name, ...) treat it as
                # generic and let the text generator fall back to something
                # more appropriate than a person's name.
                if lname == "name" and not _PERSON_LIKE_TABLE.search(table_name.lower()):
                    return None
            return hint
    return None


def _map_dtype(col_type: exp.DataType | None) -> DataType:
    if col_type is None:
        return DataType.UNKNOWN
    return _DTYPE_MAP.get(col_type.this, DataType.UNKNOWN)


def _extract_check_columns(check_expr: exp.Expression) -> list[str]:
    return sorted({c.name for c in check_expr.find_all(exp.Column)})


def _structural_parse_check(check_expr: exp.Expression) -> dict | None:
    """Recognize a handful of common CHECK shapes so the generator can
    construct-to-satisfy instead of falling back to generate-and-filter.

    Recognized shapes:
      col > N / col >= N / col < N / col <= N          -> {"kind": "bound", ...}
      col BETWEEN a AND b                                -> {"kind": "range", ...}
      col IN (a, b, c)                                   -> {"kind": "in_list", ...}
      col_a > col_b / col_a < col_b (etc.)                -> {"kind": "col_compare", ...}
    """
    e = check_expr

    if isinstance(e, exp.Between):
        col = e.this
        if isinstance(col, exp.Column):
            return {
                "kind": "range",
                "column": col.name,
                "low": e.args["low"].name if hasattr(e.args["low"], "name") else str(e.args["low"]),
                "high": e.args["high"].name if hasattr(e.args["high"], "name") else str(e.args["high"]),
            }

    if isinstance(e, exp.In):
        col = e.this
        if isinstance(col, exp.Column):
            values = [lit.name if isinstance(lit, exp.Literal) else str(lit) for lit in e.expressions]
            return {"kind": "in_list", "column": col.name, "values": values}

    comparison_ops = {
        exp.GT: ">",
        exp.GTE: ">=",
        exp.LT: "<",
        exp.LTE: "<=",
    }
    for cls, op in comparison_ops.items():
        if isinstance(e, cls):
            left, right = e.this, e.expression
            left_is_col = isinstance(left, exp.Column)
            right_is_col = isinstance(right, exp.Column)
            if left_is_col and not right_is_col:
                try:
                    literal = right.name
                except AttributeError:
                    return None
                # Type-aware: numeric literals become floats, date/timestamp
                # literals ('2020-01-01', '2020-01-01 12:00:00') become
                # date/datetime objects. Anything else stays unparsed and
                # falls through to the generate-and-filter fallback.
                bound = coerce_literal(literal)
                if bound is None:
                    return None
                return {"kind": "bound", "column": left.name, "op": op, "value": bound}
            if left_is_col and right_is_col:
                return {"kind": "col_compare", "left": left.name, "op": op, "right": right.name}
    return None


def _parse_column_def(
    col_def: exp.ColumnDef, dialect: str = "postgres", table_name: str = ""
) -> tuple[Column, bool, ForeignKey | None, list[CheckConstraint]]:
    """Returns (Column, is_primary_key, inline_foreign_key_or_None, inline_check_constraints)."""
    name = col_def.this.name
    dtype_node = col_def.args.get("kind")
    dtype = _map_dtype(dtype_node)

    max_length = None
    precision = None
    scale = None
    if dtype_node is not None and dtype_node.expressions:
        params = [p.this.this if isinstance(p, exp.DataTypeParam) else p for p in dtype_node.expressions]
        nums = []
        for p in params:
            try:
                nums.append(int(str(p)))
            except (ValueError, TypeError):
                pass
        if dtype in (DataType.VARCHAR, DataType.TEXT) and nums:
            max_length = nums[0]
        elif dtype == DataType.NUMERIC and nums:
            precision = nums[0]
            scale = nums[1] if len(nums) > 1 else 0

    nullable = True
    is_pk = False
    is_unique = False
    default = None
    inline_fk = None
    enum_values = None
    inline_checks: list[CheckConstraint] = []

    for constraint in col_def.args.get("constraints", []) or []:
        kind = constraint.args.get("kind") if hasattr(constraint, "args") else None
        if isinstance(kind, exp.NotNullColumnConstraint):
            nullable = False
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            is_pk = True
            nullable = False
        elif isinstance(kind, exp.UniqueColumnConstraint):
            is_unique = True
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = kind.this.sql()
        elif isinstance(kind, exp.Reference):
            schema_node = kind.this
            ref_table = schema_node.this.name
            ref_cols = [i.name for i in schema_node.expressions] or ["id"]
            inline_fk = ForeignKey(columns=[name], ref_table=ref_table, ref_columns=ref_cols, nullable=nullable)
        elif isinstance(kind, exp.CheckColumnConstraint):
            check_expr = kind.this
            if isinstance(check_expr, exp.In) and isinstance(check_expr.this, exp.Column) and check_expr.this.name == name:
                enum_values = [lit.name for lit in check_expr.expressions if isinstance(lit, exp.Literal)]
            else:
                inline_checks.append(
                    CheckConstraint(
                        raw_sql=check_expr.sql(dialect=dialect),
                        columns_involved=_extract_check_columns(check_expr),
                        parsed=_structural_parse_check(check_expr),
                    )
                )

    column = Column(
        name=name,
        dtype=dtype,
        max_length=max_length,
        precision=precision,
        scale=scale,
        nullable=nullable,
        default=default,
        is_unique=is_unique,
        enum_values=enum_values,
        semantic_hint=_infer_semantic_hint(name, table_name),
    )
    return column, is_pk, inline_fk, inline_checks


def parse_ddl(ddl_text: str, dialect: str = "postgres") -> SchemaModel:
    """Parse a block of CREATE TABLE statements into a SchemaModel.

    Raises ValueError with a clear message if a statement can't be parsed at
    all. Constructs that parse but aren't understood structurally (e.g. an
    exotic CHECK expression) are kept as raw SQL for the generate-and-filter
    fallback rather than dropped silently.
    """
    schema = SchemaModel()
    try:
        statements = sqlglot.parse(ddl_text, read=dialect)
    except Exception as e:  # sqlglot raises its own ParseError subclasses
        raise ValueError(f"Failed to parse DDL: {e}") from e

    for stmt in statements:
        if stmt is None or not isinstance(stmt, exp.Create):
            continue
        if stmt.kind != "TABLE":
            continue

        schema_node = stmt.this
        if isinstance(schema_node, exp.Schema):
            table_name = schema_node.this.name
            expressions = schema_node.expressions
        elif isinstance(schema_node, exp.Table):
            table_name = schema_node.name
            expressions = []
        else:
            continue

        table = Table(name=table_name)
        pk_from_columns: list[str] = []

        for e in expressions:
            if isinstance(e, exp.ColumnDef):
                column, is_pk, inline_fk, inline_checks = _parse_column_def(e, dialect=dialect, table_name=table_name)
                table.columns.append(column)
                if is_pk:
                    pk_from_columns.append(column.name)
                if inline_fk:
                    table.foreign_keys.append(inline_fk)
                table.check_constraints.extend(inline_checks)

            elif isinstance(e, exp.PrimaryKey):
                table.primary_key = [i.name for i in e.expressions]

            elif isinstance(e, exp.UniqueColumnConstraint):
                cols = [i.name for i in e.this.expressions] if isinstance(e.this, exp.Schema) else []
                if cols:
                    if len(cols) == 1:
                        table.get_column(cols[0]).is_unique = True
                    else:
                        table.unique_constraints.append(cols)

            elif isinstance(e, exp.Constraint):
                for sub in e.expressions:
                    if isinstance(sub, exp.ForeignKey):
                        fk_cols = [i.name for i in sub.expressions]
                        ref = sub.args.get("reference")
                        ref_schema = ref.this
                        ref_table = ref_schema.this.name
                        ref_cols = [i.name for i in ref_schema.expressions] or ["id"]
                        nullable = any(table.get_column(c).nullable for c in fk_cols if c in table.column_names())
                        table.foreign_keys.append(
                            ForeignKey(columns=fk_cols, ref_table=ref_table, ref_columns=ref_cols, nullable=nullable)
                        )
                    elif isinstance(sub, exp.CheckColumnConstraint):
                        check_expr = sub.this
                        table.check_constraints.append(
                            CheckConstraint(
                                raw_sql=check_expr.sql(dialect=dialect),
                                columns_involved=_extract_check_columns(check_expr),
                                parsed=_structural_parse_check(check_expr),
                            )
                        )
                    elif isinstance(sub, exp.UniqueColumnConstraint):
                        cols = [i.name for i in sub.this.expressions] if isinstance(sub.this, exp.Schema) else []
                        if len(cols) == 1:
                            table.get_column(cols[0]).is_unique = True
                        elif cols:
                            table.unique_constraints.append(cols)

            elif isinstance(e, exp.CheckColumnConstraint):
                check_expr = e.this
                table.check_constraints.append(
                    CheckConstraint(
                        raw_sql=check_expr.sql(dialect=dialect),
                        columns_involved=_extract_check_columns(check_expr),
                        parsed=_structural_parse_check(check_expr),
                    )
                )

        if pk_from_columns and not table.primary_key:
            table.primary_key = pk_from_columns

        # A column that is part of the primary key is NOT NULL by definition,
        # even if that was only established via a table-level `PRIMARY KEY (...)`
        # constraint that we processed after the column itself.
        for pk_col in table.primary_key:
            if pk_col in table.column_names():
                table.get_column(pk_col).nullable = False

        # Recompute FK nullability now that PK-derived NOT NULL has been applied.
        for fk in table.foreign_keys:
            fk.nullable = any(
                table.get_column(c).nullable for c in fk.columns if c in table.column_names()
            )

        schema.add_table(table)

    for stmt in statements:
        if stmt is None or not isinstance(stmt, exp.Alter):
            continue
        _apply_alter_statement(stmt, schema, dialect)

    if not schema.tables:
        raise ValueError("No CREATE TABLE statements found in the supplied DDL.")

    return schema


def _apply_alter_statement(stmt: "exp.Alter", schema: SchemaModel, dialect: str) -> None:
    """Handle `ALTER TABLE t ADD CONSTRAINT ... FOREIGN KEY/CHECK/UNIQUE (...)`.
    This is the standard way real-world DDL expresses a mutual-FK cycle (you
    can't inline both directions in CREATE TABLE), so MVP needs to support it
    even though it isn't a CREATE TABLE statement.
    """
    table_name = stmt.this.name
    table = schema.tables.get(table_name)
    if table is None:
        return  # ALTER on a table we don't know about -- ignore rather than crash

    for action in stmt.args.get("actions", []) or []:
        if not isinstance(action, exp.AddConstraint):
            continue
        for constraint in action.expressions:
            if not isinstance(constraint, exp.Constraint):
                continue
            for sub in constraint.expressions:
                if isinstance(sub, exp.ForeignKey):
                    fk_cols = [i.name for i in sub.expressions]
                    ref = sub.args.get("reference")
                    ref_schema = ref.this
                    ref_table = ref_schema.this.name
                    ref_cols = [i.name for i in ref_schema.expressions] or ["id"]
                    nullable = any(
                        table.get_column(c).nullable for c in fk_cols if c in table.column_names()
                    )
                    table.foreign_keys.append(
                        ForeignKey(columns=fk_cols, ref_table=ref_table, ref_columns=ref_cols, nullable=nullable)
                    )
                elif isinstance(sub, exp.CheckColumnConstraint):
                    check_expr = sub.this
                    table.check_constraints.append(
                        CheckConstraint(
                            raw_sql=check_expr.sql(dialect=dialect),
                            columns_involved=_extract_check_columns(check_expr),
                            parsed=_structural_parse_check(check_expr),
                        )
                    )
                elif isinstance(sub, exp.UniqueColumnConstraint):
                    cols = [i.name for i in sub.this.expressions] if isinstance(sub.this, exp.Schema) else []
                    if len(cols) == 1:
                        table.get_column(cols[0]).is_unique = True
                    elif cols:
                        table.unique_constraints.append(cols)
