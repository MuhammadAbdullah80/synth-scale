# Code Review — `datagen` prototype

**Reviewed:** `A:\ComeBck\datagen\datagen_pkg` (~2,000 LOC, 26 files)
**Date:** 17 Jul 2026 · Cohort 1, Synth-Scale
**Method:** full read of every module, then **19 empirical probes** run against the real engine (sqlglot 30.12, Faker 40.31, networkx 3.6). Every finding below was reproduced, not inferred.

---

## Verdict

**This is a genuinely good prototype and the right architecture.** Deterministic core, no LLM in the data path, sqlglot AST parsing, topological sort with cycle-breaking, construct-to-satisfy for linked columns, and an independent validator pass. Whoever wrote this understood the problem. The self-referential FK deferral and the junction-table handling are correct and non-trivial.

**But it does not yet deliver the one thing we sell.** Our entire pitch is *"correct on the first run — Claude gives you a probably-script, we give you guaranteed-correct data."* I found **6 defects that silently emit data a real Postgres database will reject**, plus **2 that break the reproducibility claim outright**.

The most dangerous finding isn't any single bug — it's this:

> **All 16 tests pass. The test fixture is shaped around exactly the cases that work.**
> `NUMERIC(10,2)` (wide enough to hide the precision bug), no UUID PK, no date CHECK, no composite UNIQUE mixing FK and non-FK columns. Change `NUMERIC(10,2)` to `NUMERIC(4,2)` — a normal thing in a real schema — and the output stops loading. Green CI is currently **measuring the fixture, not the engine.**

Nothing here is architectural. Every issue is a contained fix inside a module that already exists. Estimate: **~2 focused days** to clear all criticals.

---

## Severity summary

| # | Finding | Severity | Probe |
|---|---|---|---|
| 1 | `NUMERIC(p,s)` precision ignored → numeric overflow on load | 🔴 Critical | P2 |
| 2 | Two-column CHECK writes **floats into INT columns** (and clobbers PKs) | 🔴 Critical | P6, P7, P19 |
| 3 | NOT NULL column gets NULLs when derived from a nullable base | 🔴 Critical | P8 |
| 4 | Date/timestamp CHECK bounds: **crashes** on `BETWEEN`, **silently ignored** for `>=` | 🔴 Critical | P3, P4 |
| 5 | Composite UNIQUE mixing FK + non-FK columns is not enforced | 🔴 Critical | P5 |
| 6 | Exotic CHECK fallback documented but **never wired in** → invalid data | 🔴 Critical | P10 |
| 7 | UUID PKs are **not deterministic** — breaks our headline claim | 🟠 High | P1 |
| 8 | `date.today()` → same seed gives different data **on a different day** | 🟠 High | P18 |
| 9 | Validator docstring promises repair; no repair code exists | 🟠 High | read |
| 10 | Enum columns are never independently re-validated | 🟠 High | P14 |
| 11 | `updated_at` lands **before** `created_at` (70% of rows) | 🟡 Realism | P11 |
| 12 | Child rows created **before their parent existed** (75% of rows) | 🟡 Realism | P12 |
| 13 | Manager cycles: A manages B, B manages A | 🟡 Realism | P16 |
| 14 | CSV writes NULL as empty string | 🟡 Medium | P17 |
| 15 | No correlated pools (city↔country independent) | 🟡 Gap | read |
| 16 | Not packaged — no `pyproject.toml`, so no `uvx`/`pip install` | 🟡 Gap | read |
| 17 | Dead code, duplicate work, unexposed knobs | 🔵 Low | read |

---

## 🔴 Critical — produces data Postgres rejects

### 1. `NUMERIC(p,s)` precision is parsed, then ignored

The parser correctly reads `precision=4, scale=2`. The generator throws it away and uses a hardcoded `0.01 → 10_000.0` range.

```python
# generators/numeric.py:20
def generate_numeric(column, n, rng, bounds):
    lo = float(bounds.get("min", 0.01))
    hi = float(bounds.get("max", 10_000.0))   # <-- ignores column.precision
    scale = column.scale if column.scale is not None else 2
    return [round(rng.uniform(lo, hi), scale) for _ in range(n)]
```

**Probe P2** — `rate NUMERIC(4,2)` (max legal value `99.99`):
```
parsed precision=4 scale=2
values=[2379.65, 5442.3, 3699.56, 6039.2, 6257.21, 655.3, 131.69, 8374.69]
values >= 100 (would raise numeric field overflow): ALL 8
```
Every row fails with `numeric field overflow`. The test fixture uses `NUMERIC(10,2)` (max 99,999,999.99) so the ceiling is never hit — that's why CI is green.

**Fix:** clamp `hi` to `10**(precision - scale) - 10**-scale`, intersected with any CHECK bound.

---

### 2. Two-column CHECK writes floats into INT columns — and overwrites primary keys

`derive_dependent_column` adds `rng.uniform()` regardless of the column's declared type.

```python
# generators/linked_group.py:64
sign = 1 if op in (">", ">=") else -1
return [b + sign * rng.uniform(min_delta, max_delta) ...]   # float, always
```

**Probe P7** — `max_qty INT NOT NULL CHECK (max_qty > min_qty)`:
```
(min_qty, max_qty, type) = [(638, 700.4497692724918, 'float'), (262, 337.77…, 'float'), …]
```

**Probe P19** — the emitted SQL:
```sql
INSERT INTO "t" ("id", "min_qty", "max_qty") VALUES
  (1, 638, 705.0190420442049),      -- INT column
  (2, 262, 333.772227335347);
```

Worse — **Probe P6**: engine stage 3 (CHECK groups) runs *before* stage 4 (PK). A PK inside a two-column CHECK gets derived as a float and never becomes a sequential key:

```
CREATE TABLE t (id INT PRIMARY KEY, lo INT NOT NULL, CHECK (id > lo));
(id, lo) = [(700.4497692724918, 638), (337.8944758389217, 262), …]
```
The PK is now a random float. **The validator reports 0 violations** — it checks uniqueness and nullability, never type conformance.

**Fix:** make `derive_dependent_column` type-aware (integer delta for INT, respect scale for NUMERIC, clamp to the column's own bounds), and exclude PK columns from stage-3 derivation.

---

### 3. NOT NULL columns receive NULLs

Stage 3 generates the base column with its null rate, then derives the dependent column — propagating `None` without checking the *dependent* column's nullability.

**Probe P8** — `start_date DATE` (nullable), `end_date DATE NOT NULL`, `CHECK (end_date > start_date)`:
```
NULLs in NOT NULL end_date: 14/40
validator violations = 14
```
To its credit, the validator **caught this one** — it says *"this indicates a generator defect, please report it."* It's reporting a real defect that shipped.

**Fix:** if the dependent column is NOT NULL, either force the base non-null or synthesise a value where the base is NULL.

---

### 4. Date CHECK bounds: crash on `BETWEEN`, silently ignored otherwise

Two separate defects on the same path.

**(a) Hard crash — Probe P3.** `apply_single_column_bounds` calls `float()` on a `BETWEEN` literal:
```python
# generators/base.py:33
bounds["min"] = max(bounds.get("min", float(parsed["low"])), float(parsed["low"]))
```
```
CHECK (d BETWEEN '2020-01-01' AND '2020-12-31')
→ ValueError: could not convert string to float: '2020-01-01'
```
An uncaught crash on ordinary DDL.

**(b) Silent wrong data — Probe P4.** `_structural_parse_check` does `float(right.name)` on `d >= '2030-01-01'`, fails, returns `None` — the constraint vanishes:
```
parsed = [None]
generated = ['2025-03-17', '2026-03-15', '2026-01-25', '2024-11-27', '2025-07-30']
violations_found = 5
```
There's also a dead handshake here: `generate_date` reads `bounds["min_date"]`/`["max_date"]`, but `apply_single_column_bounds` **only ever writes `min`/`max`/`in_list`**. Those keys can never be set — date bounds are structurally unreachable even if parsing succeeded.

**Fix:** type-aware literal coercion in the bounds collapser (date/timestamp/numeric), and align the key names.

---

### 5. Composite UNIQUE with mixed FK + non-FK columns is unenforced

Engine stage 1 only handles composite groups where **every** column is an FK (`all(c in fk_by_col for c in group)`). A `UNIQUE (user_id, slug)` — FK + plain column, extremely common — matches nothing and each column is generated independently.

**Probe P5** — 2 users, 40 posts, `UNIQUE (user_id, slug)`:
```
duplicate (user_id, slug) pairs = 2
validator violations = 2
posts composite unique ['user_id','slug']: duplicate (1, 'De-enginee') at rows 27 and 28
```

**Fix:** generalise stage 1 — any composite UNIQUE/PK group generates as tuples, with FK members drawing from parent pools and non-FK members from their own generator.

---

### 6. The exotic-CHECK fallback is documented but not connected

README §"Known limitations" says arbitrary CHECKs *"fall back to a generate-and-filter loop using `simpleeval` against the raw SQL condition."*

**Probe P10:**
```
check_eval referenced in engine: False
parsed (exotic check) = [None]
(a, b, satisfies a+b<10) = [(638, 945, False), (262, 543, False), (760, 30, False), …]
validator violations = 10   ← every row
```
`check_eval.py` is imported **only by the validator**. Generation never filters. So `CHECK (a + b < 10)` produces 100% invalid rows. The module exists, works, and is wired to the wrong end of the pipeline.

**Fix:** either wire it into generation as documented, or correct the README. Wiring is better — it's ~20 lines at the stage-5 call site.

---

## 🟠 High — breaks our stated guarantees

### 7. UUID primary keys are not deterministic

```python
# generators/numeric.py:33
def generate_uuid(n):
    return [str(uuid.uuid4()) for _ in range(n)]   # never seeded
```

**Probe P1** — same seed, two runs:
```
run1 ids = ['1e089448-a208-…', '247f4b5c-…', 'c07a01c7-…']
run2 ids = ['aa6cd820-7c4a-…', '1c971ce2-…', 'ea699c4b-…']
IDENTICAL = False
```
README: *"the same seed always produces byte-identical output."* Not for UUID PKs — and **Supabase defaults to UUID PKs**, so this hits our primary target ecosystem on the exact claim (reproducibility) we lead with.

**Fix:** `uuid.UUID(int=rng.getrandbits(128), version=4)`.

### 8. Same seed, different day → different data

`generate_date` anchors to `date.today()`.

**Probe P18** (patched clock to simulate a later run):
```
today:      ['2026-04-15', '2025-04-04', '2025-07-19']
as-if 2030: ['2029-09-30', '2028-09-19', '2029-01-03']
IDENTICAL = False
```
Reproducibility is our #1 objection-handling answer. "Same seed, same data — *unless you run it tomorrow*" doesn't survive a demo question.

**Fix:** derive the window from the seed or an explicit `--as-of` date; never wall-clock.

### 9. The validator promises repair it doesn't do

Module docstring: *"and repair any violation found… it repairs the specific cell(s) rather than silently accepting bad data."* There is no repair code. `violations_repaired` is always `0`; `MAX_REPAIR_RETRIES` and the `rng` parameter are unused. The README is honest ("report-only") — the docstring isn't. Pick one; report-only is the right behaviour.

### 10. Enum membership is never independently re-checked

`CHECK (status IN (…))` is converted into `column.enum_values` at parse time and **removed from `check_constraints`** (P14: `check_constraints kept for validator = 0`). The validator only re-checks `check_constraints`, so nothing independently verifies enum output. The safety net has a hole exactly where the generator is trusted.

---

## 🟡 Realism — where our differentiator is supposed to live

Our Build Spec calls the coherence layer *"the differentiator — most of your time here."* It isn't built yet. These are not constraint violations; they're the tells that make data look fake.

- **P11 — `updated_at` before `created_at` in 7/10 rows.** The parser has `created_at`/`updated_at` semantic hints; the timestamp generator ignores them entirely.
- **P12 — 15/20 orders created before their user existed.** No cross-table chronology. This is the single most visible "fake data" tell in a demo, and it's exactly what we told Mahad we'd beat Claude on.
- **P16 — management cycles.** Across 10 seeds at 200 employees: 6 distinct cycles (e.g. seed 1: employee 24 manages 75, 75 manages 24; seed 3: a 7-person loop). Real org charts are trees. Fix: assign managers only from lower-indexed rows.
- **No correlated pools.** `city`, `state`, `country` are drawn independently — "Karachi, Bavaria, Peru." This is precisely Mahad's curated-pool idea, and it's the right place for it.
- **P17 — CSV NULLs.** Written as empty fields; Postgres `COPY` reads those as empty strings for text columns and errors on INT columns unless `NULL ''` is set. Emit `\N` or document the required `COPY` options.

---

## 🔵 Lower priority

- **Not packaged.** No `pyproject.toml` — only `requirements.txt`. Today it's `python -m datagen.cli`, not `uvx synth-scale`. Our whole acquisition story is the 30-second one-liner. Phase 4 work, but don't forget it.
- **Naming:** package is `datagen`; product is Synth-Scale. Decide before PyPI (the name matters for squatting).
- **Dead code:** unused `idx` (`generators/__init__.py:53`), `MAX_REPAIR_RETRIES`, validator's `rng` param.
- **Duplicate work:** `cli.py:91` rebuilds the generation plan the engine already computed (double parse cost on large schemas).
- **Unexposed knobs:** `deferred_fk_null_rate` and `pk_start` exist in `EngineConfig` but have no CLI flags.
- **`fake.unique.email()`** is used for *every* email column, unique or not — realistic schemas allow duplicate emails in some tables, and Faker's global unique registry grows unboundedly across tables.
- **`TIMESTAMPTZ`** collapses to a naive `datetime` — no tz on output.
- **`--rows` requires an entry per table.** Fine at 6 tables; painful at 15. Consider a default multiplier.

---

## What I'd do, in order

**Day 1 — stop emitting invalid data**
1. Type-aware `derive_dependent_column` + exclude PKs from stage 3 (#2)
2. Clamp NUMERIC to precision/scale (#1)
3. NOT NULL guard on derived columns (#3)
4. Type-aware bounds coercion for dates + fix the `min_date`/`max_date` key mismatch (#4)
5. Generalise composite UNIQUE to mixed FK/non-FK groups (#5)
6. Wire `check_eval` into generation, or fix the README (#6)

**Day 2 — make the guarantees true**
7. Seeded UUIDs (#7) · 8. Remove wall-clock dependence (#8) · 9. Enum re-validation (#10) · 10. Fix the validator docstring (#9)

**Then — build the differentiator**
11. Chronology: `updated_at ≥ created_at`, `child.created_at ≥ parent.created_at` (#11, #12)
12. Correlated pools — Mahad's idea, exactly the right place for it (#15)
13. Manager assignment as a tree (#13)

**And immediately, in parallel — fix the tests**

This is the highest-leverage item on the page. The suite is 16 green tests that prove the fixture works. Make it prove the *engine* works:

- Add to the fixture the cases that currently break: `NUMERIC(4,2)`, a `UUID PRIMARY KEY`, a date `CHECK`, `UNIQUE (fk_col, plain_col)`, an INT two-column CHECK, a NOT NULL dependent column.
- **Add a type-conformance check to the validator** — nothing currently verifies that an INT column contains ints. That single check catches #1, #2 and #7's fallout automatically.
- **Property test:** for N random seeds, assert zero violations *and* byte-identical repeat runs.
- **The real bar:** spin up Postgres in CI (docker service), run the DDL, load the output. Right now "correct" means "our validator is happy." It should mean **"Postgres accepted it."** That's the claim we sell, so that's the test we need. Every critical above except #6 would have been caught by one `psql` load.

---

## For the squad

Tell Mahad his architecture is sound — the deterministic engine, the topological sort, the construct-to-satisfy design, and the separate validator are all right, and the cycle-breaking is genuinely well done. Nothing here questions the design. Every defect is a leaf-level fix in a module that already exists.

But be blunt about the meta-lesson: **the tests passing told us nothing we wanted to know.** A green suite over a fixture that dodges every hard case is worse than no suite, because it buys false confidence in exactly the claim — "correct on the first run" — that we've built the entire pitch on. Demo Day is a live `psql` load in front of an audience. Until CI does that load, we don't actually know whether it works; we only know the fixture does.

Fix the six criticals, then make the CI load into real Postgres. After that, the coherence layer is what turns "technically valid" into "looks real" — and that's the part Claude genuinely can't do in one shot.

---

*Evidence: `probe_findings.py` (this folder) reproduces every finding above. Run it from `datagen_pkg/` with the deps in `requirements.txt` installed.*
