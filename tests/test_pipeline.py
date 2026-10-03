"""Pipeline function tests on moto (DynamoDB + SQS)."""

import json
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from dispatcher import handler as dispatcher_module
from dispatcher.handler import handler as dispatch
from lib.providers.transcription import FakeTranscriptionProvider, TranscribeProvider
from worker import handler as worker_module
from worker.handler import handler as work


def ts(minute: int) -> datetime:
    return datetime(2026, 9, 28, 12, minute, tzinfo=UTC)


def s3_event(bucket: str, key: str) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


@pytest.fixture
def pipeline(repo, monkeypatch):
    with mock_aws():
        sqs = boto3.client("sqs", region_name="eu-central-1")
        queue_url = sqs.create_queue(QueueName="jobs-test")["QueueUrl"]
        transcription = FakeTranscriptionProvider()
        monkeypatch.setenv("JOBS_QUEUE_URL", queue_url)
        monkeypatch.setenv("UPLOADS_BUCKET", "uploads-test")
        monkeypatch.setattr(dispatcher_module, "_jobs_repo", lambda: repo)
        monkeypatch.setattr(dispatcher_module, "_sqs", lambda: sqs)
        monkeypatch.setattr(worker_module, "_jobs_repo", lambda: repo)
        monkeypatch.setattr(worker_module, "_transcription", lambda: transcription)
        yield {
            "repo": repo,
            "sqs": sqs,
            "queue_url": queue_url,
            "transcription": transcription,
        }


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


def sqs_record(message_id: str, body) -> dict:
    raw_body = body if isinstance(body, str) else json.dumps(body)
    return {"messageId": message_id, "body": raw_body}


def sqs_job_message(message_id: str, job_id: str, key: str) -> dict:
    return sqs_record(message_id, {"job_id": job_id, "bucket": "uploads-test", "object_key": key})


def test_worker_starts_transcription(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    pipeline["repo"].transition(job["job_id"], "queued", ts(1))
    key = f"uploads/sub-1/{job['job_id']}/a.mp3"

    result = work({"Records": [sqs_job_message("m1", job["job_id"], key)]}, {})

    assert result == {"batchItemFailures": []}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "transcribing"
    assert pipeline["transcription"].started == [(job["job_id"], "uploads-test", key)]


def test_worker_duplicate_delivery_in_batch_is_skipped(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    pipeline["repo"].transition(job["job_id"], "queued", ts(1))
    key = f"uploads/sub-1/{job['job_id']}/a.mp3"

    event = {
        "Records": [
            sqs_job_message("m1", job["job_id"], key),
            sqs_job_message("m2", job["job_id"], key),
        ]
    }
    result = work(event, {})

    assert result == {"batchItemFailures": []}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "transcribing"


def test_worker_reports_poison_message(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    pipeline["repo"].transition(job["job_id"], "queued", ts(1))
    key = f"uploads/sub-1/{job['job_id']}/a.mp3"

    event = {
        "Records": [
            sqs_job_message("good", job["job_id"], key),
            sqs_record("poison", "not-json"),
        ]
    }
    result = work(event, {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "poison"}]}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "transcribing"


def test_worker_skips_unknown_job_without_starting_transcription(pipeline):
    result = work({"Records": [sqs_job_message("m1", "missing", "uploads/s/m/a.mp3")]}, {})

    assert result == {"batchItemFailures": []}
    assert pipeline["transcription"].started == []


def test_worker_reports_unsupported_media(pipeline, monkeypatch):
    # The real provider validates the media format before any AWS call,
    # so None clients are safe here.
    monkeypatch.setattr(
        worker_module,
        "_transcription",
        lambda: TranscribeProvider(None, None, "uploads-test", "ru-RU"),
    )
    job = pipeline["repo"].create_job("sub-1", "notes.txt", "text/plain", ts(0))
    pipeline["repo"].transition(job["job_id"], "queued", ts(1))
    key = f"uploads/sub-1/{job['job_id']}/notes.txt"

    result = work({"Records": [sqs_job_message("m1", job["job_id"], key)]}, {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "queued"
