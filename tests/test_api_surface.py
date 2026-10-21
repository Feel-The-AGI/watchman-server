"""The suite should fail when a removed route silently returns, or a current one disappears."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_removed_mutation_and_proposal_routes_are_gone():
    client = TestClient(create_app())
    assert client.get("/api/mutations").status_code == 404
    assert client.get("/api/mutations/pending").status_code == 404
    assert client.post("/api/proposals/parse", json={"text": "Add leave"}).status_code == 404
    assert client.post("/api/proposals/parse-pdf").status_code == 404
    assert client.post("/api/proposals/quick-add").status_code == 404


def test_current_proposal_review_route_requires_auth():
    client = TestClient(create_app())
    pending = client.get("/api/commands/proposals/pending")
    review = client.post(
        "/api/commands/proposals/00000000-0000-0000-0000-000000000001/review",
        json={"decision": "approve"},
    )
    assert pending.status_code == 401
    assert review.status_code == 401
