"""Tests for POST /api/survey (the "would you use this" feedback form)."""
from __future__ import annotations

import pytest

from app import main


def test_survey_submit_stores_response(client):
    r = client.post(
        "/api/survey",
        json={"would_use": "yes", "use_case": "CI fixtures", "blockers": "", "email": ""},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    line = main.survey_store.path.read_text(encoding="utf-8").strip()
    assert '"would_use": "yes"' in line
    assert '"use_case": "CI fixtures"' in line
    assert '"ts":' in line and '"ua":' in line


def test_survey_all_optional_fields_default_to_empty(client):
    r = client.post("/api/survey", json={"would_use": "no"})
    assert r.status_code == 200
    line = main.survey_store.path.read_text(encoding="utf-8").strip()
    assert '"use_case": ""' in line
    assert '"blockers": ""' in line
    assert '"email": ""' in line


@pytest.mark.parametrize("bad", ["yep", "Yes", "", "y", "true"])
def test_survey_rejects_invalid_would_use(client, bad):
    r = client.post("/api/survey", json={"would_use": bad})
    assert r.status_code == 422


def test_survey_requires_would_use(client):
    r = client.post("/api/survey", json={"use_case": "demos"})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "x @y.com", "a@@b.com"])
def test_survey_rejects_invalid_email_when_provided(client, bad):
    r = client.post("/api/survey", json={"would_use": "maybe", "email": bad})
    assert r.status_code == 422


def test_survey_accepts_empty_email(client):
    r = client.post("/api/survey", json={"would_use": "maybe", "email": ""})
    assert r.status_code == 200


def test_survey_has_own_rate_limit(client):
    body = {"would_use": "yes"}
    for _ in range(main.survey_limiter.limit):
        assert client.post("/api/survey", json=body).status_code == 200
    r = client.post("/api/survey", json=body)
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    # /api/contact's own limit is untouched.
    assert client.post("/api/contact", json={"email": "a@b.com", "message": "hi"}).status_code == 200


def test_stats_includes_survey_aggregates(client, monkeypatch):
    monkeypatch.setattr(main, "STATS_KEY", "correct-secret")

    client.post("/api/survey", json={"would_use": "yes", "use_case": "CI fixtures"})
    client.post("/api/survey", json={"would_use": "yes", "use_case": "CI fixtures"})
    client.post("/api/survey", json={"would_use": "maybe", "use_case": "demos"})
    client.post("/api/survey", json={"would_use": "no"})

    r = client.get("/api/stats", headers={"X-Stats-Key": "correct-secret"})
    assert r.status_code == 200
    survey = r.json()["survey"]
    assert survey["total"] == 4
    assert survey["would_use"] == {"yes": 2, "maybe": 1, "no": 1}
    assert survey["use_case_counts"] == {"CI fixtures": 2, "demos": 1}
