# Postgres: the real acceptance bar

Synth-Scale's core claim is that generated data loads into a **real Postgres
database with zero constraint violations** — not just past our own validator.
Three test modules make that claim executable:

| Module | What it proves |
|---|---|
| `tests/test_postgres_load.py` | Both fixtures (`sample.sql`, `hard.sql`) load with zero errors; row counts verified by SQL against the live DB; every FK edge checked for orphans with `NOT EXISTS` joins; plus a 50k-row benchmark. |
| `tests/test_introspect.py` | `datagen.introspect.introspect_db(db_url)` reads a live DB's schema (information_schema + pg_catalog) into the same `SchemaModel` that `parse_ddl` produces — asserted equivalent field-by-field — then generates from the *introspected* model and loads back with zero errors. |
| `.github/workflows/ci.yml` (repo root) | Runs the whole suite against a `postgres:16` service container on every push/PR. |

## Running the Postgres-gated tests locally

All live-DB tests are gated on one environment variable and **skip cleanly
when it is unset** — the suite stays green offline.

```
SYNTH_PG_URL=postgresql+psycopg2://<user>[:<pass>]@<host>:<port>/<scratch-db>
```

**WARNING:** the tests drop and re-create tables (the introspection tests
clean out the entire `public` schema). Point `SYNTH_PG_URL` at a scratch
database only.

You also need the driver in your venv: `pip install psycopg2-binary`.

### Route 1: Docker one-liner

```bash
docker run -d --name synth-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=synthtest -p 55932:5432 postgres:16
export SYNTH_PG_URL=postgresql+psycopg2://postgres:postgres@localhost:55932/synthtest
python -m pytest tests/ -q
```

### Route 2: Portable Windows binaries (no Docker, no installer, no admin)

Download the PostgreSQL 16 x64 binaries zip from the EnterpriseDB binaries
archive (https://www.enterprisedb.com/download-postgresql-binaries), unzip to
a scratch directory, then:

```powershell
# extract just what you need (skips the huge bundled pgAdmin):
tar -xf postgresql-16.4-1-windows-x64-binaries.zip pgsql/bin pgsql/lib pgsql/share

pgsql\bin\initdb.exe -D .\pgdata -U postgres -E UTF8 -A trust --no-locale
pgsql\bin\pg_ctl.exe -D .\pgdata -l server.log -o "-p 55932" -w start
pgsql\bin\createdb.exe -p 55932 -U postgres -h localhost synthtest

$env:SYNTH_PG_URL = "postgresql+psycopg2://postgres@localhost:55932/synthtest"
python -m pytest tests/ -q

pgsql\bin\pg_ctl.exe -D .\pgdata stop        # teardown
```

Windows gotcha: check `netsh interface ipv4 show excludedportrange protocol=tcp`
first — Hyper-V reserves seemingly random port ranges, and a port inside one
fails to bind with a bare "Permission denied" (55432 was reserved on the
machine this was first proven on; 55932 was not).

## What the CI workflow does

`.github/workflows/ci.yml` (repo root; **written offline, not yet exercised
on GitHub**): ubuntu-latest, Python 3.12, a `postgres:16` service container
with `pg_isready` health checks, `pip install -e ./datagen_pkg
psycopg2-binary pytest`, then the full `pytest tests/ -q` with `SYNTH_PG_URL`
pointing at the service. Because the env var is set in CI, the live-DB tests
run there — "Postgres accepted it" is the merge bar, not an opt-in extra.

## Benchmark (demo-day number)

`test_benchmark_50k_rows_hard_fixture` generates and loads **50,000 rows**
across the 10 tables of the adversarial `hard.sql` fixture (UUID PKs,
composite PK/UNIQUE, CHECK/enum constraints, self-referencing FK) and times
each phase separately. Run it with `-s` to see the timing line.

Measured 2026-07-19, Windows 11, PostgreSQL 16.4 (portable binaries, same
machine, localhost TCP), seed 42:

| Phase | Wall time |
|---|---|
| generate (`run_engine`, 50k rows) | **2.0 s** |
| load (`load_to_db`, batched inserts, one transaction) | **2.8 s** |

Repeat runs on an idle machine landed at generate 2.0–3.2 s / load 2.7–2.8 s;
with the rest of the suite running concurrently both phases degrade to ~9 s.
Zero constraint violations in every run, and post-load SQL verification
(exact row counts + no FK orphans on any edge) passed each time.
