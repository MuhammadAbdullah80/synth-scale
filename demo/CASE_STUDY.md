# Synth-Scale — Comebck Cohort 1 Case Study

> **Status: SKELETON WITH REAL STORY FILLED IN.** Everything marked
> `!!! TODO(squad)` is a metric slot the squad must fill with real numbers
> before submission. **Nothing in a TODO slot may be invented.** Everything
> not marked TODO is sourced from the repo's own documents
> (`REVIEW_REPORT.md`, `SNAPLET_REPORT.md`, `IDEAS.md`, `datagen_pkg/README.md`)
> or measured on a real machine (`demo/BENCHMARK.md`).

---

## 1. Problem

Every vibe-coded app ships with an empty database. To demo, test, or develop
against realistic data, developers today ask Claude/Cursor to write a seed
script — and get a *probably*-correct script: FKs that dangle, `NUMERIC(6,2)`
overflows, `updated_at` before `created_at`, different data every run, and a
token wall long before 15 tables x 10,000 rows. The alternatives are a
column-grid web tool with no FK topology (Mockaroo), a hand-rolled
`seed.sql` + Faker script that rots with every migration, or a dead product
(Snaplet — see §4).

Our one-line pitch: **"Claude writes you a probably-correct seed script; we
give you guaranteed-correct data — first run, every run."**

## 2. Validation plan

- Target user: Postgres/Supabase developers who already tried to seed a
  database and hit either an LLM's limits or Snaplet's grave.
- Where they congregate (from `SNAPLET_REPORT.md` §3): the
  `supabase-community/seed` issue tracker, `supabase/supabase` issue #29890
  ("recommend a maintained third-party solution" — a literal request for this
  product), the Supabase docs seeding page, Supabase Discord, r/Supabase.
- Interview scripts: see the interview scripts kit.
  `!!! TODO(squad): link the actual interview-scripts doc here.`
- `!!! TODO(squad): interviews completed: ___ (count, dates, 2-3 verbatim quotes)`
- `!!! TODO(squad): top 3 validated pains from interviews: ___`

## 3. What we built

**Architecture (real, shipped):** a deterministic core with no LLM in the
data path. A 6-stage engine — (1) sqlglot DDL parsing into a schema model
with per-column semantic hints, (2) networkx dependency graph with
topological sort and FK-cycle breaking, (3) constraint-first generation in
dependency order (junction-table composite keys first, then FK sampling from
parent pools, then two-column CHECK pairs *constructed to satisfy* rather
than retried, then PKs, correlated pools, and everything else), (4) a
bounded generate-and-filter fallback for exotic CHECKs, (5) an independent
validator that re-checks every constraint plus type conformance against the
full dataset, (6) FK-safe output as CSV / batched SQL / direct DB load — plus
a **coherence post-pass**: cross-row and cross-table chronology
(`updated_at >= created_at`, children never older than parents),
self-referencing FKs backfilled as guaranteed-acyclic forests, correlated
pools (Karachi is in Pakistan; Priya is female; "Wireless Earbuds Pro" is
Electronics/mid with a mid-tier price), zipfian FK fan-out, charm prices
(`x.99`), and status-gated lifecycle timestamps (a `pending` order has no
`shipped_at`). Everything runs off one seeded RNG: same seed = byte-identical
output, any machine, any day.

**The honest testing story (from `REVIEW_REPORT.md`):** the first review of
the prototype found that *all 16 tests passed and it didn't matter* — "green
CI was measuring the fixture, not the engine." The fixture dodged every hard
case; 19 empirical probes against the real engine found **6 critical defects
that silently emitted data Postgres would reject** (NUMERIC precision
ignored, floats written into INT columns, NULLs in NOT NULL columns, date
CHECKs crashing or ignored, mixed composite UNIQUE unenforced, the exotic
CHECK fallback never wired in) **plus 2 that broke the determinism claim
outright** (unseeded UUIDs, wall-clock-dependent dates). The response was an
**adversarial suite**: a hard fixture containing every case the old one
avoided, with tests written against *correct* behaviour — deliberately red
against the unfixed engine, no xfail markers — then the engine was fixed
until they went green. Today the suite is **129 passing tests** (8 more run
only against a live Postgres in CI), including property tests asserting zero
violations and byte-identical repeat runs across random seeds.

**Shipped surface:** `synth-scale` CLI (Typer + Rich) with `--preview`,
`--seed`, `--rows` (per-table or single-integer with automatic FK fan-out),
`--format csv|sql|db`, `--as-of`, `--pools`, live-DB introspection via
`--from-db`. Measured on a laptop: 98,000 rows across a 15-table SaaS schema
in ~14 seconds, zero validator violations (`demo/BENCHMARK.md`).

## 4. Market (Snaplet TL;DR — receipts in `SNAPLET_REPORT.md`)

- **The incumbent is dead and demand grew anyway.** Snaplet shut down Aug
  2024 ("we have not reached the necessary adoption levels"); its abandoned
  `@snaplet/seed` package is still downloaded ~140k times/month — 3.4x more
  than 18 months ago — with open bugs nobody will fix. Supabase's own docs
  call the community fork "an optional convenience"; their GitHub issues ask
  for "a maintained third-party solution."
- **The failure mode was theirs, not the market's.** Snaplet needed a cloud
  (S3 + Fargate + Neon COGS), a ~10-person Berlin team, and $30/team pricing;
  its top user-facing bugs were exactly *invalid generated data* (unique /
  identity / generated-column failures). Synth-Scale runs on the user's
  machine, costs ~nothing to serve, and makes those bugs impossible by
  construction.
- **The vacuum is contested but unfilled:** Neosync was acquired and archived
  (Aug 2025); new entrant Seedfast openly concedes it dropped determinism and
  the typed client — the two things we lead with. Honest bear case: nobody
  has yet built a big business here; our bar (4 people, near-zero COGS) is a
  fraction of the bar Snaplet missed.

## 5. Metrics

`!!! TODO(squad): DO NOT SUBMIT WITH THESE EMPTY — and do not invent numbers.`

| Metric | Value | Source / proof |
|---|---|---|
| User interviews done | `!!! TODO(squad)` | `!!! TODO(squad)` |
| Waitlist signups | `!!! TODO(squad)` | `!!! TODO(squad)` |
| CLI installs / active users | `!!! TODO(squad)` | `!!! TODO(squad)` |
| Weekly retained seeding runs (the metric that matters — see SNAPLET_REPORT §5b: "don't confuse launch applause with adoption") | `!!! TODO(squad)` | `!!! TODO(squad)` |
| User quotes (2–3, verbatim, attributed) | `!!! TODO(squad)` | `!!! TODO(squad)` |
| Measured performance | 9,800 rows / 2.8 s · 98,000 rows / 14.4 s · 490,000 rows, 15 tables, 0 violations | `demo/BENCHMARK.md` (measured, this repo) |
| Test suite | 129 passed (+8 live-Postgres tests, CI-gated) | `datagen_pkg/tests/`, run 2026-07-19 |

## 6. Lessons

1. **Green tests can lie.** A suite shaped around what works is worse than no
   suite — it buys false confidence in the exact claim the product sells.
   Write tests against correct behaviour and let them fail first.
2. **Correct-by-construction beats validate-and-retry.** Every category of
   bug that killed Snaplet's users' trust (invalid uniques, broken
   constraints) is structurally impossible when the generator constructs
   values to satisfy constraints instead of hoping.
3. **Determinism is a feature you can't bolt on.** Two of our own review
   findings (unseeded UUIDs, wall-clock dates) silently broke the headline
   claim; both had to be fixed at the RNG level, not patched.
4. **A dead competitor is evidence, not a strategy.** Snaplet's corpse proves
   demand exists at *some* price and cost structure — it does not prove ours.
   The kill criterion is stated up front: if weekly retained runs don't
   materialize within a quarter of launch, the bear case was right.
5. `!!! TODO(squad): one lesson from the validation interviews, in the
   interviewees' own words.`
