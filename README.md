# aws-transcribe-pipeline

[![CI](https://github.com/klepkin-pv/aws-transcribe-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/klepkin-pv/aws-transcribe-pipeline/actions/workflows/ci.yml)

Serverless async processing pipeline on AWS: upload a file, get an automatic transcription and an
AI-generated summary and score back.

The domain is intentionally thin — the point of the project is the AWS side: managed services,
event-driven processing, scaling, fault tolerance and day-2 operations, all defined as code.

## Status

Deployed and exercised on a real AWS account (eu-central-1): `terraform apply` builds the whole
stack, and the jobs API has been driven live with the load test below. The transcription and
scoring steps are covered by tests against mocked AWS APIs — the account used for the deploy has
no Amazon Transcribe subscription, so that leg of the pipeline has not been run against the
real service. Deployment and triage procedures are in [docs/runbook.md](docs/runbook.md).

## Architecture

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

The reasoning behind every major choice — single-table design, capacity mode, dispatcher hop,
idempotency, concurrency limits, failure taxonomy — is written down in
[docs/architecture.md](docs/architecture.md).

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

Identity comes from the Cognito JWT validated by the API Gateway authorizer, so every route —
including ones added later — is rejected without a valid token.

## Upload flow

1. `POST /jobs` creates the job and returns a presigned S3 `PUT` URL scoped to
   `uploads/<sub>/<job_id>/<filename>` (valid for 15 minutes).
2. The client uploads the file straight to S3 — the file never passes through Lambda.
3. The `ObjectCreated` event starts the pipeline: dispatcher → SQS → worker starts an
   AWS Transcribe job → EventBridge delivers the completion event → the finalizer scores
   the transcript with a Bedrock model and writes the result atomically.
4. `GET /jobs/{id}` returns the final status with `score` and `summary`, or
   `failure_reason` for failed jobs.

`filename` is restricted to `[A-Za-z0-9][A-Za-z0-9._-]*` — no path separators, no leading
dots, so it cannot escape the caller's prefix. The signature pins `Content-Type`, and the
Lambda role holds `s3:PutObject` only under `uploads/*`.

## Operations

- **Dashboard** (`aws_cloudwatch_dashboard`): EMF job counters, queue depth and backlog age,
  DLQ depths, per-function errors and p99 duration, API 5xx.
- **Alarms → SNS**: Lambda errors/throttles (all four functions), both DLQs non-empty,
  queue backlog age > 10 min, API 5xx.
- **Runbook** ([docs/runbook.md](docs/runbook.md)): alarm triage, DLQ inspection and redrive,
  single-job replay, Logs Insights queries, cost guardrails.
- **Load test** (`scripts/load_test.py`): asyncio driver for `POST /jobs` + `GET /jobs`
  with latency percentiles — see the measured run below.
- **Cost model** ([docs/cost.md](docs/cost.md)): per-service prices and a 10k-jobs/month
  scenario, dominated by Transcribe media minutes.

### Measured

`scripts/load_test.py --concurrency 10 --duration 30` against the deployed API, 10 parallel
clients, each iteration a `POST /jobs` followed by a `GET /jobs`:

| metric | value |
|--------|-------|
| iterations | 1134 |
| throughput | 37.8 it/s |
| p50 | 114 ms |
| p95 | 134 ms |
| p99 | 311 ms |

Latency here is client-observed wall time, so it includes the round trip to `eu-central-1`;
per-function server-side duration lives in the dashboard widget.

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

1. [x] Repository and IaC foundation
2. [x] Jobs API: DynamoDB single-table + FastAPI on Lambda
3. [x] Cognito authorizer and presigned uploads
4. [x] Pipeline, part 1: S3 events → SQS → worker with idempotency
5. [x] Pipeline, part 2: transcription → scoring → results
6. [x] Fault tolerance and scaling: alarms, retries, concurrency controls
7. [x] Operations: dashboard, runbook, cost model, load test

Deployment is done and the API path is measured. Still to do: run the transcription and scoring
leg against the real services on an account with an Amazon Transcribe subscription, and
`terraform destroy` of the demo stage when the walkthrough is recorded.
