"""Optional Postgres-backed persistence for waitlist / contact / analytics /
survey responses.

By default this app persists to JSONL files under SYNTH_SCALE_DATA_DIR,
which is fine on a platform with a real persistent filesystem (Render, Fly,
Railway, local/Docker) but NOT on Vercel, where only /tmp is writable and
that is not guaranteed to survive between invocations.

Setting SYNTH_SCALE_STORAGE_DB_URL to a Postgres connection string (a
Supabase Session/Transaction Pooler string works well: IPv4, free on every
tier, built for exactly this many-short-connections pattern) switches all
four stores to Postgres tables instead, auto-created on first use. This is
a separate, operator-owned connection string from the one end users paste
into "Connect to Supabase" on the playground -- that one is per-request and
never stored (see service_db.py / dbsafety.py); this one is long-lived
config, read once from the environment, and is where WE keep OUR OWN app
data. It is never exposed to a request or a caller.

Every call opens a short-lived connection and closes it -- there's no
long-lived pool to manage, which is the right shape for a serverless
function that may get one request per cold start. Table names are
`ss_`-prefixed so this can safely share a Supabase project with anything
else (e.g. a demo schema used to test the playground's DB-connect feature)
without colliding.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import psycopg2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ss_waitlist (
    email TEXT PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    ua TEXT
);
CREATE TABLE IF NOT EXISTS ss_contact_messages (
    id BIGSERIAL PRIMARY KEY,
    name TEXT,
    email TEXT NOT NULL,
    message TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    ua TEXT
);
CREATE TABLE IF NOT EXISTS ss_analytics_events (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    path TEXT,
    target TEXT,
    session TEXT,
    ts TIMESTAMPTZ NOT NULL,
    ua TEXT
);
CREATE INDEX IF NOT EXISTS ss_analytics_events_event_idx ON ss_analytics_events (event);
CREATE TABLE IF NOT EXISTS ss_survey (
    id BIGSERIAL PRIMARY KEY,
    would_use TEXT NOT NULL,
    use_case TEXT,
    blockers TEXT,
    email TEXT,
    ts TIMESTAMPTZ NOT NULL,
    ua TEXT
);
"""


def _connect(db_url: str):
    return psycopg2.connect(db_url, connect_timeout=5)


def ensure_schema(db_url: str) -> None:
    conn = _connect(db_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_SCHEMA)
    finally:
        conn.close()


class WaitlistDB:
    """Same public interface as waitlist.Waitlist, backed by Postgres."""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        conn = _connect(self.db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ss_waitlist")
                return cur.fetchone()[0]
        finally:
            conn.close()

    def add(self, email: str, user_agent: str = "") -> tuple[bool, int]:
        email = email.strip().lower()
        conn = _connect(self.db_url)
        try:
            with self._lock, conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ss_waitlist (email, ts, ua) VALUES (%s, %s, %s) "
                    "ON CONFLICT (email) DO NOTHING",
                    (email, datetime.now(timezone.utc), user_agent[:300]),
                )
                added = cur.rowcount > 0
                cur.execute("SELECT COUNT(*) FROM ss_waitlist")
                count = cur.fetchone()[0]
            return added, count
        finally:
            conn.close()


class ContactDB:
    """Same public interface as contact.ContactStore, backed by Postgres."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    def add(self, name: str, email: str, message: str, user_agent: str = "") -> None:
        conn = _connect(self.db_url)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ss_contact_messages (name, email, message, ts, ua) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, email, message, datetime.now(timezone.utc), user_agent[:300]),
                )
        finally:
            conn.close()

    def count(self) -> int:
        conn = _connect(self.db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ss_contact_messages")
                return cur.fetchone()[0]
        finally:
            conn.close()


class AnalyticsDB:
    """Same public interface as analytics.AnalyticsStore, backed by Postgres."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    def record(self, event: str, path: str, target: str, session_id: str, user_agent: str) -> None:
        conn = _connect(self.db_url)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ss_analytics_events (event, path, target, session, ts, ua) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        event,
                        (path or "/")[:200],
                        (target or "")[:100],
                        (session_id or "")[:64],
                        datetime.now(timezone.utc),
                        user_agent[:300],
                    ),
                )
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = _connect(self.db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ss_analytics_events WHERE event = 'pageview'")
                pageviews = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(DISTINCT session) FROM ss_analytics_events "
                    "WHERE session IS NOT NULL AND session <> ''"
                )
                unique_sessions = cur.fetchone()[0]
                cur.execute(
                    "SELECT target, COUNT(*) FROM ss_analytics_events WHERE event = 'click' "
                    "GROUP BY target ORDER BY COUNT(*) DESC"
                )
                clicks_by_target = dict(cur.fetchall())
                cur.execute(
                    "SELECT path, COUNT(*) FROM ss_analytics_events WHERE event = 'pageview' "
                    "GROUP BY path ORDER BY COUNT(*) DESC"
                )
                pageviews_by_path = dict(cur.fetchall())
            return {
                "pageviews": pageviews,
                "unique_sessions": unique_sessions,
                "clicks_by_target": clicks_by_target,
                "pageviews_by_path": pageviews_by_path,
            }
        finally:
            conn.close()


class SurveyDB:
    """Same public interface as survey.SurveyStore, backed by Postgres."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    def add(self, would_use: str, use_case: str, blockers: str, email: str, user_agent: str = "") -> None:
        conn = _connect(self.db_url)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ss_survey (would_use, use_case, blockers, email, ts, ua) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (would_use, use_case, blockers, email, datetime.now(timezone.utc), user_agent[:300]),
                )
        finally:
            conn.close()

    def stats(self) -> dict:
        from .survey import WOULD_USE_VALUES

        conn = _connect(self.db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ss_survey")
                total = cur.fetchone()[0]
                cur.execute("SELECT would_use, COUNT(*) FROM ss_survey GROUP BY would_use")
                rows = dict(cur.fetchall())
                would_use = {v: rows.get(v, 0) for v in WOULD_USE_VALUES}
                cur.execute(
                    "SELECT use_case, COUNT(*) FROM ss_survey WHERE use_case IS NOT NULL AND use_case <> '' "
                    "GROUP BY use_case ORDER BY COUNT(*) DESC"
                )
                use_case_counts = dict(cur.fetchall())
            return {"total": total, "would_use": would_use, "use_case_counts": use_case_counts}
        finally:
            conn.close()
