"""Shared helpers for the per-type generator modules."""
from __future__ import annotations

MAX_UNIQUE_RETRIES = 50


class DomainExhaustedError(Exception):
    """Raised when a unique/constrained column's feasible domain is smaller
    than the number of rows requested -- this should fail fast rather than
    spin forever or silently emit duplicate/invalid data."""


def apply_single_column_bounds(check_parsed_list: list[dict], column_name: str) -> dict:
    """Collapse whatever single-column CHECK clauses apply to `column_name`
    into one bounds dict: {"min": x, "max": y, "in_list": [...]}. Multiple
    bound clauses (e.g. `col > 0` and `col < 1000` as two separate CHECKs)
    are intersected.
    """
    bounds: dict = {}
    for parsed in check_parsed_list:
        if parsed is None:
            continue
        kind = parsed.get("kind")
        if kind == "bound" and parsed["column"] == column_name:
            op, value = parsed["op"], parsed["value"]
            if op in (">", ">="):
                lo = value if op == ">=" else value + _epsilon(value)
                bounds["min"] = max(bounds.get("min", lo), lo)
            elif op in ("<", "<="):
                hi = value if op == "<=" else value - _epsilon(value)
                bounds["max"] = min(bounds.get("max", hi), hi)
        elif kind == "range" and parsed["column"] == column_name:
            bounds["min"] = max(bounds.get("min", float(parsed["low"])), float(parsed["low"]))
            bounds["max"] = min(bounds.get("max", float(parsed["high"])), float(parsed["high"]))
        elif kind == "in_list" and parsed["column"] == column_name:
            bounds["in_list"] = parsed["values"]
    return bounds


def _epsilon(value: float) -> float:
    # Small nudge for strict inequalities on continuous/integer domains.
    return 1 if float(value).is_integer() else 0.01
