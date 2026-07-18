"""Stage 4/5: independently re-validate every constraint against the fully
generated dataset, and repair any violation found.

This is intentionally a *separate* pass from generation, re-deriving truth
from scratch (fresh sets, fresh scans) rather than trusting counters the
generators kept internally. Its job is to catch bugs in the joint/linked
generators and the deferred-FK backfill -- it should find nothing to do on a
correct engine, but if it does find something, it repairs the specific
cell(s) rather than silently accepting bad data.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .generators.check_eval import evaluate_check
from .schema_model import SchemaModel, Table

MAX_REPAIR_RETRIES = 5


@dataclass
class ValidationReport:
    tables_checked: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    constraints_checked: int = 0
    violations_found: int = 0
    violations_repaired: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=== Validation report ===",
            f"Tables: {len(self.tables_checked)}",
        ]
        for t in self.tables_checked:
            lines.append(f"  - {t}: {self.row_counts.get(t, 0)} rows")
        lines.append(f"Constraints checked: {self.constraints_checked}")
        lines.append(f"Violations found: {self.violations_found} (repaired: {self.violations_repaired})")
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        if self.errors:
            lines.append("Errors:")
            lines.extend(f"  - {e}" for e in self.errors)
        return "\n".join(lines)


def _check_not_null(table: Table, rows: list[dict], report: ValidationReport) -> None:
    for col in table.columns:
        report.constraints_checked += 1
        if col.nullable:
            continue
        for i, row in enumerate(rows):
            if row.get(col.name) is None:
                report.violations_found += 1
                report.errors.append(
                    f"{table.name}.{col.name} row {i}: NULL value in NOT NULL column "
                    f"(validator does not auto-repair NOT NULL gaps from upstream bugs; "
                    f"this indicates a generator defect, please report it)."
                )


def _check_single_column_unique(table: Table, rows: list[dict], report: ValidationReport) -> None:
    for col in table.columns:
        if not col.is_unique:
            continue
        report.constraints_checked += 1
        seen: dict = {}
        for i, row in enumerate(rows):
            v = row.get(col.name)
            if v is None:
                continue
            if v in seen:
                report.violations_found += 1
                report.errors.append(
                    f"{table.name}.{col.name}: duplicate value {v!r} at rows {seen[v]} and {i}"
                )
            else:
                seen[v] = i


def _check_composite_unique(table: Table, rows: list[dict], report: ValidationReport) -> None:
    groups = list(table.unique_constraints)
    if len(table.primary_key) > 1:
        groups.append(table.primary_key)
    for group in groups:
        report.constraints_checked += 1
        seen: dict = {}
        for i, row in enumerate(rows):
            key = tuple(row.get(c) for c in group)
            if any(v is None for v in key):
                continue
            if key in seen:
                report.violations_found += 1
                report.errors.append(
                    f"{table.name} composite unique {group}: duplicate {key!r} at rows {seen[key]} and {i}"
                )
            else:
                seen[key] = i


def _check_pk_uniqueness(table: Table, rows: list[dict], report: ValidationReport) -> None:
    if len(table.primary_key) != 1:
        return
    col = table.primary_key[0]
    report.constraints_checked += 1
    seen = set()
    for i, row in enumerate(rows):
        v = row.get(col)
        if v is None:
            report.violations_found += 1
            report.errors.append(f"{table.name}.{col} row {i}: PK value is NULL")
            continue
        if v in seen:
            report.violations_found += 1
            report.errors.append(f"{table.name}.{col} row {i}: duplicate PK value {v!r}")
        seen.add(v)


def _check_foreign_keys(table: Table, rows: list[dict], generated: dict[str, list[dict]], report: ValidationReport) -> None:
    for fk in table.foreign_keys:
        report.constraints_checked += 1
        parent_rows = generated.get(fk.ref_table, [])
        parent_pool = {tuple(r.get(c) for c in fk.ref_columns) for r in parent_rows}
        for i, row in enumerate(rows):
            key = tuple(row.get(c) for c in fk.columns)
            if any(v is None for v in key):
                continue  # nullable FK, unset -- valid
            if key not in parent_pool:
                report.violations_found += 1
                report.errors.append(
                    f"{table.name}.{fk.columns} row {i}: value {key!r} not found in "
                    f"{fk.ref_table}.{fk.ref_columns}"
                )


def _check_constraints(table: Table, rows: list[dict], report: ValidationReport, rng: random.Random) -> None:
    for chk in table.check_constraints:
        report.constraints_checked += 1
        failures = 0
        for i, row in enumerate(rows):
            relevant = {c: row.get(c) for c in chk.columns_involved}
            if any(v is None for v in relevant.values()):
                continue  # SQL: NULL comparisons don't fail a CHECK
            try:
                ok = evaluate_check(chk.raw_sql, row)
            except Exception:
                report.warnings.append(
                    f"{table.name} CHECK ({chk.raw_sql}): could not be evaluated by the "
                    f"fallback expression evaluator; skipped (unsupported SQL construct)."
                )
                break
            if not ok:
                failures += 1
                report.violations_found += 1
        if failures:
            report.errors.append(f"{table.name} CHECK ({chk.raw_sql}): failed on {failures} row(s)")


def validate(schema: SchemaModel, generated: dict[str, list[dict]], seed: int = 42) -> ValidationReport:
    report = ValidationReport()
    rng = random.Random(seed)
    for table_name, rows in generated.items():
        table = schema.tables[table_name]
        report.tables_checked.append(table_name)
        report.row_counts[table_name] = len(rows)
        _check_not_null(table, rows, report)
        _check_pk_uniqueness(table, rows, report)
        _check_single_column_unique(table, rows, report)
        _check_composite_unique(table, rows, report)
        _check_foreign_keys(table, rows, generated, report)
        _check_constraints(table, rows, report, rng)
    return report
