"""FastAPI application served by the `api` Lambda function.

The user identity comes from the Cognito JWT validated by the API Gateway
authorizer; Mangum exposes the original Lambda event as scope["aws.event"].
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

import boto3
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lib.settings import load_settings
from lib.storage import (
    InvalidTransitionError,
    JobAlreadyExistsError,
    JobItem,
    JobNotFoundError,
    JobsRepository,
)
from lib.uploads import object_key, presigned_upload_url

app = FastAPI(
    title="aws-transcribe-pipeline",
    description="Async transcription and scoring pipeline API",
)


@lru_cache(maxsize=1)
def _repository() -> JobsRepository:
    settings = load_settings()
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    return JobsRepository(dynamodb.Table(settings.jobs_table))


def get_jobs_repo() -> JobsRepository:
    return _repository()


@lru_cache(maxsize=1)
def _s3_client() -> Any:
    settings = load_settings()
    return boto3.client("s3", region_name=settings.aws_region)


def get_s3_client() -> Any:
    return _s3_client()


def get_now() -> datetime:
    """Injectable clock: deterministic in tests, wall clock in production."""
    return datetime.now(UTC)


def get_current_sub(request: Request) -> str:
    claims = (
        request.scope.get("aws.event", {})
        .get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    sub = claims.get("sub") if claims else None
    if not sub:
        raise HTTPException(status_code=401, detail="Missing or invalid authentication")
    return sub


RepoDep = Annotated[JobsRepository, Depends(get_jobs_repo)]
SubDep = Annotated[str, Depends(get_current_sub)]
NowDep = Annotated[datetime, Depends(get_now)]
S3Dep = Annotated[Any, Depends(get_s3_client)]


class CreateJobRequest(BaseModel):
    # No path separators, no leading dots: the filename lands in the S3 key.
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    content_type: str = Field(min_length=1, max_length=100)


class JobResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    content_type: str
    created_at: str
    updated_at: str
    score: int | None = None
    summary: str | None = None
    failure_reason: str | None = None


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None = None


class JobCreatedResponse(JobResponse):
    upload_url: str
    object_key: str


def _to_response(item: JobItem) -> JobResponse:
    return JobResponse(
        job_id=item["job_id"],
        status=item["status"],
        filename=item["filename"],
        content_type=item["content_type"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        score=item.get("score"),
        summary=item.get("summary"),
        failure_reason=item.get("failure_reason"),
    )


@app.exception_handler(JobNotFoundError)
def _job_not_found_handler(request: Request, exc: JobNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Job not found"})


@app.exception_handler(JobAlreadyExistsError)
def _job_already_exists_handler(request: Request, exc: JobAlreadyExistsError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "Duplicate job"})


@app.exception_handler(InvalidTransitionError)
def _invalid_transition_handler(request: Request, exc: InvalidTransitionError) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": "Job status does not allow this transition"}
    )


@app.post("/jobs", response_model=JobCreatedResponse, status_code=201)
def create_job(
    payload: CreateJobRequest, repo: RepoDep, sub: SubDep, now: NowDep, s3: S3Dep
) -> JobCreatedResponse:
    item = repo.create_job(sub, payload.filename, payload.content_type, now)
    settings = load_settings()
    upload_url = presigned_upload_url(
        s3, settings.uploads_bucket, sub, item["job_id"], payload.filename, payload.content_type
    )
    job = _to_response(item)
    return JobCreatedResponse(
        **job.model_dump(),
        upload_url=upload_url,
        object_key=object_key(sub, item["job_id"], payload.filename),
    )


@app.get("/jobs", response_model=JobListResponse)
def list_jobs(
    repo: RepoDep,
    sub: SubDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> JobListResponse:
    items, next_cursor = repo.list_user_jobs(sub, limit=limit, cursor=cursor)
    return JobListResponse(items=[_to_response(item) for item in items], next_cursor=next_cursor)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, repo: RepoDep, sub: SubDep) -> JobResponse:
    item = repo.get_job(job_id)
    if item["user_sub"] != sub:
        # 404, not 403: don't leak other users' job ids.
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_response(item)
