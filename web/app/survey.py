"""Feedback survey: "would you actually use this?" plus why/why-not, stored
durably alongside waitlist/contact/analytics.

Same append-only JSONL pattern as waitlist.py / contact.py. Kept to a small,
fixed set of questions on purpose -- this exists to answer one thing (would
people use Synth-Scale, and what's stopping them), not to become a general
survey builder.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

WOULD_USE_VALUES = ("yes", "maybe", "no")
MAX_TEXT_LEN = 1000


class SurveyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(
        self,
        would_use: str,
        use_case: str,
        blockers: str,
        email: str,
        user_agent: str = "",
    ) -> None:
        entry = {
            "would_use": would_use,
            "use_case": use_case,
            "blockers": blockers,
            "email": email,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ua": user_agent[:300],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()

    def stats(self) -> dict:
        """Aggregate the whole log on demand -- same tradeoff as
        AnalyticsStore.stats(): only /api/stats reads this, so an O(n) scan
        beats maintaining separate counters that could drift from disk."""
        would_use_counts = {v: 0 for v in WOULD_USE_VALUES}
        use_case_counts: dict[str, int] = {}
        total = 0
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
                total += 1
                wu = e.get("would_use")
                if wu in would_use_counts:
                    would_use_counts[wu] += 1
                uc = (e.get("use_case") or "").strip()
                if uc:
                    use_case_counts[uc] = use_case_counts.get(uc, 0) + 1
        return {
            "total": total,
            "would_use": would_use_counts,
            "use_case_counts": dict(
                sorted(use_case_counts.items(), key=lambda kv: kv[1], reverse=True)
            ),
        }
