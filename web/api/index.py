"""Vercel entrypoint: exposes the same FastAPI `app` as `uvicorn app.main:app`,
just importable from where Vercel's Python runtime expects a function (this
file's directory). Vercel bundles the repo, but `app/` lives one level up
(`web/app/`) rather than next to this file, so it's added to sys.path before
import rather than duplicating main.py's code here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402
