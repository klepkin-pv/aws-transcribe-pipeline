"""Pipeline function tests on moto (DynamoDB + SQS)."""

import json
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from dispatcher import handler as dispatcher_module
from dispatcher.handler import handler as dispatch


def ts(minute: int) -> datetime:
    return datetime(2026, 9, 28, 12, minute, tzinfo=UTC)


def s3_event(bucket: str, key: str) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


@pytest.fixture
def pipeline(repo, monkeypatch):
    with mock_aws():
        sqs = boto3.client("sqs", region_name="eu-central-1")
        queue_url = sqs.create_queue(QueueName="jobs-test")["QueueUrl"]
        monkeypatch.setenv("JOBS_QUEUE_URL", queue_url)
        monkeypatch.setenv("UPLOADS_BUCKET", "uploads-test")
        monkeypatch.setattr(dispatcher_module, "_jobs_repo", lambda: repo)
        monkeypatch.setattr(dispatcher_module, "_sqs", lambda: sqs)
        yield {"repo": repo, "sqs": sqs, "queue_url": queue_url}


def _messages(pipeline: dict) -> list:
    response = pipeline["sqs"].receive_message(
        QueueUrl=pipeline["queue_url"], MaxNumberOfMessages=10
    )
    return response.get("Messages", [])


def test_dispatcher_enqueues_created_job(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    result = dispatch(s3_event("uploads-test", f"uploads/sub-1/{job['job_id']}/a.mp3"), {})

    assert result == {"enqueued": 1}
    messages = _messages(pipeline)
    assert len(messages) == 1
    body = json.loads(messages[0]["Body"])
    assert body["job_id"] == job["job_id"]
    assert body["object_key"] == f"uploads/sub-1/{job['job_id']}/a.mp3"
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "queued"


def test_dispatcher_skips_duplicate_event(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    event = s3_event("uploads-test", f"uploads/sub-1/{job['job_id']}/a.mp3")

    assert dispatch(event, {}) == {"enqueued": 1}
    assert dispatch(event, {}) == {"enqueued": 0}

    assert len(_messages(pipeline)) == 1


def test_dispatcher_skips_orphan_object(pipeline):
    result = dispatch(s3_event("uploads-test", "uploads/sub-1/unknown-id/a.mp3"), {})

    assert result == {"enqueued": 0}
    assert _messages(pipeline) == []


def test_dispatcher_skips_malformed_key(pipeline):
    assert dispatch(s3_event("uploads-test", "some/other.bin"), {}) == {"enqueued": 0}


def test_dispatcher_skips_foreign_bucket(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    result = dispatch(s3_event("other-bucket", f"uploads/sub-1/{job['job_id']}/a.mp3"), {})

    assert result == {"enqueued": 0}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "created"


def test_dispatcher_rolls_back_status_when_sqs_is_down(pipeline, monkeypatch):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))

    class BrokenSqs:
        def send_message(self, **kwargs):
            raise RuntimeError("sqs down")

    monkeypatch.setattr(dispatcher_module, "_sqs", lambda: BrokenSqs())

    with pytest.raises(RuntimeError):
        dispatch(s3_event("uploads-test", f"uploads/sub-1/{job['job_id']}/a.mp3"), {})

    assert pipeline["repo"].get_job(job["job_id"])["status"] == "created"
