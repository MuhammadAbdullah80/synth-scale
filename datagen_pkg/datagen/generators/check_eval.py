"""Fallback evaluator for CHECK constraints the structural parser didn't recognize.

Used only when a CHECK clause doesn't match one of the common shapes handled in
ddl_parser._structural_parse_check (range / IN-list / bound / two-column
compare). In that case we generate a candidate row with generic values and test
it against the raw SQL condition using `simpleeval` (never Python's `eval`).
"""
from __future__ import annotations

from datetime import date, datetime

from simpleeval import EvalWithCompoundTypes


def evaluate_check(raw_sql_condition: str, row_values: dict) -> bool:
    """Evaluate a SQL-ish boolean condition against a candidate row's values.

    This does a light SQL->Python translation (=, <>, AND/OR/NOT, IS NULL) and
    then hands off to simpleeval for safe expression evaluation. If the
    condition uses SQL features we don't translate, this raises and the caller
    should treat the CHECK as unverifiable (log a warning, accept the value).
    """
    py_expr = _sql_to_python_expr(raw_sql_condition)
    names = {k: (v if not isinstance(v, (date, datetime)) else v.isoformat()) for k, v in row_values.items()}
    evaluator = EvalWithCompoundTypes(names=names)
    return bool(evaluator.eval(py_expr))


def _sql_to_python_expr(sql: str) -> str:
    expr = sql
    replacements = [
        (" AND ", " and "), (" and " , " and "),
        (" OR ", " or "),
        (" NOT ", " not "),
        ("<>", "!="),
        (" = ", " == "),
        (" IS NOT NULL", " != None"),
        (" IS NULL", " == None"),
    ]
    for old, new in replacements:
        expr = expr.replace(old, new)
    return expr
