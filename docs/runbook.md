# Runbook

Day-2 operations for the pipeline. Everything here assumes `aws` CLI is configured for the
deployment account and `terraform output` values are at hand.

## Service level objectives

| Objective | Target | Measured by |
|---|---|---|
| API availability | 99.5% of successful responses | API Gateway 5xx alarm |
| API latency | p99 < 1s (create + list) | Lambda `Duration` p99 widget |
| Pipeline latency | job `done` in < 5 min p95 | EMF metrics `JobClaimed` → `JobDone` |

API latency is confirmed against a live deployment: 10 concurrent clients, 30 s, p50 114 ms /
p95 134 ms / p99 311 ms client-observed (see README). Pipeline latency is still the target only —
the deployment account has no Amazon Transcribe subscription, so no job has reached `done` there.

## Fresh accounts and reserved concurrency

A brand-new AWS account gets a Lambda account limit of 10 concurrent executions, and AWS refuses
any reservation that would leave fewer than 10 unreserved — so `terraform apply` fails with
`Specified ReservedConcurrentExecutions ... below its minimum value of [10]` on an untouched
account. Raise the quota first (Service Quotas → AWS Lambda → Concurrent executions), or deploy
with `-var="api_reserved_concurrency=0" -var="worker_reserved_concurrency=0"`, which removes the
reservations and lets the account-level limit act as the only cap. On any account that runs more
than one workload, keep the reservations — they are what stops an API burst from starving the
pipeline.

## Alarms → first response

| Alarm | Likely cause | Action |
|---|---|---|
| `<fn>-errors` | code bug, downstream outage | Logs Insights `pipeline/failed-jobs`, check X-Ray traces |
| `<fn>-throttles` | reserved concurrency too low / account limit | raise the cap in Terraform, or check account limits |
| `*-dlq-not-empty` | messages exhausted retries | inspect DLQ (below), fix the root cause, then redrive |
| `jobs-queue-backlog-age` | worker saturated or failing | check `worker-errors`, queue depth; raise `reserved_concurrency` |
| `api-5xx` | API function failing | logs + `lambda-errors` alarm, rollback via `source_code_hash` |

## Inspect a DLQ

```bash
aws sqs get-queue-url --queue-name aws-transcribe-pipeline-jobs-dlq-dev
aws sqs receive-message --queue-url <URL> --max-number-of-messages 5
```

Each message carries the job id in its body — look the job up by id
(`gsi1pk = JOB#<job_id>`) and correlate with the function logs.

## Redrive a DLQ after fixing the root cause

```bash
aws sqs start-message-move-task \
  --source-arn <jobs-dlq-arn> \
  --destination-arn <jobs-queue-arn>
```

Do not redrive blindly: first confirm the root cause is gone, otherwise the messages
just complete another 3-retry loop.

## Replay a single job

Re-uploading the object with the same key re-fires the S3 event, but the job is terminal,
so the dispatcher skips it. To reprocess, enqueue directly:

```bash
aws sqs send-message \
  --queue-url <jobs-queue-url> \
  --message-body '{"job_id":"<id>","bucket":"<uploads-bucket>","object_key":"uploads/<sub>/<id>/<file>"}'
```

The worker will fail the claim (status is terminal) — for a true replay, first copy the
job item with its status reset to `created` (write a small one-off script; deliberately
not automated to avoid accidental replays in production).

## Replay the scoring step for one job

The finalizer reads only `transcripts/job-<id>.json` from S3, so scoring can be re-run without
touching Amazon Transcribe: stage the transcript, put the job back into `transcribing`, and
deliver the same event Transcribe would have sent.

```bash
aws s3 cp transcript.json s3://<uploads-bucket>/transcripts/job-<id>.json
aws lambda invoke \
  --function-name aws-transcribe-pipeline-finalizer-dev \
  --payload '{"detail":{"TranscriptionJobName":"job-<id>","TranscriptionJobStatus":"COMPLETED"}}' \
  /dev/stdout
```

Useful when Transcribe is unavailable or over budget, and as the way to re-score with a
different model after a prompt change.

## Triage queries

Saved in CloudWatch (see `dashboard.tf`):
- `pipeline/failed-jobs` — recent failure log lines with reasons.
- `pipeline/skipped-messages` — skipped-event counts by reason; a spike in
  "upload without a job" means clients create jobs but never upload.

## Deployment

```bash
make package-api package-dispatcher package-worker package-finalizer package-layer
terraform -chdir=infra apply -var="alert_email=you@example.com" -var="bedrock_model_id=<model-id>"
```

Terraform state is local for now; before team use, switch to the S3 backend with
DynamoDB locking (`terraform init -migrate-state`).

## Cost guardrails

- Reserved concurrency caps are the blast radius limiter: even a runaway client cannot
  scale the pipeline beyond `reserved_concurrency` invocations.
- The `bedrock_model_id` default is empty: scoring is disabled until a model is explicitly
  configured, so forgetting it cannot produce surprise token spend.
- `aws budgets` (console, one-time): budget alert at $5/mo for the dev stage.
- `terraform destroy` removes everything (`force_destroy` is enabled on the uploads bucket).
- See `docs/cost.md` for the per-service cost model.
