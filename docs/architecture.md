# Architecture decisions

Decisions made while building the pipeline, with the tradeoffs that were accepted.
The stack: API Gateway (HTTP API) → Lambda (FastAPI) → DynamoDB single-table;
S3 → dispatcher → SQS → worker → AWS Transcribe → EventBridge → finalizer (Bedrock scoring);
everything defined in Terraform.

## ADR-1: Single DynamoDB table

One table holds all job items. Base key `pk = USER#<sub>`, `sk = JOB#<created_at>#<job_id>`
serves the per-user listing (newest first, cheap pagination); a GSI on `gsi1pk = JOB#<job_id>`
serves the pipeline's access pattern, where a worker knows only the job id.
Accepted tradeoff: item duplication is avoided but the GSI projects all attributes.

## ADR-2: DynamoDB capacity mode — on-demand

Traffic is unpredictable (single-user demo today, load tests later, no idea about peaks).
On-demand removes capacity planning; the free tier covers 25 WCU/RCU + 25 GB.
If steady traffic appears, switching specific tables to provisioned + autoscaling is a
one-line Terraform change — the table is behind the repository abstraction.

## ADR-3: HTTP API instead of REST API

HTTP API is ~70% cheaper, has native JWT authorizers, and covers everything this API needs:
throttling, stage-level settings, access logs. REST API would add usage plans and API keys,
which are not needed here (auth is per-user Cognito JWT, not per-client keys).

## ADR-4: A dispatcher Lambda between S3 and SQS

S3 can deliver events straight to SQS. A dispatcher is worth one extra hop because it can:
validate the object key against the job layout, drop garbage/orphan uploads before they
become queue messages, deduplicate repeated S3 deliveries via a conditional write, and
keep the message schema in code. An S3→SQS destination would enqueue anything uploaded
under the prefix, including malicious junk.

## ADR-5: Conditional writes as the idempotency mechanism

The pipeline is at-least-once end to end: S3 retries, SQS redelivers, EventBridge retries.
Every state change is a `ConditionExpression` on the current status (a small state machine),
so a stale writer loses the race and skips instead of corrupting the state. The dispatcher's
`queued` transition doubles as the enqueue gate: only the winner sends a queue message.
Compensation: if the send fails after the transition, the dispatcher reverts to `created`
so the S3 retry can start over.

## ADR-6: Concurrency model

- API Lambda: `reserved_concurrency = 10` — a request burst cannot consume the account
  concurrency budget shared with the pipeline, nor hammer the table.
- Worker: `reserved_concurrency = 20` — SQS scales the worker with queue depth, the cap
  keeps the scale-out predictable for DynamoDB and the Transcribe quota.
- API Gateway stage: throttling at the edge (burst 50 / rate 100) rejects excess traffic
  before it becomes a billed Lambda invocation.
- Worker batch size 5 with `visibility_timeout = 90s`, above the 30s function timeout:
  a slow batch never double-processes messages.
- Both caps are Terraform variables (`api_reserved_concurrency`, `worker_reserved_concurrency`);
  `0` means "no reservation". A new AWS account has a Lambda limit of 10 concurrent executions and
  AWS requires 10 of them to stay unreserved, so the variables exist to let the stack deploy
  where a reservation is impossible — see [runbook.md](runbook.md).

## ADR-7: Failure taxonomy and retries

- Business skips (unknown job, duplicate delivery, foreign object) are logged and return
  success — they are not failures.
- Permanent errors (unsupported media format, scoring that cannot parse) mark the job
  `failed` with a `failure_reason`; retrying cannot help.
- Transient errors are reported via `ReportBatchItemFailures`: only the failed message is
  retried, the rest of the batch succeeds. SQS redelivery after the visibility timeout is
  the backoff — no home-grown sleep/retry loops.
- After 3 redeliveries the worker marks the job failed instead of looping it into the DLQ;
  the DLQs (jobs and pipeline events) stay for messages that have no job context at all.
  Both DLQs and the queue-backlog age raise CloudWatch alarms (see ADR-8).

## ADR-8: Observability

- Structured logs and custom metrics via AWS Lambda Powertools, deployed as a shared
  Lambda layer. Metrics use EMF (embedded metric format): they are extracted from logs,
  so there are no `PutMetricData` calls and no extra API cost.
- Custom metrics: `JobEnqueued`, `JobClaimed`, `JobDone`, `JobFailed` (+ cold starts).
- Alarms: Lambda errors and throttles (all four functions, via `for_each`), both DLQs
  non-empty, jobs-queue backlog age > 10 min, API 5xx. Everything goes to one SNS topic.
- X-Ray active on all functions (`tracing_config mode = Active`).

## ADR-9: Uploads bypass Lambda via presigned URLs

`POST /jobs` returns a presigned PUT scoped to `uploads/<sub>/<job_id>/<filename>` with
`Content-Type` pinned in the signature. Files never transit Lambda: no 6 MB payload limit
problems, no paying for media bytes through a function. The filename is validated at the
API edge (no separators, no leading dots), so a client cannot escape its own prefix; the
API Lambda's IAM allows `s3:PutObject` only under `uploads/*`.
