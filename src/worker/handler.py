"""SQS worker: claims jobs and starts AWS Transcribe jobs.

Idempotency gate: claiming is a conditional write from created/uploading/
queued to processing, so duplicate queue deliveries lose the race and are
skipped instead of double-processing the job.

Transcription is started before the claim: job names are unique per job id,
so a retried message hits a Transcribe conflict and simply proceeds.

Unknown jobs and duplicate deliveries are skipped (success). Only real
processing failures are reported via ReportBatchItemFailures, so a poison
message still reaches the DLQ through the redrive policy while the rest of
the batch succeeds.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3

from lib.providers.transcription import TranscribeProvider, TranscriptionProvider
from lib.settings import load_settings
from lib.storage import InvalidTransitionError, JobNotFoundError, JobsRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


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


def handler(event: dict, context: Any) -> dict:
    repo = _jobs_repo()
    transcription = _transcription()
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record["body"])
            job_id = body["job_id"]
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
        except Exception:
            logger.exception("Failed to process message %s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
