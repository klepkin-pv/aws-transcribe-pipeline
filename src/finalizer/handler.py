"""EventBridge handler for Transcribe Job State Change events.

COMPLETED -> score the transcript and finish the job (done, with results).
FAILED    -> mark the job failed with the Transcribe failure reason.
Everything else (foreign job names, unknown jobs, duplicate events) is
skipped. AWS-side errors (missing transcript, S3 outage) propagate so
EventBridge retries the delivery and finally parks the event in its DLQ.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Metrics

from lib.providers.scoring import BedrockScoringProvider, ScoringProvider
from lib.providers.transcription import (
    TranscribeProvider,
    TranscriptionError,
    TranscriptionProvider,
)
from lib.settings import load_settings
from lib.storage import InvalidTransitionError, JobNotFoundError, JobsRepository

logger = Logger(service="finalizer")
metrics = Metrics(namespace="TranscribePipeline", service="finalizer")

_NAME_PREFIX = "job-"


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


@lru_cache(maxsize=1)
def _scoring() -> ScoringProvider:
    settings = load_settings()
    return BedrockScoringProvider(
        boto3.client("bedrock-runtime", region_name=settings.aws_region),
        settings.bedrock_model_id,
    )


def _job_id(job_name: str) -> str | None:
    return job_name[len(_NAME_PREFIX):] if job_name.startswith(_NAME_PREFIX) else None


@logger.inject_lambda_context
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict, context: Any) -> dict:
    detail = event.get("detail", {})
    job_id = _job_id(detail.get("TranscriptionJobName", ""))
    transcribe_status = detail.get("TranscriptionJobStatus", "")
    if not job_id:
        logger.warning("Ignoring foreign transcription job: %s", detail.get("TranscriptionJobName"))
        return {"skipped": True}

    repo = _jobs_repo()
    now = datetime.now(UTC)
    try:
        job = repo.get_job(job_id)
    except JobNotFoundError:
        logger.warning("Transcription event for unknown job %s", job_id)
        return {"skipped": True}

    if job["status"] in ("done", "failed"):
        logger.info("Job %s already terminal (%s), skipping duplicate event", job_id, job["status"])
        return {"skipped": True}

    if transcribe_status != "COMPLETED":
        reason = detail.get("FailureReason") or f"transcription {transcribe_status or 'unknown'}"
        repo.transition(job_id, "failed", now, extra={"failure_reason": reason})
        logger.info("Job %s failed in transcription: %s", job_id, reason)
        metrics.add_metric(name="JobFailed", unit="Count", value=1)
        return {"status": "failed", "reason": reason}

    try:
        transcript = _transcription().transcript_text(detail["TranscriptionJobName"])
    except TranscriptionError as exc:
        # Transcribe finished without usable speech (or the output is damaged):
        # retrying cannot help, so the job fails right away.
        logger.info("Job %s failed in transcript read: %s", job_id, exc)
        repo.transition(job_id, "failed", now, extra={"failure_reason": str(exc)})
        metrics.add_metric(name="JobFailed", unit="Count", value=1)
        return {"status": "failed", "reason": str(exc)}

    try:
        repo.transition(job_id, "scoring", now)
    except InvalidTransitionError:
        # Resume: a previous attempt crashed between the scoring marker and done.
        pass

    try:
        result = _scoring().score(transcript)
    except Exception:
        logger.exception("Scoring failed for job %s", job_id)
        repo.transition(job_id, "failed", now, extra={"failure_reason": "scoring failed"})
        metrics.add_metric(name="JobFailed", unit="Count", value=1)
        return {"status": "failed", "reason": "scoring failed"}

    repo.transition(
        job_id,
        "done",
        now,
        extra={"score": result["score"], "summary": result["summary"]},
    )
    logger.info("Job %s done, score %s", job_id, result["score"])
    metrics.add_metric(name="JobDone", unit="Count", value=1)
    return {"status": "done", "score": result["score"]}
