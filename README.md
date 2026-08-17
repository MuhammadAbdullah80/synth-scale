# synth-scale

**Paste your schema. Get realistic, perfectly-linked synthetic data.**
Deterministic, referentially-consistent test data for Postgres and Supabase
— every foreign key resolves, every `CHECK` passes, every run with the same
seed is byte-identical. No LLM in the data path.

[![CI](https://github.com/MuhammadAbdullah80/synth-scale/actions/workflows/ci.yml/badge.svg)](https://github.com/MuhammadAbdullah80/synth-scale/actions/workflows/ci.yml)

**[Try it in your browser, no install →](https://synth-scale-ashen.vercel.app/)**

An LLM can write you a *probably*-correct seed script. Synth-scale parses
your DDL (or introspects a live database), resolves the foreign-key
dependency order, and generates rows table-by-table so every constraint is
satisfied **by construction** — then an independent validator re-checks
every one. 50,000 rows behave exactly like 50.

## Three ways in

| | What it is | Start here |
|---|---|---|
| **CLI** | The real product. Parses migrations, introspects live Postgres/Supabase schemas, loads data straight into your database — no caps, fully offline, deterministic. | [`datagen_pkg/README.md`](datagen_pkg/README.md) |
| **Web playground** | Paste DDL or connect a Supabase database in the browser — capped, for people who won't install a CLI. | **[synth-scale-ashen.vercel.app](https://synth-scale-ashen.vercel.app/)** ([source](web/README.md)) |
| **Supabase quickstart** | Five minutes: connection string → preview → load, including the pooler-vs-direct-connection gotcha. | [`datagen_pkg/docs/supabase.md`](datagen_pkg/docs/supabase.md) |

```bash
pip install synth-scale   # not yet on PyPI: pip install -e ./datagen_pkg
synth-scale --ddl schema.sql --rows 1000 --seed 42 --format sql --out ./seed
```

## Why not just ask an LLM for a seed script?

- **Reproducible.** Seeded end to end — same seed, same bytes, today,
  tomorrow, in CI. Hash the output and assert on it.
- **Constraint-correct at scale.** FK dependency order, composite keys,
  `CHECK` constraints — satisfied by construction, then independently
  re-validated. Proven against a **real Postgres database**, not just this
  repo's own validator (see [`datagen_pkg/docs/postgres.md`](datagen_pkg/docs/postgres.md)).
  50,000-row benchmark: 2.0s generate, 2.8s load, zero violations.
- **Coherent, not just valid.** `updated_at >= created_at`, orders newer than
  their users, a `shipped` order has a `shipped_at`, a few power users own
  most of the orders — like real production data, not a random grid.
- **Connects to what you already have.** `--from-db` introspects a live
  Postgres/Supabase schema directly — no DDL file required.

## Repo layout

```
datagen_pkg/   the engine + CLI (pip package: synth-scale, import name: datagen)
web/           FastAPI + vanilla-JS playground (paste DDL or connect a database)
demo/          Demo Day script, benchmark writeup, case study
```

We're actively building this. If you try it, tell us whether you'd actually
use it — 30 seconds, no signup: [synth-scale-ashen.vercel.app/#feedback](https://synth-scale-ashen.vercel.app/#feedback).

Built by Comebck Pakistan Cohort 1, Squad Shigar.
