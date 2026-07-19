"""Waitlist store: an append-only JSONL file, deduped by email.

Stored fields per entry: email, ts (UTC ISO), ua (user-agent string). No
other PII, no tracking. Appends are serialized behind a lock and written as
a single write() call so concurrent requests can't interleave lines.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MAX_EMAIL_LEN = 254


def valid_email(email: str) -> bool:
    return len(email) <= MAX_EMAIL_LEN and bool(EMAIL_RE.match(email))


class Waitlist:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._emails: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._emails.add(json.loads(line)["email"])
                except (json.JSONDecodeError, KeyError):
                    continue  # skip corrupt lines rather than dying

    @property
    def count(self) -> int:
        return len(self._emails)

    def add(self, email: str, user_agent: str = "") -> tuple[bool, int]:
        """Returns (added, count). Dedupes by normalized email."""
        email = email.strip().lower()
        with self._lock:
            if email in self._emails:
                return False, len(self._emails)
            entry = {
                "email": email,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ua": user_agent[:300],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
            self._emails.add(email)
            return True, len(self._emails)
