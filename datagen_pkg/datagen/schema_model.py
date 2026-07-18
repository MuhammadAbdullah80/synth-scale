"""Core in-memory schema representation.

This is the contract between the DDL parser (ddl_parser.py) and everything
downstream (dependency_graph.py, engine.py, generators/*). Nothing downstream
should ever touch sqlglot's AST directly -- it only ever sees these dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DataType(str, Enum):
    INTEGER = "integer"
    BIGINT = "bigint"
    SMALLINT = "smallint"
    NUMERIC = "numeric"
    FLOAT = "float"
    VARCHAR = "varchar"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    UUID = "uuid"
    UNKNOWN = "unknown"


@dataclass
class CheckConstraint:
    raw_sql: str
    columns_involved: list[str]
    # Structured/parsed form, populated by ddl_parser when it recognizes a
    # common shape. See generators/check_eval.py for the shapes handled.
    parsed: dict | None = None


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    nullable: bool = False


@dataclass
class Column:
    name: str
    dtype: DataType
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    nullable: bool = True
    default: str | None = None
    is_unique: bool = False
    enum_values: list[str] | None = None
    semantic_hint: str | None = None


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_constraints: list[list[str]] = field(default_factory=list)
    check_constraints: list[CheckConstraint] = field(default_factory=list)
    row_count: int = 0

    def get_column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"column {name!r} not found in table {self.name!r}")

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class SchemaModel:
    tables: dict[str, Table] = field(default_factory=dict)

    def add_table(self, table: Table) -> None:
        self.tables[table.name] = table

    def all_fks(self) -> list[tuple[str, ForeignKey]]:
        """Return (child_table_name, ForeignKey) for every FK in the schema."""
        out = []
        for tname, t in self.tables.items():
            for fk in t.foreign_keys:
                out.append((tname, fk))
        return out
