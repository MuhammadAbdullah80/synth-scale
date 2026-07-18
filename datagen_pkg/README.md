# datagen

Deterministic, referentially-consistent test-data generator for relational
schemas. Parses DDL (`CREATE TABLE`, with PK/FK/UNIQUE/CHECK constraints,
including `ALTER TABLE ... ADD CONSTRAINT`), resolves table dependency order,
and generates rows table-by-table so every constraint is satisfied — no LLM
calls, fully reproducible given a seed.

## Install

```bash
pip install -r requirements.txt
```

## Quick start

```bash
python -m datagen.cli \
  --ddl schema.sql \
  --rows "categories=5,users=10,employees=8,products=20,orders=30,order_items=60" \
  --seed 42 \
  --format csv \
  --out ./out
```

`--format` also accepts `sql` (batched `INSERT` statements, FK-safe order,
wrapped in a transaction) and `db` (direct load via SQLAlchemy — pass
`--db-url`, assumes the target tables already exist).

Every table in the DDL needs an entry in `--rows`; the CLI errors out
immediately (before generating anything) if one is missing.

## How it works

1. **Parse** (`ddl_parser.py`) — `sqlglot` walks the DDL into a `SchemaModel`:
   tables, columns, types, PK/FK/UNIQUE/CHECK constraints, plus a semantic hint
   per column (`email`, `first_name`, `city`, ...) inferred from its name, used
   later to pick realistic `Faker` output instead of generic strings.
2. **Dependency graph** (`dependency_graph.py`) — `networkx` builds a
   child→parent DAG from FKs and topologically sorts it. Self-referencing and
   mutual-FK cycles are broken one edge at a time: that FK is generated NULL in
   the main pass and backfilled once every involved table exists.
3. **Generate** (`engine.py`) — walks tables in dependency order. Per table:
   composite PK/UNIQUE groups made of FK columns (junction tables) first, then
   remaining FK columns (sampled from parent pools), then multi-column CHECK
   pairs (the dependent column is *constructed from* the base column, e.g.
   `end_date = start_date + random_delta`, rather than generated independently
   and retried on failure), then the table's own PK, then everything else as
   flat independent per-column lists.
4. **Validate** (`validator.py`) — independently re-checks every constraint
   (NOT NULL, PK/UNIQUE, composite UNIQUE, FK integrity, CHECK) against the
   full generated dataset and reports violations. This is a safety net for bugs
   in the generators, not the primary correctness mechanism — the generators
   are built to satisfy constraints by construction.
5. **Output** — CSV / SQL inserts / direct DB load, always written in
   dependency order.

Everything is seeded (`random.Random(seed)`, `Faker.seed(seed)`); the same
seed always produces byte-identical output.

## What's covered (MVP scope)

- Single and composite primary keys
- Single and composite foreign keys, including `ALTER TABLE ADD CONSTRAINT`
- Self-referencing FKs (e.g. `employees.manager_id`) and mutual FK cycles
  between two tables
- Many-to-many junction tables (composite PK of two FK columns)
- Single-column CHECK: numeric bounds (`>`, `>=`, `<`, `<=`), `BETWEEN`, `IN (...)`
- Two-column CHECK comparisons (`end_date > start_date`, `max <= min`, etc.),
  constructed to satisfy rather than retried
- Single and composite UNIQUE constraints
- NOT NULL / nullable columns with a configurable null rate
- Enum-like columns via `CHECK (col IN (...))`
- Fail-fast (not infinite retry) when a UNIQUE/composite-UNIQUE domain is
  smaller than the requested row count

## Known limitations (by design, not bugs)

- **Arbitrary CHECK expressions** beyond the shapes above fall back to a
  generate-and-filter loop using `simpleeval` against the raw SQL condition.
  This works for simple additional shapes but can be slow or fail for tightly
  constrained expressions; the validator will report it as a warning rather
  than silently accepting bad data.
- **Cross-table business rules** (e.g. "sum of `order_items.price` equals
  `orders.total`") aren't expressible in a single-table CHECK and aren't
  handled — SQL itself can't express them, so there's nothing in the DDL to
  parse. Would need a separate rule-config layer.
- **Generated/computed columns and triggers** are not evaluated — the DDL
  parser doesn't currently interpret `GENERATED ALWAYS AS (...)`, and triggers
  are invisible to an offline generator.
- **Semantic realism** is only as good as the hint dictionary in
  `ddl_parser._SEMANTIC_PATTERNS` / `generators/text.py`. Unrecognized text
  columns fall back to generic phrase-like text — extend the dictionary or
  wire in a custom generator for domain-specific fields.
- **Non-DDL input** (diagrams, freeform text schema descriptions) is out of
  scope for this package; it only reads SQL DDL.
- **Scale**: uniqueness enforcement uses Python-level retry + set tracking,
  fine to tens of thousands of rows per table; millions of rows would want a
  vectorized (numpy) rewrite of the generator loops.
- The validator's "repair" is currently report-only: it tells you exactly what
  and where a violation is (table, column, row index, constraint) rather than
  silently patching it, on the view that a generator bug should be visible and
  fixed at the source rather than papered over.

## Repo layout

```
datagen/
  schema_model.py       # dataclasses: the parser/generator contract
  ddl_parser.py          # sqlglot-based DDL -> SchemaModel
  dependency_graph.py    # networkx dependency graph, topo sort, cycle breaking
  generators/
    base.py              # shared bound/retry helpers
    numeric.py            # int/numeric/bool/uuid/sequential-PK
    text.py                # semantic-hint-aware string generation
    date_time.py            # date/timestamp + two-column date derivation
    enum_gen.py               # enum / CHECK IN (...) values
    unique.py                  # generic uniqueness-with-retry wrapper
    fk.py                       # FK sampling from parent pools, fanout control
    linked_group.py               # composite UNIQUE/PK tuples, CHECK-linked columns
    check_eval.py                  # safe fallback evaluator for exotic CHECKs
  engine.py               # orchestrator, stages 3-5
  validator.py            # independent post-generation constraint re-check
  output/
    csv_writer.py, sql_writer.py, db_loader.py
  cli.py                  # `python -m datagen.cli ...`
tests/
  fixtures/sample.sql
  test_engine_end_to_end.py   # 16 tests covering every edge case above
```

## Configuration knobs (`EngineConfig`)

| Field | Default | Meaning |
|---|---|---|
| `seed` | 42 | RNG seed; same seed = identical output |
| `null_rate` | 0.05 | Fraction of nullable non-FK column values set to `None` |
| `fk_null_rate` | 0.1 | Fraction of nullable FK values set to `None` |
| `deferred_fk_null_rate` | 0.2 | Fraction left null for self-ref/cycle-broken FKs (e.g. employees with no manager) |
| `fanout` | `"uniform"` | `"uniform"` (even FK fan-out) or `"zipfian"` (a few parents get most children) |
| `pk_start` | `{}` | Per-table starting integer for sequential PKs |
