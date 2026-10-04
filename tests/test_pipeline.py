"""Pipeline function tests on moto (DynamoDB + SQS)."""

import json
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from dispatcher import handler as dispatcher_module
from dispatcher.handler import handler as dispatch
from finalizer import handler as finalizer_module
from finalizer.handler import handler as finalize
from lib.providers.transcription import FakeTranscriptionProvider, TranscribeProvider
from worker import handler as worker_module
from worker.handler import handler as work


class StubScoring:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def score(self, transcript: str) -> dict:
        self.calls.append(transcript)
        return {"score": 87, "summary": "solid candidate"}


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
        scoring = StubScoring()
        monkeypatch.setenv("JOBS_QUEUE_URL", queue_url)
        monkeypatch.setenv("UPLOADS_BUCKET", "uploads-test")
        monkeypatch.setattr(dispatcher_module, "_jobs_repo", lambda: repo)
        monkeypatch.setattr(dispatcher_module, "_sqs", lambda: sqs)
        monkeypatch.setattr(worker_module, "_jobs_repo", lambda: repo)
        monkeypatch.setattr(worker_module, "_transcription", lambda: transcription)
        monkeypatch.setattr(finalizer_module, "_jobs_repo", lambda: repo)
        monkeypatch.setattr(finalizer_module, "_transcription", lambda: transcription)
        monkeypatch.setattr(finalizer_module, "_scoring", lambda: scoring)
        yield {
            "repo": repo,
            "sqs": sqs,
            "queue_url": queue_url,
            "transcription": transcription,
            "scoring": scoring,
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


def sqs_record(message_id: str, body, attributes: dict | None = None) -> dict:
    raw_body = body if isinstance(body, str) else json.dumps(body)
    record = {"messageId": message_id, "body": raw_body}
    if attributes:
        record["attributes"] = attributes
    return record


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


def test_worker_marks_unsupported_media_failed_without_retry(pipeline, monkeypatch):
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

    assert result == {"batchItemFailures": []}
    item = pipeline["repo"].get_job(job["job_id"])
    assert item["status"] == "failed"
    assert item["failure_reason"] == "transcription: unsupported media format: .txt"


def test_worker_retries_transient_errors_before_max_attempts(pipeline, monkeypatch):
    class FlakyTranscription(FakeTranscriptionProvider):
        def start_transcription(self, job_id: str, bucket: str, key: str) -> str:
            raise RuntimeError("transcribe down")

    monkeypatch.setattr(worker_module, "_transcription", lambda: FlakyTranscription())
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    pipeline["repo"].transition(job["job_id"], "queued", ts(1))
    key = f"uploads/sub-1/{job['job_id']}/a.mp3"

    result = work({"Records": [sqs_job_message("m1", job["job_id"], key)]}, {})

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "queued"


def test_worker_marks_failed_after_max_attempts(pipeline, monkeypatch):
    class FlakyTranscription(FakeTranscriptionProvider):
        def start_transcription(self, job_id: str, bucket: str, key: str) -> str:
            raise RuntimeError("transcribe down")

    monkeypatch.setattr(worker_module, "_transcription", lambda: FlakyTranscription())
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    pipeline["repo"].transition(job["job_id"], "queued", ts(1))
    key = f"uploads/sub-1/{job['job_id']}/a.mp3"

    record = sqs_job_message("m1", job["job_id"], key)
    record["attributes"] = {"ApproximateReceiveCount": "3"}

    result = work({"Records": [record]}, {})

    assert result == {"batchItemFailures": []}
    item = pipeline["repo"].get_job(job["job_id"])
    assert item["status"] == "failed"
    assert "processing failed after 3 attempts" in item["failure_reason"]


def transcribe_event(job_id: str, status: str, reason: str | None = None) -> dict:
    detail: dict = {"TranscriptionJobName": f"job-{job_id}", "TranscriptionJobStatus": status}
    if reason:
        detail["FailureReason"] = reason
    return {"detail": detail}


def test_finalizer_completes_job_with_results(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    for status in ("queued", "processing", "transcribing"):
        pipeline["repo"].transition(job["job_id"], status, ts(1))

    result = finalize(transcribe_event(job["job_id"], "COMPLETED"), {})

    assert result == {"status": "done", "score": 87}
    item = pipeline["repo"].get_job(job["job_id"])
    assert item["status"] == "done"
    assert item["score"] == 87
    assert item["summary"] == "solid candidate"
    assert pipeline["scoring"].calls == ["fake transcript text"]


def test_finalizer_marks_failed_on_transcribe_failure(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    for status in ("queued", "processing", "transcribing"):
        pipeline["repo"].transition(job["job_id"], status, ts(1))

    result = finalize(transcribe_event(job["job_id"], "FAILED", reason="bad audio"), {})

    assert result == {"status": "failed", "reason": "bad audio"}
    item = pipeline["repo"].get_job(job["job_id"])
    assert item["status"] == "failed"
    assert item["failure_reason"] == "bad audio"


def test_finalizer_skips_duplicate_event_after_done(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    for status in ("queued", "processing", "transcribing"):
        pipeline["repo"].transition(job["job_id"], status, ts(1))
    finalize(transcribe_event(job["job_id"], "COMPLETED"), {})

    result = finalize(transcribe_event(job["job_id"], "COMPLETED"), {})

    assert result == {"skipped": True}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "done"


def test_finalizer_marks_failed_when_scoring_fails(pipeline, monkeypatch):
    class BrokenScoring:
        def score(self, transcript: str) -> dict:
            raise RuntimeError("llm down")

    monkeypatch.setattr(finalizer_module, "_scoring", lambda: BrokenScoring())
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    for status in ("queued", "processing", "transcribing"):
        pipeline["repo"].transition(job["job_id"], status, ts(1))

    result = finalize(transcribe_event(job["job_id"], "COMPLETED"), {})

    assert result == {"status": "failed", "reason": "scoring failed"}
    item = pipeline["repo"].get_job(job["job_id"])
    assert item["status"] == "failed"
    assert item["failure_reason"] == "scoring failed"


def test_finalizer_resumes_job_stuck_in_scoring(pipeline):
    job = pipeline["repo"].create_job("sub-1", "a.mp3", "audio/mpeg", ts(0))
    for status in ("queued", "processing", "scoring"):
        pipeline["repo"].transition(job["job_id"], status, ts(1))

    result = finalize(transcribe_event(job["job_id"], "COMPLETED"), {})

    assert result == {"status": "done", "score": 87}
    assert pipeline["repo"].get_job(job["job_id"])["status"] == "done"


def test_finalizer_skips_foreign_and_unknown_jobs(pipeline):
    foreign = {
        "detail": {
            "TranscriptionJobName": "someone-elses-job",
            "TranscriptionJobStatus": "COMPLETED",
        }
    }

    assert finalize(foreign, {}) == {"skipped": True}
    assert finalize(transcribe_event("missing", "COMPLETED"), {}) == {"skipped": True}
