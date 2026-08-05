"""Vercel entrypoint: exposes the same FastAPI `app` as `uvicorn app.main:app`,
just importable from where Vercel's Python runtime expects a function (this
file's directory).

Two source trees live outside this directory and are added to sys.path
rather than pip-installed:
  - web/app/            (this app's own code)
  - datagen_pkg/datagen/ (the engine `datagen` imports)
Vercel's dependency-install step (uv, invoked against web/api/requirements.txt)
only sees files inside this function's own directory, so a local-path pip
requirement pointing at a sibling directory (../../datagen_pkg) silently
fails there even though `includeFiles` in vercel.json bundles it into the
deployed output. Importing `datagen` as plain source via sys.path -- instead
of asking pip/uv to install it as a package -- sidesteps that entirely; only
datagen's actual PyPI dependencies (sqlglot, SQLAlchemy, etc., listed in
requirements.txt) need real installation.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "web"))
sys.path.insert(0, str(_REPO_ROOT / "datagen_pkg"))

from app.main import app  # noqa: E402
