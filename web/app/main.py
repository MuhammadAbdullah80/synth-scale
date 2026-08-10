"""Synth-Scale web front door: landing page + waitlist + capped playground.

Run from the `web/` directory:  uvicorn app.main:app

Security posture:
- /api/generate: the submitted DDL is untrusted *text* fed to sqlglot for
  parsing only. It is never executed against any database.
- /api/connect: the one place this app DOES open a real database connection
  (opt-in "Connect to Supabase" flow). It is read-only end to end --
  introspection only, no INSERT/write-back path exists anywhere in this app
  -- the connection string is used for a single request and never logged or
  persisted, the target host is checked against private/internal IP ranges
  before any socket opens (dbsafety.py), and it has its own, much tighter
  rate limit than /api/generate. See service_db.py and dbsafety.py for the
  full rationale and documented residual risk.
- No CORS middleware is installed, so no Access-Control-Allow-* headers are
  ever emitted: browsers enforce same-origin for the API.
- Request bodies are capped (413) before they reach any handler.
- Security headers (CSP self-only, nosniff, frame-deny, ...) on every
  response.
- No auth, no accounts, no cookies. Stored data: the waitlist, contact
  messages, a "would you use this" feedback survey, and first-party
  analytics (pageview/click events tagged with a random per-tab session id,
  no IP, no cross-site tracking, no third party involved -- see
  analytics.py). /api/stats exposes aggregates from that data, gated behind
  SYNTH_SCALE_STATS_KEY. All four persist to local JSONL files by default,
  or to Postgres tables if SYNTH_SCALE_STORAGE_DB_URL is set (see
  storage.py) -- the latter is what actually survives on a platform like
  Vercel where the filesystem doesn't.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import analytics, contact, service, survey
from .ratelimit import SlidingWindowLimiter
from .waitlist import Waitlist, valid_email

try:
    # service_db itself only imports SQLAlchemy (a base dependency, always
    # present) -- the actual Postgres driver is loaded lazily by SQLAlchemy
    # at connect time, so importing service_db alone would succeed even
    # without the [postgres] extra. Check for psycopg2 explicitly so a
    # deploy missing it gets a clean 503 up front instead of every request
    # failing deep inside a connection attempt. storage.py needs the same
    # driver, so it's gated behind the same flag.
    import psycopg2  # noqa: F401

    from . import service_db, storage
    DB_CONNECT_AVAILABLE = True
except ImportError:
    service_db = None
    storage = None
    DB_CONNECT_AVAILABLE = False

WEB_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = WEB_DIR / "static"
DATA_DIR = Path(os.environ.get("SYNTH_SCALE_DATA_DIR", WEB_DIR / "data"))

MAX_BODY_BYTES = 256 * 1024  # generous roof over the 50 KB DDL cap
RATE_LIMIT = int(os.environ.get("SYNTH_SCALE_RATE_LIMIT", "10"))
RATE_WINDOW_SECONDS = 3600.0

# Connecting to a caller-supplied database is far more expensive/risky than
# parsing pasted DDL text (it opens a real outbound TCP connection to
# whatever host they typed), so it gets its own, much tighter limit.
DB_RATE_LIMIT = int(os.environ.get("SYNTH_SCALE_DB_RATE_LIMIT", "3"))

# The contact form triggers a real outbound email send (once SMTP is
# configured), so it gets its own tight limit -- same spirit as db_limiter,
# capping abuse of whatever mailbox SYNTH_SCALE_SMTP_USER is.
CONTACT_RATE_LIMIT = int(os.environ.get("SYNTH_SCALE_CONTACT_RATE_LIMIT", "5"))

# /api/track fires automatically on every page load and tagged click, so it
# needs real headroom -- generous default, still capped so it can't be
# turned into a free-form log-spam endpoint.
TRACK_RATE_LIMIT = int(os.environ.get("SYNTH_SCALE_TRACK_RATE_LIMIT", "120"))

# One person shouldn't be able to flood the feedback data with repeat
# submissions; low limit because a real user only fills this out once.
SURVEY_RATE_LIMIT = int(os.environ.get("SYNTH_SCALE_SURVEY_RATE_LIMIT", "5"))

# /api/stats is gated behind this shared secret (sent as an X-Stats-Key
# header, never a query param, so it never lands in server/proxy access
# logs or browser history). Unset (the default) means the endpoint 404s
# unconditionally -- there is no "stats are open" default.
STATS_KEY = os.environ.get("SYNTH_SCALE_STATS_KEY", "")

# Operator-owned storage backend for waitlist/contact/analytics. Unset (the
# default) keeps everything on local JSONL files under DATA_DIR -- correct
# for local dev and any platform with a real persistent filesystem. Set
# this to a Postgres connection string (a Supabase pooler URL works well)
# to make that data actually durable on platforms like Vercel where the
# filesystem doesn't survive between invocations. This is a separate,
# operator-configured connection string from the one end users paste into
# "Connect to Supabase" -- see storage.py's module docstring.
STORAGE_DB_URL = os.environ.get("SYNTH_SCALE_STORAGE_DB_URL", "")

limiter = SlidingWindowLimiter(limit=RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
db_limiter = SlidingWindowLimiter(limit=DB_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
contact_limiter = SlidingWindowLimiter(limit=CONTACT_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
track_limiter = SlidingWindowLimiter(limit=TRACK_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
survey_limiter = SlidingWindowLimiter(limit=SURVEY_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
stats_limiter = SlidingWindowLimiter(limit=20, window_seconds=RATE_WINDOW_SECONDS)

waitlist = contact_store = analytics_store = survey_store = None
if STORAGE_DB_URL:
    if not DB_CONNECT_AVAILABLE:
        raise RuntimeError(
            "SYNTH_SCALE_STORAGE_DB_URL is set but psycopg2 isn't installed "
            "-- install the [postgres] extra or unset the env var."
        )
    try:
        storage.ensure_schema(STORAGE_DB_URL)
        waitlist = storage.WaitlistDB(STORAGE_DB_URL)
        contact_store = storage.ContactDB(STORAGE_DB_URL)
        analytics_store = storage.AnalyticsDB(STORAGE_DB_URL)
        survey_store = storage.SurveyDB(STORAGE_DB_URL)
    except Exception as exc:  # pragma: no cover - depends on live network/DB
        # A misconfigured or momentarily-unreachable storage DB shouldn't take
        # the whole site down -- fall back to (locally durable, if the
        # filesystem is) JSONL rather than failing every request at startup.
        print(
            f"[synth-scale] SYNTH_SCALE_STORAGE_DB_URL set but unreachable at "
            f"startup ({exc}); falling back to local JSONL storage.",
            file=sys.stderr,
        )
if waitlist is None:
    waitlist = Waitlist(DATA_DIR / "waitlist.jsonl")
    contact_store = contact.ContactStore(DATA_DIR / "contact_messages.jsonl")
    analytics_store = analytics.AnalyticsStore(DATA_DIR / "analytics.jsonl")
    survey_store = survey.SurveyStore(DATA_DIR / "survey.jsonl")

app = FastAPI(title="Synth-Scale", docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class BodySizeLimitMiddleware:
    """Reject oversized request bodies with 413 before they reach a handler.

    Checks Content-Length up front and also counts streamed bytes, so a
    chunked request can't bypass the cap.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            await self._reject(send)
            return

        received = 0
        rejected = False

        async def counting_receive():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, counting_receive, send)

    async def _reject(self, send):
        body = b'{"detail":"Request body too large (max 256 KB)."}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *[(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)


def client_ip(request: Request) -> str:
    """Best-effort client IP. Behind the platform proxy (Render/Fly/...) the
    left-most X-Forwarded-For entry is the caller; it is spoofable by direct
    clients, which is acceptable for a soft playground limit."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    ddl: str = Field(min_length=1, max_length=200_000)
    rows: int = Field(default=50, ge=1, le=service.MAX_TOTAL_ROWS)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    format: Literal["preview", "sql", "csv"] = "preview"


class ConnectRequest(BaseModel):
    db_url: str = Field(min_length=1, max_length=2000)
    rows: int = Field(default=50, ge=1, le=service.MAX_TOTAL_ROWS)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    format: Literal["preview", "sql", "csv"] = "preview"


class WaitlistRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ContactRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    email: str = Field(min_length=3, max_length=254)
    message: str = Field(min_length=1, max_length=4000)


class TrackRequest(BaseModel):
    event: Literal["pageview", "click"]
    path: str = Field(default="/", max_length=200)
    target: str = Field(default="", max_length=100)
    session_id: str = Field(default="", max_length=64)


class SurveyRequest(BaseModel):
    would_use: Literal["yes", "maybe", "no"]
    use_case: str = Field(default="", max_length=survey.MAX_TEXT_LEN)
    blockers: str = Field(default="", max_length=survey.MAX_TEXT_LEN)
    email: str = Field(default="", max_length=254)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "service": "synth-scale-web"}


@app.post("/api/generate")
def generate(req: GenerateRequest, request: Request):
    allowed, retry_after = limiter.check(client_ip(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"Rate limit: {limiter.limit} generations per hour per IP. "
                    f"Try again in ~{max(retry_after // 60, 1)} min, or install "
                    f"the CLI (pip install synth-scale) for unlimited local runs."
                )
            },
            headers={"Retry-After": str(retry_after)},
        )

    try:
        schema, order, row_counts, generated, report = service.generate(
            req.ddl, req.rows, req.seed
        )
    except service.CapError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    if req.format == "preview":
        return service.preview_payload(
            schema, order, row_counts, generated, report, req.seed, req.rows
        )

    if req.format == "sql":
        text = service.sql_text(schema, generated, order, req.seed)
        return Response(
            content=text,
            media_type="application/sql",
            headers={
                "Content-Disposition": f'attachment; filename="synth_scale_seed{req.seed}.sql"'
            },
        )

    # csv -> zip of per-table files, built entirely in memory
    blob = service.csv_zip_bytes(schema, generated, order)
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="synth_scale_seed{req.seed}_csv.zip"'
        },
    )


@app.post("/api/connect")
def connect(req: ConnectRequest, request: Request):
    """Read-only: introspect a caller-supplied database, generate data from
    its live schema, and return it. There is no path from this endpoint (or
    anywhere else in this app) that writes back to the caller's database --
    see service_db.py's module docstring for the full security posture."""
    if not DB_CONNECT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Database connect isn't enabled on this deployment. Use the "
                   "CLI instead: pip install 'synth-scale[postgres]'.",
        )

    allowed, retry_after = db_limiter.check(client_ip(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": (
                    f"Rate limit: {db_limiter.limit} database connections per hour "
                    f"per IP. Try again in ~{max(retry_after // 60, 1)} min, or "
                    f"install the CLI (pip install synth-scale) for unlimited "
                    f"local runs."
                )
            },
            headers={"Retry-After": str(retry_after)},
        )

    try:
        schema, order, row_counts, generated, report = service_db.generate_from_db(
            req.db_url, req.rows, req.seed
        )
    except service.CapError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    if req.format == "preview":
        return service.preview_payload(
            schema, order, row_counts, generated, report, req.seed, req.rows
        )

    if req.format == "sql":
        text = service.sql_text(schema, generated, order, req.seed)
        return Response(
            content=text,
            media_type="application/sql",
            headers={
                "Content-Disposition": f'attachment; filename="synth_scale_seed{req.seed}.sql"'
            },
        )

    blob = service.csv_zip_bytes(schema, generated, order)
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="synth_scale_seed{req.seed}_csv.zip"'
        },
    )


@app.post("/api/waitlist")
def waitlist_add(req: WaitlistRequest, request: Request):
    email = req.email.strip().lower()
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="That doesn't look like a valid email address.")
    added, count = waitlist.add(email, request.headers.get("user-agent", ""))
    return {"ok": True, "added": added, "count": count}


@app.get("/api/waitlist/count")
def waitlist_count():
    return {"count": waitlist.count}


@app.post("/api/contact")
def contact_submit(req: ContactRequest, request: Request):
    allowed, retry_after = contact_limiter.check(client_ip(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Too many messages. Try again in ~{max(retry_after // 60, 1)} min."
            },
            headers={"Retry-After": str(retry_after)},
        )

    email = req.email.strip().lower()
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="That doesn't look like a valid email address.")
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message can't be empty.")
    name = req.name.strip()

    # Persisted first (durable) -- the email send below is best-effort on
    # top of that, so a transient SMTP hiccup never loses the message.
    contact_store.add(name, email, message, request.headers.get("user-agent", ""))
    contact.send_contact_email(name, email, message)
    return {"ok": True}


@app.post("/api/track")
def track(req: TrackRequest, request: Request):
    allowed, retry_after = track_limiter.check(client_ip(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limited."},
            headers={"Retry-After": str(retry_after)},
        )
    analytics_store.record(
        req.event, req.path, req.target, req.session_id, request.headers.get("user-agent", "")
    )
    return {"ok": True}


@app.post("/api/survey")
def survey_submit(req: SurveyRequest, request: Request):
    allowed, retry_after = survey_limiter.check(client_ip(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many submissions. Try again in ~{max(retry_after // 60, 1)} min."},
            headers={"Retry-After": str(retry_after)},
        )

    email = req.email.strip().lower()
    if email and not valid_email(email):
        raise HTTPException(status_code=422, detail="That doesn't look like a valid email address.")

    survey_store.add(
        req.would_use,
        req.use_case.strip(),
        req.blockers.strip(),
        email,
        request.headers.get("user-agent", ""),
    )
    return {"ok": True}


@app.get("/api/stats")
def stats(request: Request, x_stats_key: str = Header(default="")):
    """Aggregate site stats: pageviews, unique sessions, click counts by
    target, plus waitlist/contact counts. Gated behind SYNTH_SCALE_STATS_KEY
    (sent as a header, not a query param) so it 404s for everyone else --
    404 rather than 401/403 so the endpoint's existence isn't advertised."""
    allowed, retry_after = stats_limiter.check(client_ip(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limited."},
            headers={"Retry-After": str(retry_after)},
        )
    if not STATS_KEY or not secrets.compare_digest(x_stats_key, STATS_KEY):
        raise HTTPException(status_code=404)
    return {
        **analytics_store.stats(),
        "waitlist_count": waitlist.count,
        "contact_messages": contact_store.count(),
        "survey": survey_store.stats(),
    }


# Static frontend at / (mounted last so /api/* wins).
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
