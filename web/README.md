# Synth-Scale — web front door

Landing page + waitlist + a **capped** try-it playground for the
[synth-scale](../datagen_pkg) engine. The CLI is the product; this site exists
to capture signups and let people who won't install a CLI paste a schema,
preview coherent data, and download SQL/CSV.

- **FastAPI** backend (`app/main.py`) calling the engine in-process
  (`parse_ddl → run_engine → validate`), all output built in memory.
- **Vanilla static frontend** (`static/`) — no build chain, no CDN, no
  analytics, no cookies. Trivially replaceable with a Next.js frontend later;
  the API contract is `POST /api/generate`, `POST /api/connect`, and
  `POST /api/waitlist`.
- Two schema sources: paste DDL text (`/api/generate` — parsed only via
  sqlglot, never executed against anything) or **Connect to Supabase**
  (`/api/connect` — introspects a caller-supplied Postgres connection string
  read-only; see [Connect to Supabase](#connect-to-supabase-apiconnect)
  below). There is no write-back path anywhere in this app.

## Run locally

```bash
# 1. engine + web deps into one environment (any venv works; the repo's
#    engine venv already has everything after:)
pip install -e "../datagen_pkg[postgres]"
pip install -r requirements.txt

# 2. from web/:
uvicorn app.main:app --reload
# -> http://127.0.0.1:8000
```

On this repo's Windows checkout, the ready-made interpreter is
`A:/comebck/datagen/datagen_pkg/.venv/Scripts/python.exe`:

```powershell
cd A:\comebck\datagen\web
A:/comebck/datagen/datagen_pkg/.venv/Scripts/python.exe -m uvicorn app.main:app
```

## Tests

```bash
# from the repo root (or anywhere; conftest fixes sys.path)
python -m pytest web/tests -q
```

## API

| Route | What |
|---|---|
| `POST /api/generate` | `{ddl, rows=50, seed=42, format: "preview"\|"sql"\|"csv"}`. `preview` → JSON (first 10 rows/table, row counts, validator summary); `sql` → downloadable `.sql`; `csv` → zip of per-table CSVs. |
| `POST /api/connect` | `{db_url, rows=50, seed=42, format: "preview"\|"sql"\|"csv"}`. Same response shapes as `/api/generate`, but the schema comes from introspecting `db_url` instead of parsing DDL text. Read-only; 503 if the deploy doesn't have `psycopg2-binary` installed. |
| `POST /api/waitlist` | `{email}` → `{ok, added, count}`. Appends `{email, ts, ua}` to `data/waitlist.jsonl`, deduped by email. No other PII. |
| `GET /api/waitlist/count` | `{count}` for social proof. |
| `GET /api/health` | liveness. |
| `GET /` | static frontend. |

## Hard caps (enforced server-side, before generation)

| Cap | Value | On violation |
|---|---|---|
| DDL size | 50 KB | 413 |
| Tables | 15 | 422 |
| Total columns | 120 | 422 |
| `rows` request field | 1–1,000 | 422 |
| **Total generated rows** | 1,000 across all tables | scaled down (see below) |
| Generation wall time | 10 s (worker thread + timeout) | 408 |
| Request body | 256 KB | 413 |
| Rate limit (`/api/generate`) | 10 / hour / IP | 429 + `Retry-After` |
| Rate limit (`/api/connect`) | 3 / hour / IP | 429 + `Retry-After` |
| DB connect timeout | 5 s | 422 |
| DB introspection wall time | 12 s | 408 |

**Row distribution:** same heuristic as the CLI's integer `--rows` form —
every table gets `rows`, but a table with FK parents gets **3× the largest
parent count, capped at 10× the base** (self-referencing FKs don't count).
Because the playground caps *total* rows at 1,000, the resulting plan is then
scaled down proportionally (floor, minimum 1 row per table) until it fits.
The response's `row_counts` shows exactly what was generated.

**Timeout caveat:** Python threads can't be killed, so a timed-out generation
finishes in the background; the caps above keep that bounded. The 408 exists
so no request ever hangs.

**Rate-limit caveat:** the sliding window is in-process memory. With multiple
uvicorn workers each worker has its own window (effective limit = 10 ×
workers) and restarts clear it. Run one worker (the Dockerfile does), or move
limiting to a proxy/Redis if you scale out.

## Connect to Supabase (`/api/connect`)

The one place this app opens a real database connection. Deliberately
scoped down from what the CLI can do:

- **Read-only.** `/api/connect` only ever introspects (`information_schema`
  / `pg_catalog` SELECTs) — there is no INSERT/write-back path in this app,
  by design. To seed your database directly, use the CLI's `--format db`.
- **Not stored, not logged.** The connection string lives for the duration
  of one request and is never written to disk or a log line. Error messages
  show a redacted form (`postgresql://user:***@host:port/db`).
- **SSRF-guarded.** Before any socket opens, the target host is resolved and
  rejected if it's a private/loopback/link-local/metadata address
  (`app/dbsafety.py`). This blocks the obvious "point our server at its own
  cloud metadata endpoint" class of attack; it does not close a
  DNS-rebinding race — see that file's docstring for the honest limitation.
- **Its own rate limit** (`SYNTH_SCALE_DB_RATE_LIMIT`, default 3/hour/IP —
  tighter than `/api/generate`'s 10/hour, because this is a real outbound
  network call to a third-party host, not just parsing text.
- **Bounded twice:** a short libpq `connect_timeout`
  (`SYNTH_SCALE_DB_CONNECT_TIMEOUT`, default 5s) so an unreachable host fails
  fast, and a wall-time budget around the whole introspection call
  (`SYNTH_SCALE_DB_TIMEOUT`, default 12s).
- **Needs the postgres extra.** If `psycopg2-binary` isn't installed,
  `/api/connect` returns a clean 503 instead of the app failing to boot —
  the paste-DDL flow keeps working either way.

Where users get a connection string: Supabase dashboard → **Project
Settings → Database → Connection string**. The Session Pooler string (port
6543) works and is IPv4-reachable everywhere; the direct connection (5432)
requires SSL and is IPv6-only on some Supabase projects.

## Security notes

- No CORS middleware → no `Access-Control-Allow-*` headers → browsers enforce
  same-origin on the API.
- Security headers on every response: CSP `default-src 'self'`, `nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, COOP/CORP.
- No auth, accounts, cookies, or tracking — deliberately out of scope.
- Client IP for rate limiting prefers the left-most `X-Forwarded-For` entry
  (set by the platform proxy); direct clients can spoof it, acceptable for a
  soft playground limit.

## Deploy (free tier)

**Recommendation: [Render](https://render.com) free web service with the
Dockerfile.** It is the least-friction fit for a single always-defined
Docker service; Fly.io is the better pick *if* you pay for a volume (see
waitlist note).

1. Push the repo to GitHub.
2. Render → New → Web Service → connect the repo.
3. Settings:
   - **Root Directory:** *(repo root — leave blank)*
   - **Runtime:** Docker, **Dockerfile Path:** `web/Dockerfile`
   - Instance type: Free.
4. Deploy. Render routes to the container's port 8000 automatically
   (`EXPOSE`), or set `PORT` and override the start command with
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

**⚠ Waitlist persistence:** `data/waitlist.jsonl` is on the container
filesystem, which is **ephemeral on Render's free tier** — every deploy or
restart wipes it. Options, in order of honesty:

- **Free + zero ops:** swap the waitlist form's `fetch` to a hosted form
  (Formspree/Tally) and keep this API for the playground only.
- **Paid disk:** Render persistent disk or a Fly.io volume mounted at
  `/srv/web/data` (set `SYNTH_SCALE_DATA_DIR=/srv/web/data`).
- **Stopgap:** `GET /api/waitlist/count` before each deploy and export the
  JSONL via a shell on the instance. Fragile; don't rely on it for cohort
  metrics.

Railway and Fly work with the same Dockerfile (`fly launch` detects it; set
`internal_port = 8000`).

### Env knobs

| Var | Default | Meaning |
|---|---|---|
| `SYNTH_SCALE_DATA_DIR` | `web/data` | where `waitlist.jsonl` lives |
| `SYNTH_SCALE_TIMEOUT` | `10` | generation wall-time budget (s) |
| `SYNTH_SCALE_RATE_LIMIT` | `10` | `/api/generate` calls per hour per IP |
| `SYNTH_SCALE_DB_RATE_LIMIT` | `3` | `/api/connect` calls per hour per IP |
| `SYNTH_SCALE_DB_CONNECT_TIMEOUT` | `5` | libpq TCP connect timeout (s) for `/api/connect` |
| `SYNTH_SCALE_DB_TIMEOUT` | `12` | wall-time budget around introspection (s) for `/api/connect` |

## Layout

```
web/
  app/
    main.py        # FastAPI app, routes, middleware (security headers, body cap)
    service.py     # caps, row distribution, timeout wrapper, in-memory SQL/CSV-zip
    service_db.py  # /api/connect pipeline: introspect a live DB instead of parsing DDL
    dbsafety.py    # SSRF guard: rejects private/internal hosts before connecting
    ratelimit.py   # in-memory sliding window limiter
    waitlist.py    # JSONL waitlist store, dedupe by email
  static/          # index.html + style.css + app.js (vanilla, self-contained)
  tests/test_api.py, test_connect.py
  data/            # waitlist.jsonl (created at runtime; gitignored territory)
  Dockerfile       # build from REPO ROOT: docker build -f web/Dockerfile .
```
