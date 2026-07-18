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
   and retried on failure), then the table's own PK, then correlated pool
   groups (step 4.5, see "Coherence layer" below), then everything else as
   flat independent per-column lists, then the unparsed-CHECK
   generate-and-filter pass, then the coherence post-pass (step 7).
4. **Validate** (`validator.py`) — independently re-checks every constraint
   (NOT NULL, PK/UNIQUE, composite UNIQUE, FK integrity, CHECK) against the
   full generated dataset and reports violations, plus warn-level coherence
   realism checks (`updated_at >= created_at`, child newer than parent,
   self-referencing FKs acyclic). This is a safety net for bugs in the
   generators, not the primary correctness mechanism — the generators are
   built to satisfy constraints by construction.
5. **Output** — CSV / SQL inserts / direct DB load, always written in
   dependency order.

Everything is seeded (`random.Random(seed)`, `Faker.seed(seed)`), including
UUID columns (drawn from the seeded RNG, still valid v4 UUIDs); the same seed
always produces byte-identical output. Date/timestamp windows are anchored to
a **fixed reference date** (2026-01-01, `generators/date_time.DEFAULT_AS_OF`)
rather than the wall clock, so re-running the same command on a later day
still reproduces the same data. Pass `--as-of YYYY-MM-DD` (or
`EngineConfig.as_of`) to move the window explicitly.

## Coherence layer

On by default (`--no-coherence` disables it), the coherence layer
(`datagen/coherence.py`, `datagen/generators/pools.py`) makes the output look
like a real application's data rather than a random grid — without ever
breaking a constraint the earlier stages established, and fully inside the
same seeded RNG (same seed + config is still byte-identical).

**Cross-column chronology.** Each table gets an *anchor* timestamp (its
`created_at`/`inserted_at` column, or an event-style column like
`placed_at`/`signup_date`). Then, per row: `updated_at >= created_at` (with
~30% of rows never touched, i.e. `updated == created`); lifecycle columns
(`shipped_at`, `delivered_at`, `cancelled_at`, ...) form an ordered chain
after the anchor, each stage 2–96 h after the previous; `last_login`-style
columns land between the anchor and `as_of`. If the table has an enum
`status` column whose values match lifecycle stages, the row's status gates
which lifecycle cells are filled — a `'pending'` order has `shipped_at =
NULL`, a `'delivered'` one has both `shipped_at` and `delivered_at`, a
`'cancelled'` one has only `cancelled_at`.

**Cross-table chronology.** A child row is never older than the parent rows
it references: the child's anchor is floored at the maximum of its parents'
anchors across all FKs (NULL FK picks and anchorless parents contribute no
floor), then skewed toward the recent past and capped at `as_of`. This
composes with the per-row rules above, so `orders.updated_at >=
orders.created_at >= users.created_at` all hold together. For
deferred/cycle-broken FKs the backfill filters candidates to parents whose
anchor is not after the child's (an approximation, NULL when no candidate
qualifies).

**Hierarchy realism.** Self-referencing FKs (`employees.manager_id`,
`categories.parent_id`, `comments.reply_to_id`, ...) are backfilled so each
row only references an *earlier* row: cycles are impossible by construction
and the structure is always a forest. Row 0 is always a root; other rows are
roots with probability `--root-fraction` (default: the deferred-FK null
rate). When such a table has no cross-table floors of its own, its anchor
column is sorted ascending first, so a parent is also always *older* than
its children.

**Correlated pools.** Small packaged JSON pools under `datagen/pools/` keep
related columns mutually consistent within a row:

| Pool | Fields | Guarantees |
|---|---|---|
| `geo.json` (50 records) | `city`, `state`, `country`, `zipcode` | Karachi is in Sindh, Pakistan — never "Karachi, Bavaria, Peru" |
| `person.json` (60) | `first_name`, `gender` | Priya is `female`, Ahmed is `male` |
| `products.json` (48) | `product_name`, `category`, `tier` (+ price band) | "Wireless Earbuds Pro" is Electronics/mid; a `price`-like NUMERIC column in the same table draws from the record's price band |

Columns bind to pools via the existing semantic hints; all matched columns in
a table pick from *one record per row*. Enum columns (`CHECK (col IN (...))`)
are never pool-bound — the DDL's domain wins. If a pool-bound column is
UNIQUE and the requested row count exceeds the pool size, the pool is dropped
for that table (with a warning) and the normal Faker + uniqueness path takes
over. Supply your own pools with `--pools ./my_pools/` (repeatable) — same
JSON shape (`{"name", "match", "records"}`); a user pool named like a
packaged one replaces it wholesale.

**Distribution realism.** FK fan-out defaults to `zipfian` (a few power
users own most orders; `--fanout uniform` opts out); timestamps cluster into
waking hours with a weekend dip and skew quadratically toward the recent
past (a growing app has more new rows than old); `price`-like NUMERIC
columns get log-uniform magnitudes with retail endings (`x.99`/`x.95`/`x.00`),
still clamped inside CHECK bounds and NUMERIC precision; boolean flags skew
by name (`is_active` ~85% true, `is_deleted` ~8% true).

Precedence: constraint-correctness always outranks realism. Columns governed
by a two-column CHECK, an unparsed CHECK, UNIQUE, PK, or FK are left to the
constraint machinery; single-column CHECK bounds clamp any coherence
rewrite; NULL/NOT NULL patterns are preserved.

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
  generate-and-filter retry loop using `simpleeval` against the raw SQL
  condition (bounded at 50 retries per row; on exhaustion the engine raises
  `DomainExhaustedError` rather than emitting invalid rows — very selective
  constraints like `CHECK (a + b < 10)` over wide default ranges will hit
  this; add explicit bounds to the columns to shrink the search space). The
  fallback only applies when every column in the constraint can be safely
  regenerated: unparsed CHECKs that involve a PK, FK, UNIQUE column, or a
  member of a composite UNIQUE/PK group are **not** retried during generation
  (regenerating those would break other guarantees) — they are instead
  re-checked by the validator, which reports any violation. Expressions
  `simpleeval` can't evaluate at all are skipped in generation and surfaced
  as a validator warning rather than silently accepted.
- **Cross-table business rules** (e.g. "sum of `order_items.price` equals
  `orders.total`") aren't expressible in a single-table CHECK and aren't
  handled — SQL itself can't express them, so there's nothing in the DDL to
  parse. Would need a separate rule-config layer.
- **Generated/computed columns and triggers** are not evaluated — the DDL
  parser doesn't currently interpret `GENERATED ALWAYS AS (...)`, and triggers
  are invisible to an offline generator.
- **Semantic realism** for *unrecognized* columns is only as good as the hint
  dictionary in `ddl_parser._SEMANTIC_PATTERNS` / `generators/text.py`:
  columns the hints miss fall back to generic phrase-like text — extend the
  dictionary or supply a custom pool (`--pools`) for domain-specific fields.
  (Chronology — `updated_at >= created_at`, children newer than parents,
  acyclic hierarchies — and city/state/country, name/gender, product/category
  consistency are handled by the coherence layer; see above.)
- **Deferred-FK chronology is approximate**: for cycle-broken FKs the
  backfill filters candidate parents to those whose anchor is not after the
  child's, and falls back to NULL when none qualifies. This is a documented
  approximation, not a by-construction guarantee like the non-deferred case.
- **Non-DDL input** (diagrams, freeform text schema descriptions) is out of
  scope for this package; it only reads SQL DDL.
- **Scale**: uniqueness enforcement uses Python-level retry + set tracking,
  fine to tens of thousands of rows per table; millions of rows would want a
  vectorized (numpy) rewrite of the generator loops.
- The validator is report-only by design: it tells you exactly what and where
  a violation is (table, column, row index, constraint) rather than silently
  patching it, on the view that a generator bug should be visible and fixed at
  the source rather than papered over. It independently re-checks NOT NULL,
  PK/UNIQUE, composite UNIQUE, FK integrity, CHECKs, enum membership, and
  type conformance (declared SQL type vs the Python values produced).

## Repo layout

```
datagen/
  schema_model.py       # dataclasses: the parser/generator contract
  ddl_parser.py          # sqlglot-based DDL -> SchemaModel
  dependency_graph.py    # networkx dependency graph, topo sort, cycle breaking
  generators/
    base.py              # shared bound/retry helpers
    numeric.py            # int/numeric/bool/uuid/sequential-PK, charm prices
    text.py                # semantic-hint-aware string generation
    date_time.py            # date/timestamp windows, hour/weekday/recency shaping
    enum_gen.py               # enum / CHECK IN (...) values
    unique.py                  # generic uniqueness-with-retry wrapper
    fk.py                       # FK sampling from parent pools, fanout control
    linked_group.py               # composite UNIQUE/PK tuples, CHECK-linked columns
    check_eval.py                  # safe fallback evaluator for exotic CHECKs
    pools.py                        # correlated pool groups (engine step 4.5)
  pools/
    geo.json, person.json, products.json   # packaged correlated-value pools
  coherence.py            # chronology/hierarchy post-pass (engine step 7)
  engine.py               # orchestrator, stages 3-5
  validator.py            # independent post-generation constraint re-check
  output/
    csv_writer.py, sql_writer.py, db_loader.py
  cli.py                  # `python -m datagen.cli ...`
tests/
  fixtures/sample.sql, fixtures/hard.sql
  test_engine_end_to_end.py   # end-to-end tests covering every edge case above
  test_fixes.py               # regression tests, one per review-report defect
  test_hardening.py           # adversarial suite over the hard fixture
  test_coherence.py           # chronology, hierarchy, pools, determinism
```

## Configuration knobs (`EngineConfig`)

| Field | Default | Meaning |
|---|---|---|
| `seed` | 42 | RNG seed; same seed = identical output |
| `null_rate` | 0.05 | Fraction of nullable non-FK column values set to `None` |
| `fk_null_rate` | 0.1 | Fraction of nullable FK values set to `None` |
| `deferred_fk_null_rate` | 0.2 | Fraction left null for self-ref/cycle-broken FKs (e.g. employees with no manager) |
| `fanout` | `"zipfian"` | `"zipfian"` (a few parents get most children — realistic default) or `"uniform"` (even fan-out) |
| `pk_start` | `{}` | Per-table starting integer for sequential PKs (no CLI flag) |
| `as_of` | `None` (fixed 2026-01-01 anchor) | Anchor date for default date/timestamp windows; fixed by default so output never depends on the day the command runs |
| `coherence` | `True` | Master switch for the coherence layer (pools, chronology, hierarchy shaping). CLI: `--no-coherence` |
| `root_fraction` | `None` (= `deferred_fk_null_rate`) | Fraction of rows in a self-referencing hierarchy that are roots (NULL parent). CLI: `--root-fraction` |
| `pool_dirs` | `[]` | Extra directories of user-supplied `*.json` pools. CLI: `--pools DIR` (repeatable) |

`seed`, `null_rate`, `fk_null_rate`, `deferred_fk_null_rate`, `fanout`,
`as_of`, `coherence`, `root_fraction`, and `pool_dirs` are all exposed as CLI
flags (`--seed`, `--null-rate`, `--fk-null-rate`, `--deferred-fk-null-rate`,
`--fanout`, `--as-of`, `--no-coherence`, `--root-fraction`, `--pools`).
