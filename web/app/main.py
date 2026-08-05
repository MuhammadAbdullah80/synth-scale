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
- No auth, no accounts, no cookies, no analytics. The only stored data is
  the waitlist JSONL (email, timestamp, user-agent).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import contact, service
from .ratelimit import SlidingWindowLimiter
from .waitlist import Waitlist, valid_email

try:
    # service_db itself only imports SQLAlchemy (a base dependency, always
    # present) -- the actual Postgres driver is loaded lazily by SQLAlchemy
    # at connect time, so importing service_db alone would succeed even
    # without the [postgres] extra. Check for psycopg2 explicitly so a
    # deploy missing it gets a clean 503 up front instead of every request
    # failing deep inside a connection attempt.
    import psycopg2  # noqa: F401

    from . import service_db
    DB_CONNECT_AVAILABLE = True
except ImportError:
    service_db = None
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

limiter = SlidingWindowLimiter(limit=RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
db_limiter = SlidingWindowLimiter(limit=DB_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
contact_limiter = SlidingWindowLimiter(limit=CONTACT_RATE_LIMIT, window_seconds=RATE_WINDOW_SECONDS)
waitlist = Waitlist(DATA_DIR / "waitlist.jsonl")
contact_store = contact.ContactStore(DATA_DIR / "contact_messages.jsonl")

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


# Static frontend at / (mounted last so /api/* wins).
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
