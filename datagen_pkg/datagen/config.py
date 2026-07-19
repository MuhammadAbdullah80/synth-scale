"""`synthscale.toml` — the authoring/config layer (Snaplet-style DX).

One file states the *shape* of the dataset instead of a pile of CLI flags:

    [project]            # optional
    seed = 42
    as_of = 2026-01-01

    [rows]               # per-table row counts, same semantics as --rows
    users = 100
    orders = "5..20 per users"       # fanout range: each user gets 5..20 orders
    order_items = "1..5 per orders"  # also accepted: "1..5/orders"

    [generation]         # optional overrides mapping to EngineConfig fields
    null_rate = 0.05
    fanout = "zipfian"

    [columns."users.status"]         # optional per-column overrides
    values = ["active", "trial", "churned"]
    weights = [0.7, 0.2, 0.1]
    [columns."products.price"]
    min = 5.0
    max = 500.0

Precedence (documented and tested): **CLI flags > config file > built-in
defaults**. Loaded with stdlib ``tomllib``; unknown keys warn, bad types error
(`ConfigError`).

Fanout ranges are the headline: ``"5..20 per users"`` does not state a total —
it states the *relationship*. The child table's row count is the sum of one
``rng.randint(5, 20)`` draw per parent row (deterministic, seeded per child
table), and the exact per-parent draw is carried into FK generation
(`EngineConfig.fanout_allocations`) so each parent receives exactly its drawn
number of children.
"""
from __future__ import annotations

import random
import re
import tomllib
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .generators.base import apply_single_column_bounds
from .schema_model import CheckConstraint, DataType, SchemaModel

CONFIG_FILENAME = "synthscale.toml"

_INT_TYPES = (DataType.INTEGER, DataType.BIGINT, DataType.SMALLINT)
_NUM_TYPES = _INT_TYPES + (DataType.NUMERIC, DataType.FLOAT)
_TEXT_TYPES = (DataType.VARCHAR, DataType.TEXT, DataType.UUID, DataType.UNKNOWN)

_FANOUT_RE = re.compile(r"^\s*(\d+)\s*\.\.\s*(\d+)\s*(?:per\s+|/\s*)(\w+)\s*$")

_GENERATION_KEYS: dict[str, type | tuple] = {
    "null_rate": (int, float),
    "fk_null_rate": (int, float),
    "deferred_fk_null_rate": (int, float),
    "fanout": str,
    "coherence": bool,
    "root_fraction": (int, float),
    "pool_dirs": list,
}
_RATE_KEYS = {"null_rate", "fk_null_rate", "deferred_fk_null_rate", "root_fraction"}


class ConfigError(ValueError):
    """A synthscale.toml (or --rows fanout spec) problem the user must fix."""


@dataclass(frozen=True)
class FanoutSpec:
    """`"5..20 per users"`: each parent row draws randint(low, high) children."""
    low: int
    high: int
    parent: str


@dataclass
class FanoutAllocation:
    """Resolved fanout draw for one child table: per_parent[i] children for
    parent row i (index-aligned with the parent table's generated rows)."""
    parent_table: str
    per_parent: list[int]


@dataclass
class ColumnOverride:
    """One `[columns."table.column"]` entry: a closed value list (optionally
    weighted), or numeric/date min/max bounds. Never both."""
    table: str
    column: str
    values: list | None = None
    weights: list[float] | None = None
    min: object | None = None
    max: object | None = None


@dataclass
class SynthConfig:
    """Parsed, validated synthscale.toml."""
    path: Path | None = None
    seed: int | None = None
    as_of: date | None = None
    rows: dict[str, int | FanoutSpec] = field(default_factory=dict)
    generation: dict = field(default_factory=dict)
    columns: dict[str, ColumnOverride] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Fanout spec parsing (shared by the [rows] section and the --rows flag)
# --------------------------------------------------------------------------

def parse_fanout_value(text: str) -> int | FanoutSpec:
    """Parse a row-count value: a plain int, `"LO..HI per PARENT"`, or the
    `"LO..HI/PARENT"` shorthand. Raises ConfigError for anything else."""
    s = text.strip()
    if re.fullmatch(r"[+-]?\d+", s):
        n = int(s)
        if n < 0:
            raise ConfigError(f"row count {n} cannot be negative")
        return n
    m = _FANOUT_RE.match(s)
    if not m:
        raise ConfigError(
            f"invalid row count {text!r}: expected an integer, "
            f"'LO..HI per PARENT' or 'LO..HI/PARENT' (e.g. \"5..20 per users\")"
        )
    low, high, parent = int(m.group(1)), int(m.group(2)), m.group(3)
    if low > high:
        raise ConfigError(f"invalid fanout range {text!r}: low bound {low} > high bound {high}")
    return FanoutSpec(low=low, high=high, parent=parent)


def _coerce_row_value(table: str, value) -> int | FanoutSpec:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(
            f"[rows] {table}: expected an integer or a fanout string "
            f"(e.g. \"5..20 per users\"), got {value!r}"
        )
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"[rows] {table}: row count cannot be negative")
        return value
    try:
        return parse_fanout_value(value)
    except ConfigError as e:
        raise ConfigError(f"[rows] {table}: {e}") from None


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------

def _warn(path: Path | None, message: str) -> None:
    origin = path.name if path is not None else CONFIG_FILENAME
    warnings.warn(f"{origin}: {message}", stacklevel=3)


def _coerce_date(value, context: str) -> date:
    if isinstance(value, datetime):  # datetime is a date subclass: check first
        raise ConfigError(f"{context}: expected a date (YYYY-MM-DD), got a datetime {value!r}")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ConfigError(f"{context}: invalid date {value!r}, expected YYYY-MM-DD") from None
    raise ConfigError(f"{context}: expected a date (YYYY-MM-DD), got {value!r}")


def _parse_project(section, path: Path | None, cfg: SynthConfig) -> None:
    if not isinstance(section, dict):
        raise ConfigError("[project] must be a table")
    for key, value in section.items():
        if key == "seed":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"[project] seed must be an integer, got {value!r}")
            cfg.seed = value
        elif key == "as_of":
            cfg.as_of = _coerce_date(value, "[project] as_of")
        else:
            _warn(path, f"unknown key [project].{key} ignored")


def _parse_generation(section, path: Path | None, cfg: SynthConfig) -> None:
    if not isinstance(section, dict):
        raise ConfigError("[generation] must be a table")
    for key, value in section.items():
        expected = _GENERATION_KEYS.get(key)
        if expected is None:
            _warn(path, f"unknown key [generation].{key} ignored")
            continue
        if isinstance(value, bool) and expected is not bool:
            raise ConfigError(f"[generation] {key}: expected {_type_name(expected)}, got {value!r}")
        if not isinstance(value, expected):
            raise ConfigError(f"[generation] {key}: expected {_type_name(expected)}, got {value!r}")
        if key in _RATE_KEYS:
            value = float(value)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"[generation] {key}: must be between 0 and 1, got {value}")
        if key == "fanout" and value not in ("uniform", "zipfian"):
            raise ConfigError(
                f"[generation] fanout: must be 'uniform' or 'zipfian', got {value!r}"
            )
        if key == "pool_dirs":
            if not all(isinstance(v, str) for v in value):
                raise ConfigError("[generation] pool_dirs: must be a list of directory strings")
            value = list(value)
        cfg.generation[key] = value


def _type_name(expected) -> str:
    if isinstance(expected, tuple):
        return "/".join(t.__name__ for t in expected)
    return expected.__name__


_OVERRIDE_KEYS = {"values", "weights", "min", "max"}


def _parse_column_override(key: str, spec, path: Path | None) -> ColumnOverride:
    if "." not in key:
        raise ConfigError(
            f'[columns] key {key!r}: expected a quoted "table.column" key, '
            f'e.g. [columns."users.status"]'
        )
    table, column = key.split(".", 1)
    if not isinstance(spec, dict):
        raise ConfigError(f"[columns.\"{key}\"] must be a table of override keys")
    for k in spec:
        if k not in _OVERRIDE_KEYS:
            _warn(path, f'unknown key [columns."{key}"].{k} ignored')
    ov = ColumnOverride(table=table, column=column)

    if "values" in spec:
        values = spec["values"]
        if not isinstance(values, list) or not values:
            raise ConfigError(f'[columns."{key}"] values: must be a non-empty list')
        ov.values = list(values)
    if "weights" in spec:
        if ov.values is None:
            raise ConfigError(f'[columns."{key}"] weights: requires a values list')
        weights = spec["weights"]
        if (
            not isinstance(weights, list)
            or not weights
            or not all(isinstance(w, (int, float)) and not isinstance(w, bool) for w in weights)
        ):
            raise ConfigError(f'[columns."{key}"] weights: must be a list of numbers')
        if len(weights) != len(ov.values):
            raise ConfigError(
                f'[columns."{key}"] weights: length {len(weights)} does not match '
                f"values length {len(ov.values)}"
            )
        if any(w < 0 for w in weights) or sum(weights) <= 0:
            raise ConfigError(f'[columns."{key}"] weights: must be non-negative with a positive sum')
        ov.weights = [float(w) for w in weights]
    for bound in ("min", "max"):
        if bound in spec:
            v = spec[bound]
            if isinstance(v, bool) or not isinstance(v, (int, float, date, datetime)):
                raise ConfigError(
                    f'[columns."{key}"] {bound}: expected a number or date, got {v!r}'
                )
            setattr(ov, bound, v)
    if ov.values is not None and (ov.min is not None or ov.max is not None):
        raise ConfigError(
            f'[columns."{key}"]: use either a values list or min/max bounds, not both'
        )
    if ov.values is None and ov.min is None and ov.max is None:
        raise ConfigError(f'[columns."{key}"]: no override given (expected values or min/max)')
    if ov.min is not None and ov.max is not None:
        try:
            disjoint = _cmp_gt(ov.min, ov.max)
        except TypeError:
            raise ConfigError(
                f'[columns."{key}"]: min ({ov.min!r}) and max ({ov.max!r}) are not comparable'
            ) from None
        if disjoint:
            raise ConfigError(f'[columns."{key}"]: min ({ov.min!r}) > max ({ov.max!r})')
    return ov


def _parse_columns(section, path: Path | None, cfg: SynthConfig) -> None:
    if not isinstance(section, dict):
        raise ConfigError("[columns] must be a table")
    flat: dict[str, dict] = {}
    for key, spec in section.items():
        if "." not in key and isinstance(spec, dict) and spec and all(
            isinstance(v, dict) for v in spec.values()
        ):
            # Unquoted [columns.users.status] parses as nested tables.
            for sub, subspec in spec.items():
                flat[f"{key}.{sub}"] = subspec
        else:
            flat[key] = spec
    for key, spec in flat.items():
        cfg.columns[key] = _parse_column_override(key, spec, path)


def load_config(path: str | Path) -> SynthConfig:
    """Load and validate a synthscale.toml. Unknown keys warn; structural or
    type problems raise ConfigError with the offending key named."""
    path = Path(path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: invalid TOML -- {e}") from None

    cfg = SynthConfig(path=path)
    for key, section in data.items():
        if key == "project":
            _parse_project(section, path, cfg)
        elif key == "rows":
            if not isinstance(section, dict):
                raise ConfigError("[rows] must be a table of table = count entries")
            for table, value in section.items():
                cfg.rows[table] = _coerce_row_value(table, value)
        elif key == "generation":
            _parse_generation(section, path, cfg)
        elif key == "columns":
            _parse_columns(section, path, cfg)
        else:
            _warn(path, f"unknown section [{key}] ignored")
    return cfg


# --------------------------------------------------------------------------
# Fanout resolution: specs -> concrete row counts + per-parent allocations
# --------------------------------------------------------------------------

def resolve_row_counts(
    schema: SchemaModel,
    order: list[str],
    specs: dict[str, int | FanoutSpec],
    seed: int,
    deferred_fks: list | None = None,
) -> tuple[dict[str, int], dict[str, FanoutAllocation]]:
    """Resolve row specs in dependency order (`order`, parents first) into
    concrete counts. A FanoutSpec draws randint(low, high) once per parent row
    from a per-child seeded rng (independent of the engine rng and of spec
    ordering); the child count is the sum, and the exact draw is returned so
    FK generation can honor it per parent.

    Validation: the `per` target must be a direct single-column FK parent of
    the child, referenced by exactly one FK, and that FK must not be part of a
    broken dependency cycle.
    """
    deferred = {
        (d.child_table, tuple(d.fk.columns)) for d in (deferred_fks or [])
    }
    counts: dict[str, int] = {}
    allocations: dict[str, FanoutAllocation] = {}
    for tname in order:
        spec = specs.get(tname)
        if spec is None:
            continue  # caller decides whether missing tables are an error
        if isinstance(spec, int):
            counts[tname] = spec
            continue
        parent = spec.parent
        if parent == tname:
            raise ConfigError(
                f"rows: {tname} = \"..per {parent}\": a table cannot fan out over "
                f"itself; self-referencing hierarchies are shaped with root_fraction instead"
            )
        if parent not in schema.tables:
            raise ConfigError(
                f"rows: {tname} fans out over unknown table {parent!r} (not in the schema)"
            )
        fks = [fk for fk in schema.tables[tname].foreign_keys if fk.ref_table == parent]
        if not fks:
            raise ConfigError(
                f"rows: {tname} = \"{spec.low}..{spec.high} per {parent}\", but "
                f"{parent!r} is not a direct FK parent of {tname!r}. Fanout ranges "
                f"only apply along an existing foreign key."
            )
        if len(fks) > 1:
            raise ConfigError(
                f"rows: {tname} has {len(fks)} foreign keys to {parent!r}; a fanout "
                f"range is ambiguous with multiple FKs to the same parent. Give "
                f"{tname} an explicit integer row count instead."
            )
        fk = fks[0]
        if len(fk.columns) != 1:
            raise ConfigError(
                f"rows: the FK {tname}.({', '.join(fk.columns)}) -> {parent} is "
                f"composite; fanout ranges support single-column FKs only."
            )
        if (tname, tuple(fk.columns)) in deferred:
            raise ConfigError(
                f"rows: the FK {tname}.{fk.columns[0]} -> {parent} is part of a "
                f"dependency cycle and is backfilled after generation; fanout "
                f"ranges cannot be honored for it. Use an integer row count."
            )
        parent_n = counts.get(parent)
        if parent_n is None:
            raise ConfigError(
                f"rows: {tname} fans out over {parent!r}, but {parent!r} has no row "
                f"count. Give the parent an integer count (or its own fanout range)."
            )
        rng = random.Random(f"{seed}|fanout|{tname}")
        draws = [rng.randint(spec.low, spec.high) for _ in range(parent_n)]
        counts[tname] = sum(draws)
        allocations[tname] = FanoutAllocation(parent_table=parent, per_parent=draws)
    return counts, allocations


# --------------------------------------------------------------------------
# Per-column overrides -> schema mutation (enum narrowing + injected bounds)
# --------------------------------------------------------------------------

def _value_type_error(value, dtype: DataType) -> str | None:
    if dtype in _INT_TYPES:
        if not isinstance(value, int) or isinstance(value, bool):
            return "an integer"
    elif dtype in (DataType.NUMERIC, DataType.FLOAT):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "a number"
    elif dtype in _TEXT_TYPES:
        if not isinstance(value, str):
            return "a string"
    elif dtype == DataType.BOOLEAN:
        if not isinstance(value, bool):
            return "a boolean"
    elif dtype == DataType.DATE:
        if not isinstance(value, date) or isinstance(value, datetime):
            return "a date"
    elif dtype == DataType.TIMESTAMP:
        if not isinstance(value, (date, datetime)):
            return "a date/datetime"
    return None


def _cmp_norm(a, b):
    """Make date and datetime comparable (date -> midnight datetime)."""
    if isinstance(a, datetime) and type(b) is date:
        b = datetime(b.year, b.month, b.day)
    elif isinstance(b, datetime) and type(a) is date:
        a = datetime(a.year, a.month, a.day)
    return a, b


def _cmp_gt(a, b) -> bool:
    a, b = _cmp_norm(a, b)
    return a > b


def _sql_literal(v) -> str:
    if isinstance(v, (date, datetime)):
        return f"'{v.isoformat()}'"
    return repr(v)


def apply_column_overrides(schema: SchemaModel, overrides: dict[str, ColumnOverride]) -> None:
    """Apply `[columns]` overrides onto the parsed SchemaModel, so the normal
    generation machinery enforces them:

    * `values` (closed list) narrows `column.enum_values` -- the enum/validator
      machinery then treats it exactly like a `CHECK (col IN (...))`. If the
      column already has an enum/IN domain, the config list must be a subset.
    * `min`/`max` are injected as a synthetic single-column range CHECK, which
      the bounds machinery *intersects* with any real CHECK bounds (config
      narrows, never widens; disjoint intersections error here).

    Mutates `schema` in place. Raises ConfigError for unknown tables/columns,
    dtype mismatches, non-subset value lists, and disjoint bounds.
    """
    for key, ov in overrides.items():
        table = schema.tables.get(ov.table)
        if table is None:
            raise ConfigError(f'[columns."{key}"]: unknown table {ov.table!r}')
        try:
            column = table.get_column(ov.column)
        except KeyError:
            raise ConfigError(
                f'[columns."{key}"]: table {ov.table!r} has no column {ov.column!r}'
            ) from None
        single_col_checks = [
            c.parsed for c in table.check_constraints if len(c.columns_involved) == 1
        ]

        if ov.values is not None:
            for v in ov.values:
                problem = _value_type_error(v, column.dtype)
                if problem:
                    raise ConfigError(
                        f'[columns."{key}"] values: {v!r} is not {problem} '
                        f"(column type is {column.dtype.value})"
                    )
                if (
                    column.max_length is not None
                    and isinstance(v, str)
                    and len(v) > column.max_length
                ):
                    raise ConfigError(
                        f'[columns."{key}"] values: {v!r} exceeds VARCHAR({column.max_length})'
                    )
            existing = column.enum_values
            if existing is None:
                in_list = apply_single_column_bounds(single_col_checks, column.name).get("in_list")
                existing = in_list
            if existing is not None:
                allowed = set(existing)
                extra = [v for v in ov.values if v not in allowed]
                if extra:
                    raise ConfigError(
                        f'[columns."{key}"] values: {extra!r} not allowed by the '
                        f"column's CHECK/enum domain {sorted(allowed)!r}; the config "
                        f"list must be a subset."
                    )
            column.enum_values = list(ov.values)

        if ov.min is not None or ov.max is not None:
            if column.dtype not in _NUM_TYPES + (DataType.DATE, DataType.TIMESTAMP):
                raise ConfigError(
                    f'[columns."{key}"]: min/max bounds are not supported for '
                    f"column type {column.dtype.value}"
                )
            for bound_name in ("min", "max"):
                v = getattr(ov, bound_name)
                if v is None:
                    continue
                problem = _value_type_error(v, column.dtype)
                if problem:
                    raise ConfigError(
                        f'[columns."{key}"] {bound_name}: {v!r} is not {problem} '
                        f"(column type is {column.dtype.value})"
                    )
            existing = apply_single_column_bounds(single_col_checks, column.name)
            try:
                lo = _intersect_bound(existing.get("min"), ov.min, max)
                hi = _intersect_bound(existing.get("max"), ov.max, min)
                if lo is not None and hi is not None and _cmp_gt(lo, hi):
                    raise ConfigError(
                        f'[columns."{key}"]: the configured bounds '
                        f"[{ov.min!r}, {ov.max!r}] are disjoint with the column's "
                        f"CHECK bounds [{existing.get('min')!r}, {existing.get('max')!r}]. "
                        f"Config bounds can only narrow CHECK bounds, never widen them."
                    )
            except TypeError:
                raise ConfigError(
                    f'[columns."{key}"]: bounds {ov.min!r}/{ov.max!r} are not '
                    f"comparable with the column's CHECK bounds"
                ) from None
            parts = []
            if ov.min is not None:
                parts.append(f"{column.name} >= {_sql_literal(ov.min)}")
            if ov.max is not None:
                parts.append(f"{column.name} <= {_sql_literal(ov.max)}")
            table.check_constraints.append(
                CheckConstraint(
                    raw_sql=" AND ".join(parts),
                    columns_involved=[column.name],
                    parsed={
                        "kind": "range",
                        "column": column.name,
                        "low": ov.min,
                        "high": ov.max,
                    },
                )
            )


def _intersect_bound(existing, configured, pick):
    if existing is None:
        return configured
    if configured is None:
        return existing
    a, b = _cmp_norm(existing, configured)
    return pick(a, b)
