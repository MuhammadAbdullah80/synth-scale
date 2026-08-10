import sys
from pathlib import Path

# Make `app` importable regardless of where pytest is invoked from.
WEB_DIR = Path(__file__).resolve().parents[1]
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))

import pytest  # noqa: E402

from app import main  # noqa: E402
from app.analytics import AnalyticsStore  # noqa: E402
from app.contact import ContactStore  # noqa: E402
from app.survey import SurveyStore  # noqa: E402
from app.waitlist import Waitlist  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Fresh rate-limit windows and throwaway waitlist/contact/analytics/survey
    files per test."""
    main.limiter.reset()
    main.db_limiter.reset()
    main.contact_limiter.reset()
    main.track_limiter.reset()
    main.survey_limiter.reset()
    main.stats_limiter.reset()
    monkeypatch.setattr(main, "waitlist", Waitlist(tmp_path / "waitlist.jsonl"))
    monkeypatch.setattr(main, "contact_store", ContactStore(tmp_path / "contact_messages.jsonl"))
    monkeypatch.setattr(main, "analytics_store", AnalyticsStore(tmp_path / "analytics.jsonl"))
    monkeypatch.setattr(main, "survey_store", SurveyStore(tmp_path / "survey.jsonl"))
    yield
    main.limiter.reset()
    main.db_limiter.reset()
    main.contact_limiter.reset()
    main.track_limiter.reset()
    main.survey_limiter.reset()
    main.stats_limiter.reset()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c
