"""S3 ObjectCreated -> SQS dispatcher.

The dispatcher validates the object key, moves the job to `queued` with a
conditional write and enqueues exactly one message per job: duplicate S3
events lose the conditional-write race and are skipped instead of being
double-enqueued.

Business-level skips (bad key, orphan object, duplicate event) are logged
and reported as success; only infrastructure failures raise and let S3
retry the event.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import unquote_plus

import boto3
from aws_lambda_powertools import Logger, Metrics

from lib.settings import load_settings
from lib.storage import InvalidTransitionError, JobNotFoundError, JobsRepository

logger = Logger(service="dispatcher")
metrics = Metrics(namespace="TranscribePipeline", service="dispatcher")


@lru_cache(maxsize=1)
def _jobs_repo() -> JobsRepository:
    settings = load_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    return JobsRepository(dynamodb.Table(settings.jobs_table))


@lru_cache(maxsize=1)
def _sqs() -> Any:
    settings = load_settings()
    return boto3.client("sqs", region_name=settings.aws_region)


def _object_job_id(key: str) -> str | None:
    """Extract the job id from `uploads/<sub>/<job_id>/<filename>`."""
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "uploads" or not parts[2]:
        return None
    return parts[2]


def _enqueue(repo: JobsRepository, sqs: Any, job_id: str, bucket: str, key: str) -> None:
    try:
        repo.transition(job_id, "queued", datetime.now(UTC))
    except InvalidTransitionError:
        # A concurrent dispatcher invocation already enqueued this job.
        return

    try:
        sqs.send_message(
            QueueUrl=load_settings().jobs_queue_url,
            MessageBody=json.dumps({"job_id": job_id, "bucket": bucket, "object_key": key}),
        )
    except Exception:
        # Roll the status back so a retried S3 event can enqueue the job again.
        repo.revert_to_created(job_id, datetime.now(UTC))
        raise

    metrics.add_metric(name="JobEnqueued", unit="Count", value=1)


@logger.inject_lambda_context
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context: Any) -> dict:
    repo = _jobs_repo()
    sqs = _sqs()
    settings = load_settings()
    enqueued = 0

    for record in event.get("Records", []):
        bucket = record.get("s3", {}).get("bucket", {}).get("name", "")
        key = unquote_plus(record.get("s3", {}).get("object", {}).get("key", ""))

        if bucket != settings.uploads_bucket:
            logger.warning("Skipping event for unexpected bucket: %s", bucket)
            continue
        job_id = _object_job_id(key)
        if job_id is None:
            logger.warning("Skipping object outside the job layout: %s", key)
            continue

        try:
            job = repo.get_job(job_id)
        except JobNotFoundError:
            # Upload without a job record (or an expired one) — nothing to run.
            logger.warning("Skipping upload without a job: %s", key)
            continue

        if job["status"] not in ("created", "uploading"):
            logger.info("Job %s is already %s, skipping", job_id, job["status"])
            continue

        _enqueue(repo, sqs, job_id, bucket, key)
        enqueued += 1

    return {"enqueued": enqueued}
