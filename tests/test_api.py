"""API tests. Cognito claims are injected the same way Mangum passes them in scope."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.handler import handler
from api.main import app, get_jobs_repo, get_now


class FakeJwtClaims:
    """ASGI middleware mimicking the scope entry Mangum creates for API Gateway events."""

    def __init__(self, app, claims: dict[str, str]) -> None:
        self.app = app
        self.claims = claims

    async def __call__(self, scope, receive, send) -> None:
        scope["aws.event"] = {
            "requestContext": {"authorizer": {"jwt": {"claims": self.claims}}}
        }
        await self.app(scope, receive, send)


@pytest.fixture
def clock():
    """Each POST gets a distinct minute so the newest-first order is deterministic."""
    state = {"minute": 0}

    def next_now() -> datetime:
        state["minute"] += 1
        return datetime(2026, 9, 28, 12, state["minute"], tzinfo=UTC)

    return next_now


@pytest.fixture
def client(repo, clock):
    app.dependency_overrides[get_jobs_repo] = lambda: repo
    app.dependency_overrides[get_now] = clock
    with TestClient(FakeJwtClaims(app, {"sub": "sub-1"})) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_create_job_returns_201(client):
    response = client.post(
        "/jobs", json={"filename": "interview.mp3", "content_type": "audio/mpeg"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["filename"] == "interview.mp3"
    assert "pk" not in body and "gsi1pk" not in body


def test_create_job_requires_filename(client):
    response = client.post("/jobs", json={"content_type": "audio/mpeg"})

    assert response.status_code == 422


def test_get_job_by_owner(client):
    created = client.post(
        "/jobs", json={"filename": "a.mp3", "content_type": "audio/mpeg"}
    ).json()

    response = client.get(f"/jobs/{created['job_id']}")

    assert response.status_code == 200
    assert response.json()["job_id"] == created["job_id"]


def test_get_job_missing_returns_404(client):
    assert client.get("/jobs/nope").status_code == 404


def test_get_job_of_another_user_is_404(client):
    created = client.post(
        "/jobs", json={"filename": "a.mp3", "content_type": "audio/mpeg"}
    ).json()

    stranger = TestClient(FakeJwtClaims(app, {"sub": "sub-2"}))

    assert stranger.get(f"/jobs/{created['job_id']}").status_code == 404


def test_list_jobs_newest_first(client):
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        client.post("/jobs", json={"filename": name, "content_type": "audio/mpeg"})

    body = client.get("/jobs").json()

    assert [item["filename"] for item in body["items"]] == ["c.mp3", "b.mp3", "a.mp3"]
    assert body["next_cursor"] is None


def test_list_jobs_pagination(client):
    for _ in range(3):
        client.post("/jobs", json={"filename": "a.mp3", "content_type": "audio/mpeg"})

    page1 = client.get("/jobs", params={"limit": 2}).json()
    page2 = client.get("/jobs", params={"limit": 2, "cursor": page1["next_cursor"]}).json()

    assert len(page1["items"]) == 2 and page1["next_cursor"]
    assert len(page2["items"]) == 1 and page2["next_cursor"] is None
    job_ids = {item["job_id"] for item in page1["items"] + page2["items"]}
    assert len(job_ids) == 3


def test_missing_authentication_returns_401(repo):
    app.dependency_overrides[get_jobs_repo] = lambda: repo
    try:
        with TestClient(app) as client:
            response = client.get("/jobs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_mangum_handler_end_to_end(repo):
    app.dependency_overrides[get_jobs_repo] = lambda: repo
    try:
        event = {
            "version": "2.0",
            "routeKey": "GET /jobs",
            "rawPath": "/jobs",
            "rawQueryString": "",
            "cookies": [],
            "headers": {},
            "requestContext": {
                "http": {
                    "method": "GET",
                    "path": "/jobs",
                    "protocol": "HTTP/1.1",
                    "sourceIp": "127.0.0.1",
                    "userAgent": "test",
                },
                "authorizer": {"jwt": {"claims": {"sub": "sub-1"}}},
            },
            "isBase64Encoded": False,
        }
        response = handler(event, {})
    finally:
        app.dependency_overrides.clear()

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["items"] == []
