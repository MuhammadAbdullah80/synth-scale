"""Tests for POST /api/track and GET /api/stats."""
from __future__ import annotations

import pytest

from app import main


def test_track_pageview_stores_entry(client):
    r = client.post(
        "/api/track",
        json={"event": "pageview", "path": "/", "session_id": "sess-1"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    line = main.analytics_store.path.read_text(encoding="utf-8").strip()
    assert '"event": "pageview"' in line
    assert '"session": "sess-1"' in line
    assert '"ts":' in line and '"ua":' in line


def test_track_click_stores_target(client):
    r = client.post(
        "/api/track",
        json={"event": "click", "path": "/", "target": "pip-install-copy", "session_id": "sess-1"},
    )
    assert r.status_code == 200
    line = main.analytics_store.path.read_text(encoding="utf-8").strip()
    assert '"target": "pip-install-copy"' in line


def test_track_rejects_unknown_event(client):
    r = client.post("/api/track", json={"event": "bogus"})
    assert r.status_code == 422


def test_track_defaults_are_safe(client):
    # No path/target/session_id supplied -- should not 500.
    r = client.post("/api/track", json={"event": "pageview"})
    assert r.status_code == 200


def test_track_has_own_rate_limit(client):
    for _ in range(main.track_limiter.limit):
        assert client.post("/api/track", json={"event": "pageview"}).status_code == 200
    r = client.post("/api/track", json={"event": "pageview"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_stats_404s_without_key_configured(client):
    # SYNTH_SCALE_STATS_KEY is unset in the test environment by default.
    assert main.STATS_KEY == ""
    r = client.get("/api/stats")
    assert r.status_code == 404


def test_stats_404s_with_wrong_key(client, monkeypatch):
    monkeypatch.setattr(main, "STATS_KEY", "correct-secret")
    r = client.get("/api/stats", headers={"X-Stats-Key": "wrong"})
    assert r.status_code == 404


def test_stats_aggregates_events_and_counts(client, monkeypatch):
    monkeypatch.setattr(main, "STATS_KEY", "correct-secret")

    client.post("/api/track", json={"event": "pageview", "path": "/", "session_id": "a"})
    client.post("/api/track", json={"event": "pageview", "path": "/", "session_id": "b"})
    client.post("/api/track", json={"event": "click", "path": "/", "target": "generate", "session_id": "a"})
    client.post("/api/waitlist", json={"email": "stats@example.com"})
    client.post("/api/contact", json={"email": "stats@example.com", "message": "hi"})

    r = client.get("/api/stats", headers={"X-Stats-Key": "correct-secret"})
    assert r.status_code == 200
    data = r.json()
    assert data["pageviews"] == 2
    assert data["unique_sessions"] == 2
    assert data["clicks_by_target"] == {"generate": 1}
    assert data["waitlist_count"] == 1
    assert data["contact_messages"] == 1


def test_stats_has_own_rate_limit_and_is_independent_of_track(client, monkeypatch):
    monkeypatch.setattr(main, "STATS_KEY", "correct-secret")
    for _ in range(main.stats_limiter.limit):
        assert client.get("/api/stats", headers={"X-Stats-Key": "correct-secret"}).status_code == 200
    r = client.get("/api/stats", headers={"X-Stats-Key": "correct-secret"})
    assert r.status_code == 429
    # /api/track's own limit is untouched.
    assert client.post("/api/track", json={"event": "pageview"}).status_code == 200
