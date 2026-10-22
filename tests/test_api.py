"""Integration tests for the TriageBot HTTP API."""

from __future__ import annotations

import pytest


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_triage_preview_does_not_persist(client):
    resp = client.post("/triage", json={"title": "App crashes", "body": "500 error"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["severity"] == "high"
    assert "bug" in data["labels"]

    # Preview must not have created a stored issue.
    assert client.get("/issues").json() == []


def test_batch_triage(client):
    payload = [
        {"title": "Critical outage", "body": ""},
        {"title": "Fix typo in docs", "body": ""},
    ]
    resp = client.post("/triage/batch", json=payload)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    assert results[0]["severity"] == "urgent"
    assert results[1]["severity"] == "low"


def test_create_and_get_issue(client):
    resp = client.post("/issues", json={"title": "API 500 on login", "body": "server error"})
    assert resp.status_code == 201
    created = resp.json()
    assert created["id"] > 0
    assert created["severity"] in ("high", "urgent")
    assert created["owner"] == "backend-team"
    assert created["status"] == "open"

    fetched = client.get(f"/issues/{created['id']}").json()
    assert fetched["title"] == "API 500 on login"


def test_list_issues_newest_first(client):
    client.post("/issues", json={"title": "First issue"})
    client.post("/issues", json={"title": "Second issue"})
    issues = client.get("/issues").json()
    assert [i["title"] for i in issues] == ["Second issue", "First issue"]


def test_update_reruns_triage(client):
    created = client.post("/issues", json={"title": "Minor typo"}).json()
    assert created["severity"] == "low"

    updated = client.patch(
        f"/issues/{created['id']}",
        json={"title": "Critical production outage"},
    ).json()
    assert updated["severity"] == "urgent"


def test_update_status_only(client):
    created = client.post("/issues", json={"title": "Some bug"}).json()
    updated = client.patch(f"/issues/{created['id']}", json={"status": "closed"}).json()
    assert updated["status"] == "closed"


def test_delete_issue(client):
    created = client.post("/issues", json={"title": "Temp issue"}).json()
    assert client.delete(f"/issues/{created['id']}").status_code == 204
    assert client.get(f"/issues/{created['id']}").status_code == 404


def test_get_missing_issue_returns_404(client):
    assert client.get("/issues/9999").status_code == 404


def test_validation_rejects_empty_title(client):
    resp = client.post("/issues", json={"title": ""})
    assert resp.status_code == 422


def test_batch_triage_is_limited_to_100_items(client):
    # Regression (final audit): an unbounded batch let one request run the
    # classifier thousands of times.
    assert client.post("/triage/batch", json=[{"title": "x"}] * 100).status_code == 200
    assert client.post("/triage/batch", json=[{"title": "x"}] * 101).status_code == 422


# --- Cross-site request protection (pre-demo hardening) --------------------------------


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_write_bodies_must_be_json(client, content_type):
    """A cross-site "simple" request (no preflight) cannot carry a JSON content type."""
    headers = {"content-type": content_type} if content_type else {}
    resp = client.post("/issues", content=b'{"title": "x"}', headers=headers)
    assert resp.status_code == 415
    assert client.get("/issues").json() == []


def test_json_writes_and_bodyless_deletes_still_work(client):
    created = client.post("/issues", json={"title": "Real issue"})
    assert created.status_code == 201
    assert client.delete(f"/issues/{created.json()['id']}").status_code == 204


def test_security_headers(client):
    resp = client.get("/")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
