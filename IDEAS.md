# Synth-Scale — Product Ideas, Filtered

**Clock:** ~5 weeks to demo week (ends Aug 16). 4 people. Two are on engine correctness + coherence (see `COHERENCE_DESIGN.md`); this list is what the other capacity buys.

**Competitive frame (verified Jul 2026):**
- **Claude/Cursor seed scripts** — the real competitor. Probably-correct, non-deterministic, re-prompt on every schema change, impractical at 50k rows. Our line: *"Claude writes you a probably-correct script; we give you guaranteed-correct data — first run, every run."*
- **Mockaroo** — column-grid web tool. No DDL parsing, no FK topology across 15 tables, no determinism, row caps. Not a CLI-native or CI story.
- **Snaplet — dead.** Hosted service shut down Aug 31 2024; `@snaplet/seed` was open-sourced and handed to `supabase-community/seed`, where activity since has been issues and minor fixes, not features. Translation: **validated demand in exactly our niche, with a vacuum where the product was.** A "Seedfast" has popped up marketing itself as the Snaplet-seed alternative — the vacuum is being noticed; move fast. (Sources at bottom.)

---

## Do before demo day (max 5)

| # | Idea | Why it wins | Effort |
|---|---|---|---|
| 1 | **Package it: `uvx synth-scale`** — `pyproject.toml`, console entry point, publish to PyPI (settle the name now, `datagen` is unshippable). | The whole acquisition strategy is the 30-second try. Every demo, README, and tweet starts with this one line. Currently it's `python -m datagen.cli` and a `requirements.txt` — not shareable. Review gap #16. | **S** (0.5–1d) |
| 2 | **Supabase live introspection** — `synth-scale --db postgres://... --rows users=1000...`: read `information_schema`/`pg_catalog` (columns, PK/FK/UNIQUE/CHECK, enums) into the existing `SchemaModel`, generate, load back with psycopg. No DDL file at all. | The magic moment for the exact target user: point it at your Supabase project, get a populated project. Kills the "I don't have a clean .sql file" objection (most real projects don't — they have migration folders). Reuses everything downstream of `SchemaModel`. | **M** (3–4d) |
| 3 | **`--preview` mode (Rich)** — parse → print the plan (table order, deferred FKs) + a pretty 10-row sample per table, coherence highlights (created→shipped→delivered in order), zero writes. | Demo gold and trust-builder: "see it before you seed it." Also the squad's own best debugging tool for the coherence lane. Rich is already the chosen stack. | **S** (1–2d) |
| 4 | **Data contracts for CI** — print a per-table SHA-256 of output; `synth-scale --lock synthscale.lock` writes hashes, `--check` exits non-zero on mismatch. Docs show a 10-line GitHub Actions job (postgres service → generate → psql load → `--check`). | The wedge neither Claude nor Mockaroo can copy: *deterministic* fixtures you can assert on. Turns "same seed, same bytes" from a claim into a CI primitive. **Depends on determinism fixes #7 (UUID) and #8 (wall-clock) landing first.** | **S** (1–2d) |
| 5 | **The mic-drop demo asset** — a 15-table SaaS schema (orgs, users, employees w/ managers, categories, products, orders, order_items, invoices, tickets…), scripted run: `uvx synth-scale` → `--preview` → 50k rows into a live Supabase project in under a minute → open Studio: coherent order history, real org chart, matching cities. Rehearsed, timed, with a fallback recording. | Demos are won by the artifact, not the feature list. This schema doubles as the property-test fixture the review demands (put the hard cases in it: `NUMERIC(4,2)`, UUID PKs, date CHECKs, mixed composite UNIQUE). | **S–M** (2d, owned by the product/growth lane) |

Order of operations: 1 → 3 → 5 can start immediately; 2 in parallel; 4 waits on the determinism fixes. The coherence layer itself (other lane) is the prerequisite for 3 and 5 looking good — it ships first.

## Do only if time (max 5)

| Idea | Notes | Effort |
|---|---|---|
| **Web front door** — paste DDL, ≤1k rows, download CSV/SQL, waitlist capture | Build-spec rule stands: only after the CLI runs clean on the reference schema. Thin Next.js page over the same engine via one API route. Waitlist page alone (no generator) is the S fallback. | **M–L** |
| **`--smart` config generator** — one LLM call, schema in → `synthscale.toml` out (semantic hints, fan-out ratios, status chains). Never touches rows. | Great narrative ("Claude configures, the engine guarantees") but the demo works without it. Post-demo is fine. | **M** |
| **`--rows` ergonomics** — `--rows 1000` seeds root tables at N and children by fan-out multiplier; per-table syntax stays as override | Review gap: 15-table schema currently needs 15 `table=count` pairs. Painful exactly in our own demo. | **S** |
| **Output polish** — CSV `\N` nulls (review #14), `COPY`-format writer, JSONL export for mock APIs | Small, real, unglamorous. The `\N` fix should ride along with the criticals regardless. | **S** |
| **`--dialect supabase` niceties** — recognize `auth.users` FK convention, `uuid_generate_v4()`/`gen_random_uuid()` defaults, RLS-table noise tolerance | Only if introspection (#2) surfaces these as actual blockers on real Supabase projects. | **S–M** |

## Explicitly NOT doing (and why)

| Trap | Why we refuse |
|---|---|
| **MySQL / Mongo / SQLite support** | Each is a fork of type system + introspection + loader + quirks. sqlglot making parsing *look* free is exactly what makes this a trap. The Postgres/Supabase wedge is the strategy, not a limitation. Revisit post-cohort. |
| **Web-app-first** | Browser can't safely hold live DB credentials → web is structurally the lite tier (build spec already fenced this). Every web hour is an hour off the differentiator. |
| **AI in the data path** | One LLM call per row/table destroys determinism, speed, and offline use — i.e., destroys the pitch. AI's only seat is the optional one-shot schema→config step (`--smart`, later). |
| **Millions-of-rows / vectorization** | numpy rewrite of the generator core for a use case (seed/dev/CI data) that needs 10k–100k rows. 50k rows in seconds is already achievable; perf theater steals coherence time. |
| **Prod-data anonymization / snapshotting** | That's Snaplet's *other* product — different trust, security, and legal surface. Also: adjacent graveyard. We generate from schema, full stop. |
| **Custom generator plugin API** | Pool JSON files + config overrides cover ~90% of "make column X realistic." A public API surface frozen before v1 is pure future regret. |
| **Auth, billing, dashboards, hosted SaaS** | Already out per build spec. Nothing about demo day or the grade needs an account system. |

---

**One reallocation worth saying out loud:** nothing above matters if the live `psql` load fails on stage — the review's six criticals and the "CI loads into real Postgres" test are the true top of this list, and they're already in flight. Everything in "Do before demo day" assumes that lane lands by end of July.

Sources: [Snaplet is now open source (Supabase blog)](https://supabase.com/blog/snaplet-is-now-open-source) · [snaplet.dev](https://snaplet.dev/) · [supabase-community/seed handover & activity](https://github.com/snaplet/docs) · [Seedfast positioning as Snaplet-seed alternative](https://seedfa.st/blog/snaplet-seed-alternative)
