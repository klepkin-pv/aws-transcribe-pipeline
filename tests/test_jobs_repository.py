"""Repository tests against moto's DynamoDB. The fixture lives in conftest.py."""

from datetime import UTC, datetime

import pytest

from lib.storage import (
    InvalidTransitionError,
    JobAlreadyExistsError,
    JobNotFoundError,
)


def ts(minute: int) -> datetime:
    return datetime(2026, 9, 28, 12, minute, tzinfo=UTC)


def test_create_job(repo):
    item = repo.create_job("sub-1", "interview.mp3", "audio/mpeg", ts(0))

    assert item["pk"] == "USER#sub-1"
    assert item["sk"].startswith("JOB#2026-09-28T12:00")
    assert item["gsi1pk"] == f"JOB#{item['job_id']}"
    assert item["status"] == "created"


def test_create_job_sets_ttl(repo):
    item = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    expected = int(ts(0).timestamp()) + 30 * 24 * 3600
    assert int(item["expires_at"]) == expected


def test_get_job(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    assert repo.get_job(created["job_id"])["job_id"] == created["job_id"]


def test_get_job_not_found(repo):
    with pytest.raises(JobNotFoundError):
        repo.get_job("missing-job-id")


def test_exact_replay_rejected(repo):
    repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0), job_id="fixed-id")

    with pytest.raises(JobAlreadyExistsError):
        repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0), job_id="fixed-id")


def test_list_user_jobs_newest_first(repo):
    for minute in (0, 1, 2):
        repo.create_job("sub-1", f"f{minute}.mp3", "audio/mpeg", ts(minute))

    items, cursor = repo.list_user_jobs("sub-1")

    created_at = [item["created_at"] for item in items]
    assert created_at == sorted(created_at, reverse=True)
    assert cursor is None


def test_list_user_jobs_isolated_by_user(repo):
    repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    repo.create_job("sub-2", "b.mp3", "audio/mpeg", ts(1))

    items, _ = repo.list_user_jobs("sub-1")

    assert [item["user_sub"] for item in items] == ["sub-1"]


def test_list_pagination(repo):
    for minute in range(5):
        repo.create_job("sub-1", f"f{minute}.mp3", "audio/mpeg", ts(minute))

    page1, cursor = repo.list_user_jobs("sub-1", limit=2)
    page2, cursor = repo.list_user_jobs("sub-1", limit=2, cursor=cursor)
    page3, cursor = repo.list_user_jobs("sub-1", limit=2, cursor=cursor)

    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    assert cursor is None
    job_ids = [item["job_id"] for page in (page1, page2, page3) for item in page]
    assert len(set(job_ids)) == 5


def test_transition_updates_status(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    item = repo.transition(created["job_id"], "uploading", ts(1))

    assert item["status"] == "uploading"
    assert item["updated_at"] == ts(1).isoformat()
    assert repo.get_job(created["job_id"])["status"] == "uploading"


def test_transition_unknown_status_rejected(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    with pytest.raises(InvalidTransitionError):
        repo.transition(created["job_id"], "teleported", ts(1))


def test_transition_invalid_edge_rejected(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    with pytest.raises(InvalidTransitionError):
        repo.transition(created["job_id"], "done", ts(1))


def test_transition_stale_write_loses_race(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    repo.transition(created["job_id"], "uploading", ts(1))
    repo.transition(created["job_id"], "queued", ts(2))

    # A stale dispatcher replaying an old event tries created -> uploading
    # again; the conditional write must fail instead of corrupting the state.
    with pytest.raises(InvalidTransitionError):
        repo.transition(created["job_id"], "uploading", ts(3))


def test_failed_is_terminal(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    repo.transition(created["job_id"], "failed", ts(1))

    with pytest.raises(InvalidTransitionError):
        repo.transition(created["job_id"], "queued", ts(2))


def test_transition_extra_attributes(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    for status in ("queued", "processing", "transcribing", "scoring"):
        repo.transition(created["job_id"], status, ts(1))

    item = repo.transition(
        created["job_id"], "done", ts(2), extra={"score": 87, "summary": "good"}
    )

    assert item["status"] == "done"
    assert item["score"] == 87
    assert item["summary"] == "good"


def test_processing_reachable_from_created(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    item = repo.transition(created["job_id"], "processing", ts(1))

    assert item["status"] == "processing"


def test_revert_to_created_from_queued(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    repo.transition(created["job_id"], "queued", ts(1))

    item = repo.revert_to_created(created["job_id"], ts(2))

    assert item["status"] == "created"


def test_revert_to_created_rejects_other_states(repo):
    created = repo.create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    with pytest.raises(InvalidTransitionError):
        repo.revert_to_created(created["job_id"], ts(1))
