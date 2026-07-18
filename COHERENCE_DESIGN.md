# Coherence Layer — Design

**Scope:** make generated data *look like a real app*: timestamps that respect cause-and-effect, org charts that are trees, cities that match their countries, distributions that aren't a flat grid.
**Fixes review defects:** P11 (`updated_at` < `created_at`), P12 (child rows older than parent), P16 (manager cycles), "no correlated pools" gap.
**Hard constraints:** deterministic (seeded `random.Random` only, no wall clock — coordinate with fix #8), no scipy/numpy, bolts onto the existing pipeline in `A:\comebck\datagen\datagen_pkg\datagen\`.

## Where it hooks into the pipeline

The pipeline is: **1** parse (`ddl_parser.py`) → **2** plan (`dependency_graph.py`) → **3** generate (`engine.py`, per-table steps 1–5) → **4** validate (`validator.py`) → **5** output. Coherence adds:

```
engine.generate_table()                         engine.run_engine()
  steps 1-5 (unchanged)                           per-table loop (unchanged)
  + step 6: apply_coherence(table, row_data,      + ctx: dict[str, TableContext]
            n, rng, ctx, config)                    threaded through calls
  + capture TableContext (anchor timestamps)     _backfill_deferred_fks()
                                                   + earlier-rows-only sampling (§c)
validator.validate()
  + coherence checks (warn-level): updated>=created, child>=parent, self-FK acyclic
```

**Key decision — post-pass rewrite, not in-line generation.** Steps 1–5 run exactly as today; a new module `datagen/coherence.py` then *rewrites* the timestamp columns it owns (using the same seeded `rng`, so output stays deterministic). One new call site in `engine.py`, one new module, no reordering of existing steps. This also minimizes merge conflicts with the concurrent bug-fix work: all detection logic lives in `coherence.py` and reads only the `Table`/`Column` dataclasses — **`ddl_parser.py` is not touched.**

```python
# datagen/coherence.py — public surface
@dataclass
class TableContext:
    anchor_col: str | None            # e.g. "created_at"
    anchor: list                      # per-row anchor timestamp (index-aligned with rows)
    fk_parent_idx: dict[str, list]    # fk column -> per-row parent ROW INDEX (or None)

def detect_plan(table: Table, config_overrides: dict) -> TableCoherencePlan: ...
def apply_coherence(table, row_data, n, rng, ctx, config) -> None: ...   # mutates row_data
```

---

## a. Cross-column chronology (within one row)

### Rules detected (heuristics, all on lowercased column names)

| Rule | Detection | Behaviour |
|---|---|---|
| **Anchor** | first match in priority order: `created_at`, `inserted_at`, `^(order\|purchase\|signup\|placed\|opened\|start)_?(date\|at\|time)$` | The row's "birth" timestamp. Everything else is derived from it. |
| **updated_at** | `updated_?at\|modified_?at` (hint already exists) | `anchor + U(0, min(90d, as_of - anchor))`; 30% of rows get `updated = anchor` exactly (never-touched rows — real apps have them). |
| **Lifecycle** | `(shipped\|delivered\|cancelled\|completed\|paid\|refunded\|approved\|published\|archived\|closed\|resolved\|confirmed\|deleted)_?(at\|date\|on)` | Ordered chain after anchor, each stage `prev + U(2h, 96h)`. |
| **last_*** | `last_(login\|seen\|active\|used)_?(at)?` | `U(anchor, as_of)`. |
| **Status gate** | enum column named `status\|state\|.*_status` whose values stem-match lifecycle columns (`shipped` ↔ `shipped_at`, suffixes `_at/_date/_on` stripped, `ed→` stem fallback) | Row's status decides which lifecycle cells are filled: stages **≤** status get timestamps, later stages get `NULL` (only if nullable — if NOT NULL, fill anyway and let the chain order hold). Known chains, in order: `pending→processing→paid→shipped→delivered`, `draft→published→archived`, `open→in_progress→resolved→closed`; `cancelled/refunded` = terminal branch, gets its own timestamp, later stages NULL. |

All offsets come from `rng` (deterministic). `as_of` is the config anchor date (**never** `date.today()` — same fix as review #8; default: derive from seed epoch or `--as-of` flag).

Interplay guard: if a timestamp column is already governed by a *parsed CHECK* (`col_compare`, handled in engine step 3), coherence leaves it alone — CHECK-correctness outranks realism.

### Config overrides (`synthscale.toml`, loaded via stdlib `tomllib`, lands in `EngineConfig.coherence: dict`)

```toml
[coherence]
enabled = true              # master switch; false = today's behaviour
as_of   = 2026-07-17        # replaces wall clock everywhere

[coherence.tables.orders]
anchor   = "placed_at"                                  # override detection
sequence = ["placed_at", "paid_at", "shipped_at"]       # explicit chain order
status   = "status"

[coherence.tables.orders.offsets]
shipped_at = { min_hours = 4, max_hours = 72 }

[coherence.tables.orders.status_gates]                  # value -> furthest non-null stage
shipped = "shipped_at"
```

### Worked example

```sql
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  status TEXT CHECK (status IN ('pending','shipped','delivered')),
  created_at TIMESTAMP NOT NULL,
  shipped_at TIMESTAMP,
  updated_at TIMESTAMP
);
```
Before (today): `('delivered', 2025-11-02 03:14, 2024-06-30 22:51, 2024-03-12 09:00)` — shipped before created, updated before created, 3 a.m. everything.
After: `('delivered', 2026-03-04 14:22, 2026-03-06 09:41, 2026-03-06 09:41)` and a `'pending'` row gets `shipped_at = NULL`.

---

## b. Cross-table chronology (child ≥ parent)

### Mechanism: FK sampling returns row *indices*; anchors become per-row floors

Today `engine._ref_pool_scalar` flattens parent rows to a value list, losing the parent row. Change: sample **indices** into the parent's row list, then map to values.

1. **`generators/fk.py`**: add `generate_fk_indices(n, pool_size, rng, null_rate, fanout) -> list[int | None]` (zipfian weights move here unchanged). `generate_fk_column` becomes a thin wrapper: indices → values. Engine steps 1 (composite FK tuples) and 2 (single/multi-col FK) record picks in `fk_parent_idx[col]`.
2. **Floor computation** (in `apply_coherence`, runs step 6): for row *i*,
   `floor[i] = max(ctx[fk.ref_table].anchor[idx] for each FK pick idx that is not None and whose parent table has an anchor)` — **multi-FK rows take the max of all floors** (an `order_item` is after both its order and its product listing). No usable floor → `None`.
3. **Anchor rewrite**: `anchor[i] = floor[i] + skewed_delta(rng)` when `floor[i]` is set, else default window ending at `as_of` (delta capped so we never exceed `as_of`; if `floor[i] >= as_of`, clamp to `floor[i] + U(1min, 1h)` — accept slight future drift over violating causality). Then §a re-derives updated/lifecycle columns *from the corrected anchor*, so cross-column and cross-table compose automatically.
4. **Capture**: `ctx[table.name] = TableContext(anchor_col, anchor_values, fk_parent_idx)` after step 6, so this table is ready to be a parent.

### Edge cases

| Case | Handling |
|---|---|
| Nullable FK, pick is NULL | contributes no floor (row may be older than any would-be parent — correct, there is no parent) |
| Parent table has no anchor column | contributes no floor |
| Multi-FK row | `max()` of available floors |
| Deferred / cycle-broken FK (backfilled after all tables exist) | floor can't be applied at generation time. Instead constrain the **backfill**: candidate parent pool = rows with `parent.anchor <= child.anchor` (plus §c's earlier-index rule for self-refs). Empty candidate set → NULL if the FK is nullable, else unrestricted pick + validator warning. Documented approximation, not a guarantee — fine for MVP. |
| Table with rows but 0-row parent | already a hard error in `generate_fk_column` — unchanged |

### Worked example

```sql
CREATE TABLE users  (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL);
CREATE TABLE orders (id SERIAL PRIMARY KEY,
                     user_id INT NOT NULL REFERENCES users(id),
                     created_at TIMESTAMP NOT NULL);
```
Before: user 3 created `2026-05-20`, their order created `2024-11-02` (P12: 75% of rows).
After: user 3 created `2025-08-14 10:05` → all of user 3's orders in `(2025-08-14, as_of]`, e.g. `2025-09-02 19:44`, `2026-04-11 12:03`. Violations: 0 by construction (non-deferred FKs).

---

## c. Hierarchy realism (self-referencing FKs)

**Change is confined to `engine._backfill_deferred_fks`** (self-ref FKs are always deferred by `dependency_graph.py`, so this is the single choke point).

- For a same-table deferred FK, row *i*'s candidate parents = **rows with index `< i` only** (generation order = PK order). Parent index strictly less ⇒ cycles are *impossible by construction* — the structure is a forest. Replaces the current "avoid pointing at yourself, 10 attempts" loop.
- **Roots**: row 0 is always a root (`NULL` parent); additionally each row is a root with probability `EngineConfig.root_fraction` (new field, default = reuse `deferred_fk_null_rate = 0.2`). Expose as `--root-fraction`.
- **Parent pick among earlier rows**: uniform over `[0, i)` gives realistic-enough shallow-ish trees for free. (Optional knob later: bias toward mid-range indices for bushier trees — not MVP.)
- **NOT NULL self-FK**: stays a hard error (roots need NULL) — `dependency_graph.py` already raises exactly this; no change.
- **Generalizes as-is**: `categories.parent_id`, `comments.reply_to_id`, `employees.manager_id` all flow through the same code path — nothing category-specific needed.
- **Chronology tie-in (cheap win)**: for a table with a self-ref FK and an anchor, sort the anchor column ascending before assignment (row order = time order, which also matches how serial PKs behave in real apps). Then "parent has an earlier index" *implies* `manager.created_at <= report.created_at`. Only safe when the table has no cross-table floors of its own (e.g. `employees`, `categories` — typically true); skip the sort otherwise.

Worked example — `employees(id, name, manager_id REFERENCES employees(id))`, 8 rows, seed 42:
Before (P16): `24→75, 75→24` style cycles, ~6 per 10 seeds at n=200.
After: `manager_id = [NULL, 1, 1, NULL, 2, 3, 4, 3]` — two trees, max depth 3, zero cycles at any seed/size.

---

## d. Correlated pools

### Pool file format — JSON, packaged under `datagen/pools/`, loaded via `importlib.resources`

```json
{
  "name": "geo",
  "match": ["city", "state", "country", "zipcode"],
  "records": [
    {"city": "Austin",  "state": "Texas",   "country": "United States", "zipcode": "78701"},
    {"city": "Karachi", "state": "Sindh",   "country": "Pakistan",      "zipcode": "74000"},
    {"city": "Munich",  "state": "Bavaria", "country": "Germany",       "zipcode": "80331"}
  ]
}
```
`match` values are **existing semantic hints** (from `ddl_parser._SEMANTIC_PATTERNS`) — the binding is hint-based, so the parser needs no changes for geo/person. Fields a record omits fall back to Faker.

### Shipped pools (keep tiny — target < 25 KB total packaged)

| File | Records | Fields | Contents spec |
|---|---|---|---|
| `geo.json` | 50 | city, state, country, zipcode | 20 US (top metros, real state + one real zip each), 30 international: 4 each PK/IN/UK/DE/CA/AU, 6 across BR/JP/FR/NG/AE/SG — each with its real admin region + plausible postal code |
| `person.json` | 60 | first_name, gender | 30 `female`, 28 `male`, 2 `nonbinary`; common EN + PK/IN/ES names (Ayesha, Fatima, Priya, Sofia… / Ahmed, Bilal, Raj, Diego…) |
| `products.json` | 48 | product_name, category, tier, price_min, price_max | 8 categories × 6 products. Categories: Electronics, Clothing, Home & Kitchen, Sports, Books, Beauty, Toys, Grocery. Tiers: `budget` (3–29), `mid` (29–199), `premium` (199–2499); each record's `[price_min, price_max]` sits inside its tier band (e.g. `{"product_name": "Wireless Earbuds Pro", "category": "Electronics", "tier": "mid", "price_min": 49, "price_max": 129}`) |

New hint patterns needed only for products/person extras — add to `_SEMANTIC_PATTERNS` **after** the concurrent bug-fix branch merges (one-line entries): `gender|sex → gender`, `product_?name|item_?name → product_name`, `category → category`, `tier|plan|grade → tier`.

### Lookup flow

New module `datagen/generators/pools.py` + one insertion in `engine.generate_table` as **step 4.5** (after PK, before independents):

1. `detect_pool_groups(table, pools)`: columns not yet generated whose hints appear in one pool's `match`, grouped per pool. **≥ 2 matched columns → correlated group**; a singleton (lone `city`) still draws from the pool's field instead of bare Faker (keeps style consistent, costs nothing).
2. For each group: `record_idx[i] = rng.randrange(len(records))` per row, then every matched column *j* takes `records[record_idx[i]][field_j]`. Mark columns generated. Store `record_idx` on `TableContext` (enables the stretch below).
3. Price coupling: a `money`-hinted NUMERIC column in the same table as a products group draws `U(price_min, price_max)` from the row's record, then charm-priced (§e), then clamped to the column's NUMERIC precision (respect the concurrent #1 fix).
4. Constraint safety: if any matched column `is_unique` and `n > len(records)`, **drop the group for that table** and fall back to Faker + `generate_unique` (warn once) — never let a 50-record pool exhaust into `DomainExhaustedError`.

### User-supplied pools

`--pools ./my_pools/` (repeatable) or `[pools] extra = ["./my_pools"]`. Same JSON shape; a user pool whose `name` matches a packaged pool replaces it wholesale. Explicit column binding for names the hints miss:

```toml
[pools.bind.warehouses]      # table
location_city = "geo.city"   # column = pool.field
location_ctry = "geo.country"
```

**Stretch (only if lane is green, effort L):** cross-table pool linkage — `products.category_id → categories`, where `categories.name` came from the pool: use `fk_parent_idx` (§b) to fetch the parent's category string and restrict product record choice to matching records. Design supports it (context already carries both `record_idx` and `fk_parent_idx`); do not start it before a–e are done.

### Worked example
Before: `("Karachi", "Bavaria", "Peru")`, `("Priya", "male")`, `("Ergonomic Wooden Chair", "Electronics", 7343.02)`.
After: `("Karachi", "Sindh", "Pakistan")`, `("Priya", "female")`, `("Wireless Earbuds Pro", "Electronics", 89.99)`.

---

## e. Distribution realism (cheap wins only)

| Win | Where | Design |
|---|---|---|
| **Skewed fan-out** | `generators/fk.py` (exists) | Verdict on current zipfian: correct and deterministic (shuffle assigns which parents are "power users", `1/(i+1)` weights, `rng.choices`). Two changes: (1) add exponent `s` (`weights = 1/(i+1)**s`), default `0.7` — pure `1/rank` is harsher than real order data; (2) flip the **default** `EngineConfig.fanout` to `"zipfian"` — uniform fan-out is itself a fake-data tell. Keep `--fanout uniform` as the opt-out. |
| **Business-hours clustering** | `generators/date_time.py` | Replace `rng.randint(0,23)` with `rng.choices(range(24), weights=HOUR_WEIGHTS)[0]`; `HOUR_WEIGHTS` peaks 9–21, trough 1–6 (consumer profile — one profile only, no config). Weekday dip: if sampled date is Sat/Sun, re-roll it once with 60% probability (~gives the weekend dip without date arithmetic). |
| **Recent-past growth curve** | `generators/date_time.py` | `days_ago = int(span * rng.random()**2)` — quadratic mass toward "recent", mimics a growing app. Applies to anchor sampling when there's no floor; floored rows already skew recent via §b's delta. |
| **Charm prices** | `generators/numeric.py` | For `money`-hinted NUMERIC: draw log-uniform (`exp(U(ln lo, ln hi))` — `math` only), then ending mix: 70% `x.99`, 15% `x.95`, 15% `x.00`, via `math.floor(v) + ending`. Clamp to precision/scale (post-fix #1). Non-money numerics untouched. |
| **Boolean skew** | `generators/numeric.py` | `is_active/is_verified/is_enabled` → 85% True; `is_deleted/is_banned/is_archived` → 8% True; other booleans stay 50/50. Pure name regex in the generator, ~10 lines. |

No scipy anywhere: `rng.choices` weights, `**2`, `math.exp/log` cover all of it.

---

## f. Effort, files, risk, and the 2-person / 2-week lane

| Item | Effort | Files touched | Risk |
|---|---|---|---|
| c. Hierarchy | **S** | `engine.py` (`_backfill_deferred_fks`) | ~none; most isolated change |
| e. Distributions | **S** (×4 small) | `fk.py`, `date_time.py`, `numeric.py` | conflicts with concurrent bug-fix branch → land after it merges |
| a. Cross-column | **M** | `coherence.py` (new), `engine.py` (+1 call), `cli.py`/`EngineConfig` (config) | hint false positives (e.g. a `state` column that means status — enum detection already wins that dispatch, but audit); CHECK-vs-coherence overlap guard must hold |
| b. Cross-table | **M** | `coherence.py`, `engine.py`, `fk.py` (index sampling) | changes rng draw order → old seeds produce different bytes (fine pre-1.0, say so in changelog); deferred-FK floors are approximate |
| d. Pools (in-table) | **M** | `pools/*.json` (new), `generators/pools.py` (new), `engine.py` (step 4.5), `ddl_parser.py` (4 hint lines) | UNIQUE × small pool (mitigated §d.4); pool JSON authoring time is real — timebox to half a day |
| d-stretch. Cross-table pools | **L** | `engine.py`, `pools.py` | scope trap — explicitly gated |
| Validator coherence checks | **S** | `validator.py` | none; this is what proves the layer on demo day |

**Priority order: c → e → a → b → d → validator checks → (d-stretch only if green).** Rationale: c and e are day-one wins with near-zero risk; a and b are the headline fixes (P11/P12) and b depends on a's anchor detection; d is independent and parallelizable.

**Two-week split (Dev A = chronology, Dev B = pools/distributions):**

| | Dev A | Dev B |
|---|---|---|
| W1 D1–2 | §a: `coherence.py` skeleton, anchor/lifecycle detection, updated_at + status gates | §c hierarchy fix (D1); §e all four wins (D2) |
| W1 D3–5 | §b: index-based FK sampling, `TableContext`, floors, anchor rewrite | §d: author 3 pool JSONs (timeboxed), `pools.py`, step 4.5 wiring |
| W2 D1–2 | §b edges: nullable/multi-FK/deferred backfill filtering; config file loading (`tomllib`) | §d: custom `--pools`, bind overrides, UNIQUE fallback |
| W2 D3 | Validator coherence checks (both — pairing point) | — |
| W2 D4–5 | Buffer: run the 15-table demo schema end-to-end, fix what looks fake | Buffer + docs for config/pool format |

**Coordination rule:** the bug-fix branch (criticals #1–#10) merges **first**; coherence branches from it. New-module-first design (`coherence.py`, `pools.py`) keeps the overlap to ~5 call-site lines in `engine.py`.

**Definition of done** (matches Build Spec's "zero chronological anomalies"): on the reference schema at 3 seeds — 0 rows with `updated_at < created_at`; 0 non-deferred child rows older than their parent; 0 self-FK cycles; every city/state/country row internally consistent; validator reports all four as explicit PASS lines (that's the demo screenshot).
