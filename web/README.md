# Synth-Scale — web front door

Landing page + waitlist + a **capped** try-it playground for the
[synth-scale](../datagen_pkg) engine. The CLI is the product; this site exists
to capture signups and let people who won't install a CLI paste a schema,
preview coherent data, and download SQL/CSV.

- **FastAPI** backend (`app/main.py`) calling the engine in-process
  (`parse_ddl → run_engine → validate`), all output built in memory.
- **Vanilla static frontend** (`static/`) — no build chain, no CDN, no
  third-party trackers, no cookies. First-party pageview/click counts only
  (`POST /api/track`, key-gated `GET /api/stats`). Trivially replaceable
  with a Next.js frontend later; the API contract is `POST /api/generate`,
  `POST /api/connect`, `POST /api/waitlist`, `POST /api/contact`, and
  `POST /api/track`.
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
| `POST /api/contact` | `{name?, email, message}` → `{ok: true}`. Appends to `data/contact_messages.jsonl` (durable regardless of email config), then best-effort sends an SMTP notification if `SYNTH_SCALE_SMTP_*` env vars are set. Rate-limited separately (5/hour). |
| `POST /api/track` | `{event: "pageview"\|"click", path, target?, session_id?}` → `{ok: true}`. Fired automatically by `app.js` on every page load and on every click of an element tagged `data-track="..."`. Appends to `data/analytics.jsonl`. No IP is stored; `session_id` is a random id the client keeps in `sessionStorage` (not a cookie), purely so `/api/stats` can report unique sessions. Rate-limited (120/hour/IP by default). |
| `GET /api/stats` | Key-gated (`X-Stats-Key` header, must equal `SYNTH_SCALE_STATS_KEY`) → `{pageviews, unique_sessions, clicks_by_target, pageviews_by_path, waitlist_count, contact_messages}`. 404s (not 401/403) for a missing/wrong key, and unconditionally if `SYNTH_SCALE_STATS_KEY` isn't set. See [Stats dashboard](#stats-dashboard-apistats). |
| `GET /api/health` | liveness. |
| `GET /` | static frontend. |
| `GET /stats.html` | private stats dashboard UI (not linked from the site). Prompts for the stats key once, keeps it in `localStorage`, calls `/api/stats`. |

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
| Rate limit (`/api/track`) | 120 / hour / IP | 429 + `Retry-After` |
| Rate limit (`/api/stats`) | 20 / hour / IP | 429 + `Retry-After` |
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
- No auth, accounts, or cookies. First-party analytics (`/api/track`,
  above) is opt-in-to-view (`/api/stats` is key-gated) and stores no IP
  addresses — see [Stats dashboard](#stats-dashboard-apistats).
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

**⚠ Waitlist / contact / analytics persistence:** `waitlist.jsonl`,
`contact_messages.jsonl`, and `analytics.jsonl` all live under
`SYNTH_SCALE_DATA_DIR` (default `web/data`) on whatever filesystem the
process sees — which is **ephemeral on every free-tier platform this app
has been deployed to so far**, just in different ways:

- **Render free tier:** container filesystem, wiped on every deploy or
  restart.
- **Vercel (current live deploy):** `SYNTH_SCALE_DATA_DIR=/tmp/data`,
  because Vercel's filesystem is read-only everywhere except `/tmp`. `/tmp`
  is **not shared across invocations** and is **not guaranteed to survive**
  between them — Vercel's Python functions run as ephemeral serverless
  containers, so a signup captured by one invocation may vanish before the
  next one runs `GET /api/waitlist/count`. In practice this means: don't
  trust the live Vercel deployment's stored data as the source of truth for
  real signups/messages/analytics right now.

Options, in order of honesty:

- **Free + zero ops, no code change:** swap the waitlist/contact forms'
  `fetch` calls to a hosted form (Formspree/Tally) and keep this app's API
  for the playground and `/api/stats` only.
- **Free + durable, moderate effort:** point this app at a Postgres
  database you already control (e.g. the Supabase project used for
  "Connect to Supabase" testing) and swap the three JSONL stores for a
  couple of tables. Not implemented yet — ask if you want this built; it's
  the actual fix for "where do these get stored durably."
- **Paid disk:** Render persistent disk or a Fly.io volume mounted at
  `/srv/web/data` (set `SYNTH_SCALE_DATA_DIR=/srv/web/data`). Doesn't apply
  to Vercel regardless of paid tier — Vercel functions have no persistent
  disk at any price point; you'd need to move off Vercel for this option.
- **Stopgap:** hit `/api/stats` (with the key) or `GET
  /api/waitlist/count` frequently and copy the numbers down. Fragile,
  loses the underlying rows, don't rely on it for real cohort data.

Railway and Fly work with the same Dockerfile (`fly launch` detects it; set
`internal_port = 8000`) and both have real persistent-container
filesystems, so JSONL storage is actually durable there (still wiped by a
full redeploy unless you also mount a volume).

### Stats dashboard (`/api/stats`)

1. Set `SYNTH_SCALE_STATS_KEY` to a long random string wherever the app
   runs (unset = the endpoint 404s for everyone, always).
2. Visit `/stats.html` (not linked from the site anywhere), paste the key
   once — it's kept in that browser's `localStorage` and sent as an
   `X-Stats-Key` header, never a URL query param, so it doesn't end up in
   server/proxy access logs or browser history.
3. See pageviews, unique sessions, clicks by target, pageviews by path,
   waitlist signups, and contact messages, all pulled live from
   `analytics.jsonl` / `waitlist.jsonl` / `contact_messages.jsonl`.

Same caveat as above applies: on the current Vercel deployment these
numbers reset unpredictably because the underlying files live in `/tmp`.

### Env knobs

| Var | Default | Meaning |
|---|---|---|
| `SYNTH_SCALE_DATA_DIR` | `web/data` | where `waitlist.jsonl` lives |
| `SYNTH_SCALE_TIMEOUT` | `10` | generation wall-time budget (s) |
| `SYNTH_SCALE_RATE_LIMIT` | `10` | `/api/generate` calls per hour per IP |
| `SYNTH_SCALE_DB_RATE_LIMIT` | `3` | `/api/connect` calls per hour per IP |
| `SYNTH_SCALE_DB_CONNECT_TIMEOUT` | `5` | libpq TCP connect timeout (s) for `/api/connect` |
| `SYNTH_SCALE_DB_TIMEOUT` | `12` | wall-time budget around introspection (s) for `/api/connect` |
| `SYNTH_SCALE_CONTACT_RATE_LIMIT` | `5` | `/api/contact` submissions per hour per IP |
| `SYNTH_SCALE_SMTP_HOST` / `_PORT` (587) / `_USER` / `_PASS` | unset | SMTP credentials for the contact form's email notification. Unset means messages are still stored in `data/contact_messages.jsonl`, just not emailed. A Gmail account works with an [App Password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification on) as `_USER`/`_PASS`, `smtp.gmail.com` as `_HOST`. |
| `SYNTH_SCALE_CONTACT_TO` | `abdullahk80808080@gmail.com` | where contact-form notifications are sent |
| `SYNTH_SCALE_TRACK_RATE_LIMIT` | `120` | `/api/track` events per hour per IP |
| `SYNTH_SCALE_STATS_KEY` | unset | shared secret required (as an `X-Stats-Key` header) to read `/api/stats`. Unset = the endpoint 404s unconditionally, i.e. stats are off by default. |

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
    contact.py     # contact form: JSONL store + best-effort SMTP notification
    analytics.py   # pageview/click JSONL store + aggregation for /api/stats
  static/          # index.html + style.css + app.js + stats.html/stats.js (vanilla, self-contained)
  tests/test_api.py, test_connect.py, test_contact.py, test_analytics.py
  data/            # waitlist.jsonl, contact_messages.jsonl, analytics.jsonl (created at runtime; gitignored territory)
  Dockerfile       # build from REPO ROOT: docker build -f web/Dockerfile .
```
