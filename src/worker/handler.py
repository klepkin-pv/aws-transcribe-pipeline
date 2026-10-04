"""SQS worker: claims jobs and starts AWS Transcribe jobs.

Idempotency gate: claiming is a conditional write from created/uploading/
queued to processing, so duplicate queue deliveries lose the race and are
skipped instead of double-processing the job.

Transcription is started before the claim: job names are unique per job id,
so a retried message hits a Transcribe conflict and simply proceeds.

Failure taxonomy:
- unknown job / duplicate delivery    -> skipped (success)
- permanent errors (bad media format) -> job failed with a reason
- transient errors                    -> reported via ReportBatchItemFailures;
  SQS redelivers after the visibility timeout, which is the backoff
- after MAX_ATTEMPTS redeliveries     -> job failed, message no longer
  retried (a terminal job must not keep looping)
- poison messages without a job id    -> DLQ via the partial batch report
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3

from lib.providers.transcription import (
    TranscribeProvider,
    TranscriptionError,
    TranscriptionProvider,
)
from lib.settings import load_settings
from lib.storage import InvalidTransitionError, JobNotFoundError, JobsRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_MAX_ATTEMPTS = 3


@lru_cache(maxsize=1)
def _jobs_repo() -> JobsRepository:
    settings = load_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    return JobsRepository(dynamodb.Table(settings.jobs_table))


@lru_cache(maxsize=1)
def _transcription() -> TranscriptionProvider:
    settings = load_settings()
    return TranscribeProvider(
        boto3.client("transcribe", region_name=settings.aws_region),
        boto3.client("s3", region_name=settings.aws_region),
        settings.uploads_bucket,
        settings.transcribe_language,
    )


def _receive_count(record: dict) -> int:
    try:
        return int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
    except ValueError:
        return 1


def _mark_failed(repo: JobsRepository, job_id: str, reason: str) -> None:
    try:
        repo.transition(job_id, "failed", datetime.now(UTC), extra={"failure_reason": reason})
    except (InvalidTransitionError, JobNotFoundError):
        logger.info("Job %s is already terminal, not marking failed", job_id)


def handler(event: dict, context: Any) -> dict:
    repo = _jobs_repo()
    transcription = _transcription()
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record["body"])
            job_id = body["job_id"]
        except Exception:
            logger.exception("Dropping unparseable message %s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        try:
            repo.get_job(job_id)  # unknown jobs are skipped, not retried

            job_name = transcription.start_transcription(
                job_id, body["bucket"], body["object_key"]
            )
            repo.transition(job_id, "processing", datetime.now(UTC))
            repo.transition(job_id, "transcribing", datetime.now(UTC))
            logger.info("Started transcription %s for job %s", job_name, job_id)
        except (InvalidTransitionError, JobNotFoundError) as exc:
            # Duplicate delivery or an unknown job — not worth a retry.
            logger.info("Skipping message %s: %s", message_id, exc)
        except TranscriptionError as exc:
            # Permanent: retrying a bad media format never helps.
            logger.info("Job %s failed permanently: %s", job_id, exc)
            _mark_failed(repo, job_id, f"transcription: {exc}")
        except Exception as exc:
            attempts = _receive_count(record)
            if attempts >= _MAX_ATTEMPTS:
                logger.exception("Job %s failed after %s attempts", job_id, attempts)
                _mark_failed(repo, job_id, f"processing failed after {attempts} attempts: {exc}")
            else:
                # SQS redelivers after the visibility timeout — the backoff.
                logger.exception("Message %s failed, attempt %s", message_id, attempts)
                batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
