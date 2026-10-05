# Runbook

Day-2 operations for the pipeline. Everything here assumes `aws` CLI is configured for the
deployment account and `terraform output` values are at hand.

## Service level objectives (draft)

| Objective | Target | Measured by |
|---|---|---|
| API availability | 99.5% of successful responses | API Gateway 5xx alarm |
| API latency | p99 < 1s (create + list) | Lambda `Duration` p99 widget |
| Pipeline latency | job `done` in < 5 min p95 | EMF metrics `JobClaimed` → `JobDone` |

Numbers become real after the first deployment and load test.

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
