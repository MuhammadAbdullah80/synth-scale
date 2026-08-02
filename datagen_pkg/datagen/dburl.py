"""Small connection-string helpers shared by the CLI (--db-url / --from-db)
and the web playground's "Connect to Supabase" flow.

Two independent concerns:
  * normalize_db_url: SQLAlchemy 1.4+ dropped support for the bare
    ``postgres://`` scheme (NoSuchModuleError) -- but that's exactly what
    Heroku, Supabase's older docs/snippets, and most copy-pasted "connection
    string" examples still hand out. Silently rewriting it to
    ``postgresql://`` is the difference between "just works" and a confusing
    stack trace for the exact audience this tool is for.
  * redact_db_url: never let a password leak into a log line, an error
    message, or a UI toast. Used anywhere a connection string (or an
    exception that might quote one) could otherwise end up in output.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

# Schemes SQLAlchemy's postgres dialects will actually accept. Anything else
# passed to --db-url / --from-db is almost certainly a copy-paste mistake
# (or, on the web path, exactly what an SSRF/other-protocol probe looks like).
ALLOWED_SCHEMES = {
    "postgresql",
    "postgresql+psycopg2",
    "postgresql+psycopg",
    "postgres",  # normalized away below, listed for clarity
}


def normalize_db_url(db_url: str) -> str:
    """Rewrite a bare ``postgres://`` scheme to ``postgresql://`` (the only
    thing SQLAlchemy >=1.4 recognizes for the Postgres dialect); every other
    scheme passes through unchanged."""
    db_url = db_url.strip()
    if db_url.startswith("postgres://"):
        return "postgresql://" + db_url[len("postgres://"):]
    return db_url


def redact_db_url(db_url: str) -> str:
    """Return a display-safe form of a connection string: scheme, username,
    host, port and database survive; the password (if any) is replaced with
    ``***``. Falls back to a fixed placeholder if the string can't be parsed
    as a URL at all, so a malformed --db-url never gets echoed verbatim."""
    try:
        parts = urlsplit(db_url)
    except ValueError:
        return "<unparseable connection string>"
    if not parts.netloc:
        return "<unparseable connection string>"
    userinfo = ""
    if parts.username:
        userinfo = parts.username + (":***" if parts.password else "") + "@"
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{userinfo}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def mask_secret(text: str, secret: str | None) -> str:
    """Defense-in-depth: strip a known secret (e.g. a parsed-out password)
    out of an arbitrary error-message string before it's shown to a user or
    written anywhere. No-op if the secret is empty/None."""
    if not secret:
        return text
    return text.replace(secret, "***")
