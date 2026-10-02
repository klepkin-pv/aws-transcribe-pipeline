"""SQS worker: claims jobs and (from the next step) starts transcription.

Idempotency gate: claiming is a conditional write from created/uploading/
queued to processing, so duplicate queue deliveries lose the race and are
skipped instead of double-processing the job.

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

from lib.settings import load_settings
from lib.storage import InvalidTransitionError, JobNotFoundError, JobsRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@lru_cache(maxsize=1)
def _jobs_repo() -> JobsRepository:
    settings = load_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    return JobsRepository(dynamodb.Table(settings.jobs_table))


def handler(event: dict, context: Any) -> dict:
    repo = _jobs_repo()
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record["body"])
            repo.transition(body["job_id"], "processing", datetime.now(UTC))
            # Next step: start the transcription job, then mark transcribing.
            logger.info("Claimed job %s for processing", body["job_id"])
        except (InvalidTransitionError, JobNotFoundError) as exc:
            # Duplicate delivery or an unknown job — not worth a retry.
            logger.info("Skipping message %s: %s", message_id, exc)
        except Exception:
            logger.exception("Failed to process message %s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
