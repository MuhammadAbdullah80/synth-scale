"""Tests for the synthscale.toml authoring/config layer (datagen/config.py):
loader validation, fanout-range resolution and exact per-parent honoring,
per-column overrides, and CLI wiring/precedence (CLI flags > config file >
built-in defaults)."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datagen.cli import app
from datagen.config import (
    ColumnOverride,
    ConfigError,
    FanoutSpec,
    apply_column_overrides,
    load_config,
    parse_fanout_value,
    resolve_row_counts,
)
from datagen.ddl_parser import parse_ddl
from datagen.dependency_graph import DeferredFK, build_generation_plan
from datagen.engine import EngineConfig, run_engine
from datagen.schema_model import ForeignKey
from datagen.validator import validate

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_DDL = FIXTURES / "sample.sql"
HARD_DDL = FIXTURES / "hard.sql"

runner = CliRunner()


def _sample_schema():
    return parse_ddl(SAMPLE_DDL.read_text(encoding="utf-8"))


def _hard_schema():
    return parse_ddl(HARD_DDL.read_text(encoding="utf-8"))


def _write(tmp_path: Path, text: str, name: str = "synthscale.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _csv_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.reader(f)) - 1


FULL_TOML = """
[project]
seed = 7
as_of = 2026-01-01

[rows]
users = 100
orders = "5..20 per users"
order_items = "1..5 per orders"

[generation]
null_rate = 0.02
fk_null_rate = 0.1
fanout = "uniform"
coherence = true
root_fraction = 0.15

[columns."users.status"]
values = ["active", "trial", "churned"]
weights = [0.7, 0.2, 0.1]

[columns."products.price"]
min = 5.0
max = 500.0
"""


# =========================================================================
# Loader: happy path
# =========================================================================

def test_load_happy_path(tmp_path):
    cfg = load_config(_write(tmp_path, FULL_TOML))
    assert cfg.seed == 7
    assert cfg.as_of == date(2026, 1, 1)
    assert cfg.rows["users"] == 100
    assert cfg.rows["orders"] == FanoutSpec(low=5, high=20, parent="users")
    assert cfg.rows["order_items"] == FanoutSpec(low=1, high=5, parent="orders")
    assert cfg.generation["null_rate"] == 0.02
    assert cfg.generation["fanout"] == "uniform"
    assert cfg.generation["coherence"] is True
    assert cfg.generation["root_fraction"] == 0.15
    ov = cfg.columns["users.status"]
    assert ov.table == "users" and ov.column == "status"
    assert ov.values == ["active", "trial", "churned"]
    assert ov.weights == [0.7, 0.2, 0.1]
    price = cfg.columns["products.price"]
    assert price.min == 5.0 and price.max == 500.0


def test_load_as_of_string_form(tmp_path):
    cfg = load_config(_write(tmp_path, '[project]\nas_of = "2025-06-15"\n'))
    assert cfg.as_of == date(2025, 6, 15)


def test_load_unquoted_nested_columns_key(tmp_path):
    cfg = load_config(_write(tmp_path, "[columns.users.status]\nvalues = ['a']\n"))
    assert cfg.columns["users.status"].values == ["a"]


def test_unknown_section_warns(tmp_path):
    with pytest.warns(UserWarning, match=r"unknown section \[projects\]"):
        load_config(_write(tmp_path, "[projects]\nseed = 1\n"))


def test_unknown_keys_warn(tmp_path):
    with pytest.warns(UserWarning, match=r"unknown key \[generation\].bogus"):
        load_config(_write(tmp_path, "[generation]\nbogus = 1\n"))
    with pytest.warns(UserWarning, match=r"unknown key \[project\].name"):
        load_config(_write(tmp_path, '[project]\nname = "x"\n'))


# =========================================================================
# Loader: every validation error
# =========================================================================

@pytest.mark.parametrize(
    "toml_text, match",
    [
        ('[project]\nseed = "abc"', "seed must be an integer"),
        ("[project]\nseed = true", "seed must be an integer"),
        ('[project]\nas_of = "not-a-date"', "invalid date"),
        ("[project]\nas_of = 2026-01-01T10:00:00", "got a datetime"),
        ("[rows]\nusers = 1.5", "expected an integer or a fanout string"),
        ("[rows]\nusers = -3", "cannot be negative"),
        ('[rows]\nusers = "garbage"', "invalid row count"),
        ('[rows]\norders = "20..5 per users"', "low bound 20 > high bound 5"),
        ('[generation]\nnull_rate = "high"', "expected int/float"),
        ("[generation]\nnull_rate = 1.5", "between 0 and 1"),
        ('[generation]\ncoherence = "yes"', "expected bool"),
        ('[generation]\nfanout = "power-law"', "'uniform' or 'zipfian'"),
        ("[generation]\npool_dirs = [1, 2]", "list of directory strings"),
        ('[columns.nodot]\nvalues = ["a"]', "expected a quoted"),
        ('[columns."u.c"]\nvalues = []', "non-empty list"),
        ('[columns."u.c"]\nweights = [1.0]', "requires a values list"),
        ('[columns."u.c"]\nvalues = ["a", "b"]\nweights = [1.0]', "does not match"),
        ('[columns."u.c"]\nvalues = ["a"]\nweights = ["x"]', "list of numbers"),
        ('[columns."u.c"]\nvalues = ["a", "b"]\nweights = [-1.0, 2.0]', "non-negative"),
        ('[columns."u.c"]\nmin = 10\nmax = 2', "min .* > max"),
        ('[columns."u.c"]\nmin = "low"', "expected a number or date"),
        ('[columns."u.c"]\nvalues = ["a"]\nmin = 1', "not both"),
        ('[columns."u.c"]\nignored_key = 1', "no override given"),
    ],
)
def test_loader_errors(tmp_path, toml_text, match):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ConfigError, match=match):
            load_config(_write(tmp_path, toml_text))


def test_invalid_toml_syntax(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(_write(tmp_path, "not toml ==="))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


# =========================================================================
# Fanout spec parsing
# =========================================================================

def test_parse_fanout_forms():
    assert parse_fanout_value("5..20 per users") == FanoutSpec(5, 20, "users")
    assert parse_fanout_value("5..20/users") == FanoutSpec(5, 20, "users")
    assert parse_fanout_value("  5 .. 20  per   users ") == FanoutSpec(5, 20, "users")
    assert parse_fanout_value("7") == 7


def test_parse_fanout_bad_forms():
    with pytest.raises(ConfigError, match="low bound"):
        parse_fanout_value("20..5 per users")
    with pytest.raises(ConfigError, match="expected an integer"):
        parse_fanout_value("5..20 by users")
    with pytest.raises(ConfigError, match="negative"):
        parse_fanout_value("-4")


# =========================================================================
# resolve_row_counts: dependency-order resolution + validation
# =========================================================================

BASE_SPECS = {
    "categories": 3,
    "users": 10,
    "employees": 2,
    "products": 6,
    "orders": FanoutSpec(2, 4, "users"),
    "order_items": FanoutSpec(1, 2, "orders"),
}


def _resolve(schema=None, specs=None, seed=42):
    schema = schema or _sample_schema()
    plan = build_generation_plan(schema)
    return resolve_row_counts(
        schema, plan.order, specs or dict(BASE_SPECS), seed=seed,
        deferred_fks=plan.deferred_fks,
    )


def test_resolve_three_level_chain_in_dependency_order():
    counts, allocs = _resolve()
    draws = allocs["orders"].per_parent
    assert len(draws) == 10                       # one draw per users row
    assert all(2 <= d <= 4 for d in draws)
    assert counts["orders"] == sum(draws)         # total == sum of draws
    item_draws = allocs["order_items"].per_parent
    assert len(item_draws) == counts["orders"]    # chained off orders' resolved count
    assert all(1 <= d <= 2 for d in item_draws)
    assert counts["order_items"] == sum(item_draws)
    assert allocs["orders"].parent_table == "users"
    assert allocs["order_items"].parent_table == "orders"


def test_resolve_is_deterministic():
    a = _resolve(seed=123)
    b = _resolve(seed=123)
    assert a[0] == b[0]
    assert {k: v.per_parent for k, v in a[1].items()} == {
        k: v.per_parent for k, v in b[1].items()
    }
    c = _resolve(seed=124)
    assert c[0] != a[0] or c[1]["orders"].per_parent != a[1]["orders"].per_parent


def test_resolve_errors_when_target_is_not_a_parent():
    specs = dict(BASE_SPECS)
    specs["orders"] = FanoutSpec(1, 2, "products")
    with pytest.raises(ConfigError, match="not a direct FK parent"):
        _resolve(specs=specs)


def test_resolve_errors_on_unknown_parent():
    specs = dict(BASE_SPECS)
    specs["orders"] = FanoutSpec(1, 2, "userz")
    with pytest.raises(ConfigError, match="unknown table"):
        _resolve(specs=specs)


def test_resolve_errors_when_parent_count_missing():
    specs = dict(BASE_SPECS)
    del specs["users"]
    with pytest.raises(ConfigError, match="has no row count"):
        _resolve(specs=specs)


def test_resolve_errors_on_self_fanout():
    specs = dict(BASE_SPECS)
    specs["employees"] = FanoutSpec(1, 2, "employees")
    with pytest.raises(ConfigError, match="cannot fan out over"):
        _resolve(specs=specs)


def test_resolve_errors_on_multiple_fks_to_same_parent():
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(50));
    CREATE TABLE messages (
      id SERIAL PRIMARY KEY,
      sender_id INT NOT NULL REFERENCES users(id),
      recipient_id INT NOT NULL REFERENCES users(id)
    );
    """
    schema = parse_ddl(ddl)
    plan = build_generation_plan(schema)
    with pytest.raises(ConfigError, match="2 foreign keys .* ambiguous"):
        resolve_row_counts(
            schema, plan.order,
            {"users": 5, "messages": FanoutSpec(1, 2, "users")},
            seed=42, deferred_fks=plan.deferred_fks,
        )


def test_resolve_errors_on_deferred_cycle_fk():
    schema = _sample_schema()
    plan = build_generation_plan(schema)
    fake_deferred = [
        DeferredFK(
            child_table="orders",
            fk=ForeignKey(columns=["user_id"], ref_table="users", ref_columns=["id"]),
        )
    ]
    with pytest.raises(ConfigError, match="dependency cycle"):
        resolve_row_counts(
            schema, plan.order, dict(BASE_SPECS), seed=42, deferred_fks=fake_deferred
        )


# =========================================================================
# Engine: exact per-parent honoring
# =========================================================================

def test_fanout_allocation_exactly_honored():
    schema = _sample_schema()
    counts, allocs = _resolve(schema=schema)
    config = EngineConfig(seed=42, fanout_allocations=allocs)
    generated = run_engine(schema, counts, config)

    # orders.user_id: users row i (id = i+1, sequential PK) gets exactly its draw.
    draws = allocs["orders"].per_parent
    user_ids = [row["id"] for row in generated["users"]]
    actual = {uid: 0 for uid in user_ids}
    for row in generated["orders"]:
        actual[row["user_id"]] += 1
    for uid, drawn in zip(user_ids, draws):
        assert actual[uid] == drawn, f"user {uid}: expected {drawn} orders, got {actual[uid]}"
    assert sum(draws) == len(generated["orders"])

    # order_items (composite PK junction): each order gets exactly its draw.
    item_draws = allocs["order_items"].per_parent
    order_ids = [row["id"] for row in generated["orders"]]
    item_counts = {oid: 0 for oid in order_ids}
    for row in generated["order_items"]:
        item_counts[row["order_id"]] += 1
    for oid, drawn in zip(order_ids, item_draws):
        assert item_counts[oid] == drawn

    # Nothing else broke: independent validator pass is clean.
    report = validate(schema, generated)
    assert report.violations_found == 0, report.errors


def test_fanout_assignment_not_clustered_by_index():
    schema = _sample_schema()
    counts, allocs = _resolve(schema=schema)
    generated = run_engine(schema, counts, EngineConfig(seed=42, fanout_allocations=allocs))
    user_ids = [row["user_id"] for row in generated["orders"]]
    # A deterministic shuffle means the column is NOT sorted (children of
    # parent 1 first, then parent 2, ...). Astronomically unlikely if shuffled.
    assert user_ids != sorted(user_ids)


def test_fanout_determinism_same_seed_same_bytes():
    def _run():
        schema = _sample_schema()
        counts, allocs = _resolve(schema=schema, seed=7)
        return run_engine(schema, counts, EngineConfig(seed=7, fanout_allocations=allocs))

    assert _run() == _run()


# =========================================================================
# Per-column overrides
# =========================================================================

def test_values_override_respected():
    schema = _sample_schema()
    overrides = {
        "orders.status": ColumnOverride(
            table="orders", column="status", values=["pending", "shipped"]
        )
    }
    apply_column_overrides(schema, overrides)
    assert schema.tables["orders"].get_column("status").enum_values == ["pending", "shipped"]
    counts = {"categories": 3, "users": 5, "employees": 2, "products": 5,
              "orders": 40, "order_items": 10}
    generated = run_engine(schema, counts, EngineConfig(seed=42))
    statuses = {r["status"] for r in generated["orders"] if r["status"] is not None}
    assert statuses <= {"pending", "shipped"}
    assert validate(schema, generated).violations_found == 0


def test_weights_approximately_honored_at_n_2000():
    schema = _sample_schema()
    overrides = {
        "orders.status": ColumnOverride(
            table="orders", column="status",
            values=["pending", "shipped"], weights=[0.9, 0.1],
        )
    }
    apply_column_overrides(schema, overrides)
    counts = {"categories": 3, "users": 30, "employees": 2, "products": 10,
              "orders": 2000, "order_items": 20}
    config = EngineConfig(seed=42, null_rate=0.0, column_overrides=overrides)
    generated = run_engine(schema, counts, config)
    vals = [r["status"] for r in generated["orders"]]
    assert set(vals) <= {"pending", "shipped"}
    share_pending = vals.count("pending") / len(vals)
    assert 0.85 <= share_pending <= 0.95, share_pending
    assert validate(schema, generated).violations_found == 0


def test_min_max_intersects_with_check_bounds():
    schema = _sample_schema()
    overrides = {
        "products.price": ColumnOverride(
            table="products", column="price", min=5.0, max=500.0
        )
    }
    apply_column_overrides(schema, overrides)  # CHECK (price > 0) intersected
    counts = {"categories": 3, "users": 5, "employees": 2, "products": 200,
              "orders": 5, "order_items": 5}
    generated = run_engine(schema, counts, EngineConfig(seed=42, null_rate=0.0))
    prices = [r["price"] for r in generated["products"] if r["price"] is not None]
    assert prices and all(5.0 <= p <= 500.0 for p in prices)
    assert validate(schema, generated).violations_found == 0


def test_values_must_be_subset_of_enum():
    schema = _sample_schema()
    with pytest.raises(ConfigError, match="subset"):
        apply_column_overrides(schema, {
            "orders.status": ColumnOverride(
                table="orders", column="status", values=["pending", "bogus"]
            )
        })


def test_disjoint_bounds_error():
    schema = _sample_schema()
    with pytest.raises(ConfigError, match="disjoint"):
        apply_column_overrides(schema, {
            "products.price": ColumnOverride(
                table="products", column="price", min=-10.0, max=0.2
            )  # CHECK (price > 0) -> generator min 1; [.., 0.2] is disjoint
        })


def test_values_type_checked_against_dtype():
    schema = _sample_schema()
    with pytest.raises(ConfigError, match="not a string"):
        apply_column_overrides(schema, {
            "users.first_name": ColumnOverride(
                table="users", column="first_name", values=[1, 2]
            )
        })
    with pytest.raises(ConfigError, match="not supported"):
        apply_column_overrides(schema, {
            "users.first_name": ColumnOverride(
                table="users", column="first_name", min=1, max=5
            )
        })


def test_override_unknown_table_and_column():
    schema = _sample_schema()
    with pytest.raises(ConfigError, match="unknown table"):
        apply_column_overrides(schema, {
            "nope.x": ColumnOverride(table="nope", column="x", values=["a"])
        })
    with pytest.raises(ConfigError, match="no column"):
        apply_column_overrides(schema, {
            "users.nope": ColumnOverride(table="users", column="nope", values=["a"])
        })


# =========================================================================
# Full-feature config against the adversarial fixture: zero violations
# =========================================================================

HARD_TOML = """
[project]
seed = 11
as_of = 2026-01-01

[rows]
organizations = 6
categories = 5
coupons = 8
users = "2..4 per organizations"
products = "3..5 per organizations"
subscriptions = "1..2 per organizations"
inventory = "1..2 per products"
orders = "1..3 per users"
order_items = "1..2 per orders"
audit_log = "2..4 per organizations"

[generation]
null_rate = 0.02
fk_null_rate = 0.05
fanout = "uniform"
coherence = true
root_fraction = 0.3

[columns."products.status"]
values = ["draft", "active"]
weights = [0.3, 0.7]

[columns."products.price"]
min = 5.0
max = 500.0

[columns."coupons.valid_from"]
min = 2024-06-01
max = 2025-06-01
"""


def test_hard_fixture_full_feature_config_zero_violations(tmp_path):
    cfg = load_config(_write(tmp_path, HARD_TOML))
    schema = _hard_schema()
    apply_column_overrides(schema, cfg.columns)
    plan = build_generation_plan(schema)
    counts, allocs = resolve_row_counts(
        schema, plan.order, cfg.rows, seed=cfg.seed, deferred_fks=plan.deferred_fks
    )
    config = EngineConfig(
        seed=cfg.seed,
        as_of=cfg.as_of,
        null_rate=cfg.generation["null_rate"],
        fk_null_rate=cfg.generation["fk_null_rate"],
        fanout=cfg.generation["fanout"],
        coherence=cfg.generation["coherence"],
        root_fraction=cfg.generation["root_fraction"],
        fanout_allocations=allocs,
        column_overrides=cfg.columns,
    )
    generated = run_engine(schema, counts, config, plan=plan)
    report = validate(schema, generated)
    assert report.violations_found == 0, report.errors

    # Exactness on a UUID-PK parent (users -> orders), and on a composite
    # UNIQUE alloc member (organizations -> products via UNIQUE(org_id, sku)).
    for child, parent, fk_col in [
        ("orders", "users", "user_id"),
        ("products", "organizations", "org_id"),
        ("users", "organizations", "org_id"),
        ("order_items", "orders", "order_id"),
    ]:
        draws = allocs[child].per_parent
        parent_ids = [r["id"] for r in generated[parent]]
        got = {pid: 0 for pid in parent_ids}
        for r in generated[child]:
            got[r[fk_col]] += 1
        for pid, drawn in zip(parent_ids, draws):
            assert got[pid] == drawn, f"{child} per {parent} ({pid}): {got[pid]} != {drawn}"

    # Column overrides took effect.
    statuses = [r["status"] for r in generated["products"]]
    assert set(statuses) <= {"draft", "active"}
    assert all(5.0 <= r["price"] <= 500.0 for r in generated["products"])
    assert all(
        date(2024, 6, 1) <= r["valid_from"] <= date(2025, 6, 1)
        for r in generated["coupons"]
    )


# =========================================================================
# CLI: --config / --no-config / precedence / round-trip
# =========================================================================

SAMPLE_CLI_TOML = """
[project]
seed = 7

[rows]
categories = 3
users = 10
employees = 4
products = 6
orders = "2..4 per users"
order_items = "1..2 per orders"

[generation]
null_rate = 0.02
"""


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def test_cli_config_round_trip(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML)
    out = tmp_path / "out"
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg), "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    assert "Violations found: 0" in result.stdout
    assert _csv_rows(out / "users.csv") == 10
    # Fanout totals match an out-of-band resolution with the config's seed.
    counts, _ = _resolve(specs={
        "categories": 3, "users": 10, "employees": 4, "products": 6,
        "orders": FanoutSpec(2, 4, "users"), "order_items": FanoutSpec(1, 2, "orders"),
    }, seed=7)
    assert _csv_rows(out / "orders.csv") == counts["orders"]
    assert _csv_rows(out / "order_items.csv") == counts["order_items"]


def test_cli_config_byte_identical_reruns(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML)
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg), "--out", str(out))
        assert result.exit_code == 0, result.output + result.stderr
        outs.append(out)
    for f in outs[0].iterdir():
        assert f.read_bytes() == (outs[1] / f.name).read_bytes(), f.name


def test_cli_auto_discovery_and_no_config(tmp_path, monkeypatch):
    _write(tmp_path, SAMPLE_CLI_TOML)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    # Auto-discovered ./synthscale.toml supplies the row counts.
    result = _invoke("--ddl", str(SAMPLE_DDL), "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    assert _csv_rows(out / "users.csv") == 10
    # --no-config ignores it: now --rows is missing entirely.
    result = _invoke("--ddl", str(SAMPLE_DDL), "--no-config", "--out", str(out))
    assert result.exit_code == 1
    assert "--rows is required" in result.stderr


def test_cli_explicit_config_beats_discovery(tmp_path, monkeypatch):
    _write(tmp_path, SAMPLE_CLI_TOML)  # discovered one: users = 10
    other = _write(tmp_path, SAMPLE_CLI_TOML.replace("users = 10", "users = 4"),
                   name="other.toml")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(other), "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    assert _csv_rows(out / "users.csv") == 4


def test_cli_flag_beats_config_beats_default(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML)  # [project] seed = 7
    out_file_seed = tmp_path / "file_seed"
    out_flag_seed = tmp_path / "flag_seed"
    out_no_config = tmp_path / "no_config"

    # Config seed (7) used when the flag is absent.
    r = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg), "--out", str(out_file_seed))
    assert r.exit_code == 0, r.output + r.stderr
    # Explicit --seed 99 beats the config's 7.
    r = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg), "--seed", "99",
                "--out", str(out_flag_seed))
    assert r.exit_code == 0, r.output + r.stderr
    assert (out_file_seed / "users.csv").read_bytes() != (out_flag_seed / "users.csv").read_bytes()

    # Same run without a config but with everything the config supplied,
    # seed 99: must equal the flag-beats-config run byte for byte.
    r = _invoke(
        "--ddl", str(SAMPLE_DDL), "--no-config", "--seed", "99", "--null-rate", "0.02",
        "--rows", "categories=3,users=10,employees=4,products=6,orders=2..4/users,order_items=1..2 per orders",
        "--out", str(out_no_config),
    )
    assert r.exit_code == 0, r.output + r.stderr
    for f in out_flag_seed.iterdir():
        assert f.read_bytes() == (out_no_config / f.name).read_bytes(), f.name


def test_cli_rows_entry_overrides_config_per_table(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML)
    out = tmp_path / "out"
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg),
                     "--rows", "users=3", "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    assert _csv_rows(out / "users.csv") == 3          # CLI wins for users
    assert _csv_rows(out / "categories.csv") == 3     # config fills the rest
    # The orders fanout now draws over 3 parents only.
    counts, _ = _resolve(specs={
        "categories": 3, "users": 3, "employees": 4, "products": 6,
        "orders": FanoutSpec(2, 4, "users"), "order_items": FanoutSpec(1, 2, "orders"),
    }, seed=7)
    assert _csv_rows(out / "orders.csv") == counts["orders"]


def test_cli_rows_fanout_without_config(tmp_path):
    out = tmp_path / "out"
    result = _invoke(
        "--ddl", str(SAMPLE_DDL), "--no-config", "--seed", "5",
        "--rows", "categories=3,users=8,employees=2,products=6,orders=5..20/users,order_items=1..2/orders",
        "--out", str(out),
    )
    assert result.exit_code == 0, result.output + result.stderr
    n_orders = _csv_rows(out / "orders.csv")
    assert 8 * 5 <= n_orders <= 8 * 20
    assert "Violations found: 0" in result.stdout


def test_cli_config_and_no_config_are_mutually_exclusive(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML)
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg), "--no-config")
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stderr


def test_cli_config_file_not_found(tmp_path):
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(tmp_path / "nope.toml"))
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_cli_fanout_validation_error_surfaces(tmp_path):
    result = _invoke(
        "--ddl", str(SAMPLE_DDL), "--no-config",
        "--rows", "categories=3,users=8,employees=2,products=6,orders=1..2 per products,order_items=9",
    )
    assert result.exit_code == 1
    assert "not a direct FK parent" in result.stderr


def test_cli_config_error_surfaces(tmp_path):
    cfg = _write(tmp_path, '[project]\nseed = "abc"\n')
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg))
    assert result.exit_code == 1
    assert "seed must be an integer" in result.stderr


def test_cli_unknown_key_warning_on_stderr(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML + "\n[extras]\nx = 1\n")
    out = tmp_path / "out"
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg), "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    assert "unknown section" in result.stderr


def test_cli_integer_rows_replaces_config_rows_section(tmp_path):
    cfg = _write(tmp_path, SAMPLE_CLI_TOML)
    out = tmp_path / "out"
    result = _invoke("--ddl", str(SAMPLE_DDL), "--config", str(cfg),
                     "--rows", "2", "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    # Heuristic plan, not the config's counts: users = base = 2.
    assert _csv_rows(out / "users.csv") == 2
    assert _csv_rows(out / "orders.csv") == 6  # 3x users
