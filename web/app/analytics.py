"""Site analytics: durable JSONL event log. No cookies, no third-party
trackers, no IP storage.

Two event kinds are recorded, both fired by the client via POST /api/track:
  - "pageview": one per page load.
  - "click": one per click on anything tagged data-track="..." in the HTML
    (nav links, the pip-install chip, Generate, downloads, waitlist/contact
    submit buttons, the GitHub link, ...).

The client generates its own random session id and keeps it in
sessionStorage (not a cookie -- it's gone as soon as the tab closes, isn't
sent to any third party, and exists purely so /api/stats can report "unique
sessions" instead of raw hit counts). Same append-only JSONL pattern as
waitlist.py / contact.py: one write() per event, no read-modify-write race.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_PATH_LEN = 200
MAX_TARGET_LEN = 100
MAX_SESSION_LEN = 64


class AnalyticsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, event: str, path: str, target: str, session_id: str, user_agent: str) -> None:
        entry = {
            "event": event,
            "path": (path or "/")[:MAX_PATH_LEN],
            "target": (target or "")[:MAX_TARGET_LEN],
            "session": (session_id or "")[:MAX_SESSION_LEN],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ua": user_agent[:300],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()

    def stats(self) -> dict:
        """Aggregate the whole log on demand. Only /api/stats (key-gated,
        rarely called) reads this, so an O(n) scan is the simplest correct
        thing -- no separate counters to keep in sync, no risk of drifting
        from what's actually on disk."""
        pageviews = 0
        sessions: set[str] = set()
        clicks: Counter = Counter()
        pages: Counter = Counter()
        if self.path.exists():
            with self._lock:
                text = self.path.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = e.get("session") or ""
                if sid:
                    sessions.add(sid)
                if e.get("event") == "pageview":
                    pageviews += 1
                    pages[e.get("path") or "/"] += 1
                elif e.get("event") == "click":
                    clicks[e.get("target") or "unknown"] += 1
        return {
            "pageviews": pageviews,
            "unique_sessions": len(sessions),
            "clicks_by_target": dict(clicks.most_common()),
            "pageviews_by_path": dict(pages.most_common()),
        }
