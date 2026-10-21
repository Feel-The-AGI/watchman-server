"""The suite should fail when a removed route silently returns, or a current one disappears."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.routes import cron


def test_removed_mutation_and_proposal_routes_are_gone():
    client = TestClient(create_app())
    assert client.get("/api/mutations").status_code == 404
    assert client.get("/api/mutations/pending").status_code == 404
    assert client.post("/api/proposals/parse", json={"text": "Add leave"}).status_code == 404
    assert client.post("/api/proposals/parse-pdf").status_code == 404
    assert client.post("/api/proposals/quick-add").status_code == 404


def test_cron_without_secret_is_not_open(monkeypatch):
    monkeypatch.setattr(cron.settings, "cron_secret", "")
    client = TestClient(create_app())
    missing = client.post("/api/cron/weekly-summary")
    wrong = client.post("/api/cron/weekly-summary", headers={"X-Cron-Secret": "dev-cron-secret"})
    assert missing.status_code == 503
    assert wrong.status_code == 503


def test_cron_rejects_a_wrong_secret(monkeypatch):
    monkeypatch.setattr(cron.settings, "cron_secret", "expected-cron-secret")
    client = TestClient(create_app())
    response = client.post("/api/cron/weekly-summary", headers={"X-Cron-Secret": "nope"})
    assert response.status_code == 401


def test_current_proposal_review_route_requires_auth():
    client = TestClient(create_app())
    pending = client.get("/api/commands/proposals/pending")
    review = client.post(
        "/api/commands/proposals/00000000-0000-0000-0000-000000000001/review",
        json={"decision": "approve"},
    )
    assert pending.status_code == 401
    assert review.status_code == 401
