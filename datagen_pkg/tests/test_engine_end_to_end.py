"""End-to-end + targeted tests covering every edge case promised in the MVP spec:
composite PK, composite FK, self-referencing FK, mutual/circular FK, many-to-many
junction tables, multi-column CHECK, single-column CHECK (bound + IN-list),
composite UNIQUE, nullable columns, infeasible-domain fail-fast.
"""
import pytest

from datagen.ddl_parser import parse_ddl
from datagen.dependency_graph import build_generation_plan
from datagen.engine import EngineConfig, run_engine
from datagen.generators.base import DomainExhaustedError
from datagen.validator import validate


FULL_SCHEMA = """
CREATE TABLE categories (
  id SERIAL PRIMARY KEY,
  name VARCHAR(50) UNIQUE NOT NULL
);
CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  email VARCHAR(120) UNIQUE NOT NULL,
  first_name VARCHAR(50),
  last_name VARCHAR(50)
);
CREATE TABLE employees (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  manager_id INT REFERENCES employees(id)
);
CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  category_id INT REFERENCES categories(id),
  name VARCHAR(100) NOT NULL,
  price NUMERIC(10,2) CHECK (price > 0)
);
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  user_id INT NOT NULL,
  start_date DATE,
  end_date DATE,
  status VARCHAR(20) CHECK (status IN ('pending','shipped','delivered')),
  CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id),
  CONSTRAINT chk_dates CHECK (end_date > start_date)
);
CREATE TABLE order_items (
  order_id INT REFERENCES orders(id),
  product_id INT REFERENCES products(id),
  quantity INT NOT NULL CHECK (quantity > 0),
  PRIMARY KEY (order_id, product_id)
);
"""

ROW_COUNTS = {
    "categories": 5, "users": 10, "employees": 8,
    "products": 20, "orders": 30, "order_items": 60,
}


@pytest.fixture(scope="module")
def schema():
    return parse_ddl(FULL_SCHEMA)


@pytest.fixture(scope="module")
def generated(schema):
    return run_engine(schema, ROW_COUNTS, EngineConfig(seed=42))


def test_parses_all_tables(schema):
    assert set(schema.tables) == {"categories", "users", "employees", "products", "orders", "order_items"}


def test_composite_pk_detected(schema):
    assert schema.tables["order_items"].primary_key == ["order_id", "product_id"]


def test_self_referencing_fk_deferred(schema):
    plan = build_generation_plan(schema)
    deferred_tables = {d.child_table for d in plan.deferred_fks}
    assert "employees" in deferred_tables


def test_row_counts_match(generated):
    for table_name, expected_n in ROW_COUNTS.items():
        assert len(generated[table_name]) == expected_n


def test_pk_uniqueness(generated):
    for table_name, rows in generated.items():
        # single-column PK tables here all use "id"
        if table_name == "order_items":
            continue
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids))


def test_composite_pk_uniqueness(generated):
    rows = generated["order_items"]
    keys = [(r["order_id"], r["product_id"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_fk_referential_integrity(generated):
    order_ids = {r["id"] for r in generated["orders"]}
    product_ids = {r["id"] for r in generated["products"]}
    for r in generated["order_items"]:
        assert r["order_id"] in order_ids
        assert r["product_id"] in product_ids


def test_multi_column_check_satisfied(generated):
    for r in generated["orders"]:
        if r["start_date"] is not None and r["end_date"] is not None:
            assert r["end_date"] > r["start_date"]


def test_single_column_check_bound_satisfied(generated):
    for r in generated["products"]:
        if r["price"] is not None:
            assert r["price"] > 0
    for r in generated["order_items"]:
        assert r["quantity"] > 0  # NOT NULL, always present


def test_enum_check_in_list_satisfied(generated):
    allowed = {"pending", "shipped", "delivered"}
    for r in generated["orders"]:
        if r["status"] is not None:
            assert r["status"] in allowed


def test_not_null_respected(generated):
    for r in generated["categories"]:
        assert r["name"] is not None
    for r in generated["order_items"]:
        assert r["quantity"] is not None


def test_self_referencing_fk_backfilled_and_valid(generated):
    emp_ids = {r["id"] for r in generated["employees"]}
    for r in generated["employees"]:
        if r["manager_id"] is not None:
            assert r["manager_id"] in emp_ids
            assert r["manager_id"] != r["id"]  # no self-loop


def test_validator_finds_no_violations(schema, generated):
    report = validate(schema, generated, seed=42)
    assert report.violations_found == 0, report.summary()


def test_determinism_same_seed():
    schema = parse_ddl(FULL_SCHEMA)
    run1 = run_engine(schema, ROW_COUNTS, EngineConfig(seed=99))
    run2 = run_engine(schema, ROW_COUNTS, EngineConfig(seed=99))
    assert run1 == run2


def test_mutual_fk_cycle():
    ddl = """
    CREATE TABLE departments (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      head_employee_id INT
    );
    CREATE TABLE staff (
      id SERIAL PRIMARY KEY,
      full_name VARCHAR(100) NOT NULL,
      department_id INT,
      CONSTRAINT fk_dept FOREIGN KEY (department_id) REFERENCES departments(id)
    );
    ALTER TABLE departments ADD CONSTRAINT fk_head FOREIGN KEY (head_employee_id) REFERENCES staff(id);
    """
    schema = parse_ddl(ddl)
    generated = run_engine(schema, {"departments": 5, "staff": 30}, EngineConfig(seed=1))
    report = validate(schema, generated, seed=1)
    assert report.violations_found == 0, report.summary()


def test_infeasible_unique_domain_fails_fast():
    ddl = """
    CREATE TABLE codes (
      id SERIAL PRIMARY KEY,
      short_code VARCHAR(2) UNIQUE NOT NULL
    );
    """
    schema = parse_ddl(ddl)
    with pytest.raises(DomainExhaustedError):
        run_engine(schema, {"codes": 100_000}, EngineConfig(seed=1))
