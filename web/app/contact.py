"""Contact form: durable JSONL store + best-effort SMTP notification.

Every submission is written to disk first (same append-only JSONL pattern
as waitlist.py) so nothing is lost if SMTP isn't configured yet or a send
fails transiently -- email delivery is a notification on top of that
durable store, never the only copy of the message.

SMTP is opt-in via env vars (unset by default -- no credentials ship in
this repo):
  SYNTH_SCALE_SMTP_HOST, SYNTH_SCALE_SMTP_PORT (default 587)
  SYNTH_SCALE_SMTP_USER, SYNTH_SCALE_SMTP_PASS
  SYNTH_SCALE_CONTACT_TO (default abdullahk80808080@gmail.com)
A Gmail account works here using an App Password (requires 2-Step
Verification enabled on that account): SMTP_HOST=smtp.gmail.com,
SMTP_USER=<that gmail address>, SMTP_PASS=<16-char app password>.
"""
from __future__ import annotations

import json
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

MAX_NAME_LEN = 100
DEFAULT_CONTACT_TO = "abdullahk80808080@gmail.com"


def _clean_header_field(value: str) -> str:
    """Strip CR/LF so a display name can never inject extra email headers."""
    return "".join(ch for ch in value if ch not in "\r\n").strip()


class ContactStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def add(self, name: str, email: str, message: str, user_agent: str = "") -> None:
        entry = {
            "name": name,
            "email": email,
            "message": message,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ua": user_agent[:300],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with self._lock:
            text = self.path.read_text(encoding="utf-8")
        return sum(1 for line in text.splitlines() if line.strip())


def send_contact_email(name: str, email: str, message: str) -> bool:
    """Best-effort SMTP send. Returns False (not an exception) if SMTP isn't
    configured or the send fails -- the caller already persisted the message
    via ContactStore before calling this, so a False here just means "no
    notification went out yet", not "the message was lost"."""
    host = os.environ.get("SYNTH_SCALE_SMTP_HOST")
    user = os.environ.get("SYNTH_SCALE_SMTP_USER")
    password = os.environ.get("SYNTH_SCALE_SMTP_PASS")
    if not (host and user and password):
        return False

    to_addr = os.environ.get("SYNTH_SCALE_CONTACT_TO", DEFAULT_CONTACT_TO)
    port = int(os.environ.get("SYNTH_SCALE_SMTP_PORT", "587"))
    safe_name = _clean_header_field(name)[:MAX_NAME_LEN] or "Synth-Scale visitor"

    msg = EmailMessage()
    msg["Subject"] = f"Synth-Scale contact form: {safe_name}"
    msg["From"] = user
    msg["To"] = to_addr
    msg["Reply-To"] = email
    msg.set_content(f"From: {safe_name} <{email}>\n\n{message}")

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True
    except Exception:
        return False
