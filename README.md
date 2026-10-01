# aws-transcribe-pipeline

[![CI](https://github.com/klepkin-pv/aws-transcribe-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/klepkin-pv/aws-transcribe-pipeline/actions/workflows/ci.yml)

Serverless async processing pipeline on AWS: upload a file, get an automatic transcription and an
AI-generated summary and score back.

The domain is intentionally thin — the point of the project is the AWS side: managed services,
event-driven processing, scaling, fault tolerance and day-2 operations, all defined as code.

## Status

Work in progress. The pipeline is being built incrementally; every commit keeps CI green.

## Architecture (draft)

```
client
  │
  ▼
API Gateway (HTTP API) ──── JWT authorizer (Cognito)
  │
  ▼
Lambda: api (FastAPI) ── POST /jobs returns a presigned PUT URL for S3
  │                       GET /jobs/{id} returns status and results
  ▼
DynamoDB single-table (job state)
  ▲
  │
S3 uploads/ ──ObjectCreated──▶ Lambda: dispatcher ──▶ SQS jobs (with DLQ)
                                                        │
                                                        ▼
                                                 Lambda: worker
                                                  │ starts an AWS Transcribe job
                                                  ▼
                                       EventBridge (Transcribe job completed)
                                                        │
                                                        ▼
                                                 Lambda: finalizer
                                                  │ transcript → scoring provider
                                                  ▼
                                       DynamoDB (status=done, score, summary)
```

External providers (AWS Transcribe, the LLM behind the scoring step) sit behind interfaces with
fakes in tests, so the whole pipeline is testable locally with `moto` and without paid API calls.

## Data model

Single DynamoDB table (`PAY_PER_REQUEST`, PITR enabled, TTL on `expires_at`):

- base item: `pk = USER#<sub>`, `sk = JOB#<created_at>#<job_id>` — per-user listing, newest first
- alternate lookup: GSI `gsi1pk = JOB#<job_id>` — pipeline workers reach a job by id alone
- job status follows a small state machine (`created → … → done / failed`); transitions are
  guarded by conditional writes, so a stale worker replay loses the race instead of corrupting
  the state

## API

- `POST /jobs` — create a job (`filename`, `content_type`); returns the job record
- `GET /jobs` — the caller's jobs, newest first; cursor pagination via `limit` (1–100) and `cursor`
- `GET /jobs/{job_id}` — a single job; other users' jobs return 404

Identity comes from the Cognito JWT validated by the API Gateway authorizer
(Terraform wiring lands with the function deployment step).

## Upload flow

1. `POST /jobs` creates the job and returns a presigned S3 `PUT` URL scoped to
   `uploads/<sub>/<job_id>/<filename>` (valid for 15 minutes).
2. The client uploads the file straight to S3 — the file never passes through Lambda.
3. The `ObjectCreated` event kicks off the pipeline (see roadmap, day 4).

`filename` is restricted to `[A-Za-z0-9][A-Za-z0-9._-]*` — no path separators, no leading
dots, so it cannot escape the caller's prefix. The signature pins `Content-Type`, and the
Lambda role holds `s3:PutObject` only under `uploads/*`.

## Stack

- Python 3.12, FastAPI, boto3
- AWS Lambda, API Gateway (HTTP API), DynamoDB, S3, Cognito, SQS, EventBridge, AWS Transcribe
- CloudWatch: structured logs, EMF metrics, dashboards, alarms, X-Ray
- Terraform for everything above
- pytest + moto, ruff, GitHub Actions

## Repository layout

```
src/          Lambda functions and shared library
infra/        Terraform (modules are extracted as the pipeline grows)
tests/        unit and moto-based integration tests
scripts/      load testing and maintenance scripts
docs/         architecture decisions, runbook, cost model
```

## Roadmap

1. Repository and IaC foundation
2. Jobs API: DynamoDB single-table + FastAPI on Lambda
3. Cognito authorizer and presigned uploads
4. Pipeline, part 1: S3 events → SQS → worker with idempotency
5. Pipeline, part 2: transcription → scoring → results
6. Fault tolerance and scaling: alarms, retries, concurrency controls
7. Operations: dashboard, runbook, cost model, load test
