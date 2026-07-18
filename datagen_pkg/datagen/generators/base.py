"""Shared helpers for the per-type generator modules."""
from __future__ import annotations

from datetime import date, datetime, timedelta

MAX_UNIQUE_RETRIES = 50


class DomainExhaustedError(Exception):
    """Raised when a unique/constrained column's feasible domain is smaller
    than the number of rows requested -- this should fail fast rather than
    spin forever or silently emit duplicate/invalid data."""


def coerce_literal(value):
    """Type-aware coercion of a CHECK literal to a comparable Python value.

    Tries, in order: pass-through for already-typed values (numbers, dates),
    float, ISO date (``YYYY-MM-DD``), ISO datetime. Returns None when the
    literal can't be understood -- callers should then drop the bound rather
    than crash (the raw CHECK is still kept for the fallback evaluator).
    """
    if value is None or isinstance(value, (int, float, date, datetime)):
        return value
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def apply_single_column_bounds(check_parsed_list: list[dict], column_name: str) -> dict:
    """Collapse whatever single-column CHECK clauses apply to `column_name`
    into one bounds dict: {"min": x, "max": y, "in_list": [...]}. Multiple
    bound clauses (e.g. `col > 0` and `col < 1000` as two separate CHECKs)
    are intersected. Values may be numbers or date/datetime objects,
    depending on the column's literals.
    """
    bounds: dict = {}
    for parsed in check_parsed_list:
        if parsed is None:
            continue
        kind = parsed.get("kind")
        if kind == "bound" and parsed["column"] == column_name:
            op, value = parsed["op"], coerce_literal(parsed["value"])
            if value is None:
                continue
            if op in (">", ">="):
                lo = value if op == ">=" else value + _epsilon(value)
                bounds["min"] = max(bounds.get("min", lo), lo)
            elif op in ("<", "<="):
                hi = value if op == "<=" else value - _epsilon(value)
                bounds["max"] = min(bounds.get("max", hi), hi)
        elif kind == "range" and parsed["column"] == column_name:
            lo = coerce_literal(parsed["low"])
            hi = coerce_literal(parsed["high"])
            if lo is not None:
                bounds["min"] = max(bounds.get("min", lo), lo)
            if hi is not None:
                bounds["max"] = min(bounds.get("max", hi), hi)
        elif kind == "in_list" and parsed["column"] == column_name:
            bounds["in_list"] = parsed["values"]
    return bounds


def _epsilon(value):
    # Small nudge for strict inequalities, per domain: one second for
    # timestamps, one day for dates, 1 for integer-valued numbers, 0.01
    # for continuous numbers.
    if isinstance(value, datetime):
        return timedelta(seconds=1)
    if isinstance(value, date):
        return timedelta(days=1)
    return 1 if float(value).is_integer() else 0.01
