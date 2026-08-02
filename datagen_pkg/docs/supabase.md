# Supabase quickstart

Synth-Scale needs no special "Supabase mode" — Supabase is just Postgres, and
`--from-db` / `--format db` already speak plain SQLAlchemy connection URLs.
This page is the five-minute path: get your connection string, introspect
your real schema, generate data, load it back in.

## 1. Get your connection string

Supabase dashboard → **Project Settings → Database → Connection string**.
You'll see two flavors:

| Flavor | Port | Notes |
|---|---|---|
| **Session Pooler** (recommended) | 6543 | IPv4-reachable everywhere, works fine for both introspection and loading. Use this unless you have a reason not to. |
| **Direct connection** | 5432 | Requires SSL; on many Supabase projects it's IPv6-only, which fails from networks/CI runners without IPv6 egress. |

Either works with Synth-Scale — pooler is just the fewer-surprises default.
Copy the string as shown (it already includes `postgresql://`); Synth-Scale
also accepts the older `postgres://` form some tools still hand out and
rewrites it automatically.

Install the driver once: `pip install 'synth-scale[postgres]'` (or
`pip install synth-scale psycopg2-binary`).

## 2. Skip pasting a password on the command line

Export it once instead of passing `--db-url` every time:

```bash
export DATABASE_URL="postgresql://postgres.xxxx:yourpassword@aws-0-region.pooler.supabase.com:6543/postgres"
```

`--db-url` falls back to `$DATABASE_URL`, then `$SYNTH_SCALE_DB_URL`, if
omitted — so every command below can drop `--db-url` entirely once this is
set. (`SYNTH_SCALE_DB_URL` takes precedence over `DATABASE_URL` if both are
set, in case your shell already uses `DATABASE_URL` for something else.)

## 3. Introspect your real schema and preview

No DDL file needed — `--from-db` reads the live schema (tables, FKs, PKs,
UNIQUE, CHECK) straight out of `information_schema`/`pg_catalog`:

```bash
synth-scale --from-db --rows 20 --preview
```

This only ever runs `SELECT`s — nothing about your data or schema is
modified by `--preview`.

## 4. Generate and load back in

```bash
# write INSERT statements to a file you can review first
synth-scale --from-db --rows 1000 --format sql --out ./seed.sql

# ...or load directly, same connection, one transaction
synth-scale --from-db --rows 1000 --format db
```

`--format db` assumes the tables already exist (it never runs `CREATE
TABLE`) and loads in FK-safe order inside a single transaction — either the
whole batch lands or none of it does.

## Gotchas specific to Supabase

- **`postgres://` vs `postgresql://`** — SQLAlchemy 1.4+ rejects the bare
  `postgres://` scheme some older docs/snippets still use. Synth-Scale
  rewrites it for you; you don't need to edit the string Supabase gives you.
- **Auth schema is skipped.** Introspection only reads the `public` schema,
  so Supabase's own `auth.*` / `storage.*` tables are never touched or
  generated into.
- **Password ≠ account password.** The database password (used in the
  connection string) is set separately from your Supabase login — reset it
  from the same Database settings page if you don't have it.
- **Pooler + transactions:** `--format db`'s load runs as one transaction.
  PgBouncer's transaction-mode pooler (what the Session Pooler uses)
  supports that natively — no special flag needed.
- **Scratch database first.** Same advice as any tool that writes rows:
  point a first run at a Supabase branch/staging project, not production,
  until you trust the shape of what comes out.

## Web playground

The web front door's **Connect a database** tab does the read-only half of
this (introspect + generate + download) straight from the browser — see
[`web/README.md`](../../web/README.md#connect-to-supabase-apiconnect) for
what it does and doesn't do. It never writes back to your database; use the
CLI's `--format db` for that.
