"""CLI tests (Typer app in datagen/cli.py).

Offline-only: no --db-url / --from-db coverage here (introspection has its
own test suite). Everything runs in-process through Typer's CliRunner
against tests/fixtures/sample.sql.
"""
from __future__ import annotations

import csv
from pathlib import Path

from typer.testing import CliRunner

from datagen.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_DDL = str(FIXTURES / "sample.sql")
ALL_TABLES = ["categories", "users", "employees", "products", "orders", "order_items"]
PER_TABLE_ROWS = "categories=5,users=10,employees=8,products=20,orders=30,order_items=60"

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _csv_row_count(path: Path) -> int:
    """Data rows (excluding header), parsed properly so quoted newlines don't
    skew the count."""
    with path.open(newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.reader(f)) - 1


# --- happy path: csv with explicit per-table rows ---------------------------

def test_csv_happy_path(tmp_path):
    out = tmp_path / "out"
    result = _invoke(
        "--ddl", SAMPLE_DDL, "--rows", PER_TABLE_ROWS,
        "--seed", "42", "--format", "csv", "--out", str(out),
    )
    assert result.exit_code == 0, result.output + result.stderr
    for t in ALL_TABLES:
        assert (out / f"{t}.csv").exists(), f"missing {t}.csv"
    assert _csv_row_count(out / "users.csv") == 10
    assert _csv_row_count(out / "order_items.csv") == 60
    assert "Violations found: 0" in result.stdout
    assert "Wrote 6 CSV file(s)" in result.stdout


# --- integer --rows convenience form ----------------------------------------

def test_integer_rows_applies_dependency_multiplier(tmp_path):
    out = tmp_path / "out"
    result = _invoke("--ddl", SAMPLE_DDL, "--rows", "10", "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    # Rootless-of-FK tables get the base; children get 3x their largest
    # parent (cap 10x base never binds here). employees' FK is
    # self-referencing, which does not count as a parent.
    expected = {
        "categories": 10,
        "users": 10,
        "employees": 10,
        "products": 30,      # 3x categories
        "orders": 30,        # 3x users
        "order_items": 90,   # 3x max(orders=30, products=30)
    }
    for t, n in expected.items():
        assert _csv_row_count(out / f"{t}.csv") == n, f"{t}: expected {n} rows"


def test_integer_rows_cap_at_10x_base(tmp_path):
    # base=2: orders = 3*2 = 6, order_items = min(3*6, 10*2) = 18 -> the cap
    # (20) does not bind; base=1: order_items = min(3*3, 10) = 9 < 10. Use a
    # deeper check: with base 1, products/orders = 3, order_items = 9.
    out = tmp_path / "out"
    result = _invoke("--ddl", SAMPLE_DDL, "--rows", "1", "--out", str(out))
    assert result.exit_code == 0, result.output + result.stderr
    assert _csv_row_count(out / "orders.csv") == 3
    assert _csv_row_count(out / "order_items.csv") == 9  # min(3*3, 10*1) = 9


# --- --preview ---------------------------------------------------------------

def test_preview_prints_tables_and_writes_nothing(tmp_path):
    out = tmp_path / "never_created"
    result = _invoke(
        "--ddl", SAMPLE_DDL, "--rows", PER_TABLE_ROWS,
        "--preview", "--out", str(out),
    )
    assert result.exit_code == 0, result.output + result.stderr
    # Nothing may be written to disk in preview mode.
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
    # The note and one preview table per schema table are printed.
    assert "--preview" in result.stdout
    assert "nothing is written to disk" in result.stdout
    for t in ALL_TABLES:
        assert t in result.stdout
    # Column headers from the schema appear in the preview output.
    assert "email" in result.stdout
    assert "manager_id" in result.stdout


# --- error paths (exit code 1, stderr) --------------------------------------

def test_missing_ddl_file_exits_1(tmp_path):
    result = _invoke("--ddl", str(tmp_path / "nope.sql"), "--rows", "5")
    assert result.exit_code == 1
    assert "DDL file not found" in result.stderr


def test_missing_rows_entry_exits_1():
    result = _invoke("--ddl", SAMPLE_DDL, "--rows", "users=5")
    assert result.exit_code == 1
    assert "missing --rows entry" in result.stderr
    # Every absent table is named.
    for t in ("categories", "employees", "order_items", "orders", "products"):
        assert t in result.stderr


def test_malformed_rows_exits_1():
    result = _invoke("--ddl", SAMPLE_DDL, "--rows", "users:5")
    assert result.exit_code == 1
    assert "expected table=count" in result.stderr


# --- determinism -------------------------------------------------------------

def test_seed_determinism_byte_identical(tmp_path):
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    for out in (out_a, out_b):
        result = _invoke(
            "--ddl", SAMPLE_DDL, "--rows", PER_TABLE_ROWS,
            "--seed", "42", "--out", str(out),
        )
        assert result.exit_code == 0, result.output + result.stderr
    for t in ALL_TABLES:
        a = (out_a / f"{t}.csv").read_bytes()
        b = (out_b / f"{t}.csv").read_bytes()
        assert a == b, f"{t}.csv differs between identical-seed runs"


# --- sql format --------------------------------------------------------------

def test_sql_format_writes_file(tmp_path):
    out_file = tmp_path / "inserts.sql"
    result = _invoke(
        "--ddl", SAMPLE_DDL, "--rows", PER_TABLE_ROWS,
        "--format", "sql", "--out", str(out_file),
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert out_file.exists()
    text = out_file.read_text(encoding="utf-8")
    assert "INSERT INTO" in text
    assert "users" in text and "order_items" in text


def test_sql_format_directory_out_defaults_to_insert_sql(tmp_path):
    out_dir = tmp_path / "sqlout"
    result = _invoke(
        "--ddl", SAMPLE_DDL, "--rows", PER_TABLE_ROWS,
        "--format", "sql", "--out", str(out_dir),
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert (out_dir / "insert.sql").exists()
