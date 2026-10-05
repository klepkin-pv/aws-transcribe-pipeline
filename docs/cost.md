# Cost model

Approximate eu-central-1 prices (verify against the current AWS pricing pages before
drawing conclusions). Free-tier eligible parts are marked.

## Per-service

| Service | Price shape | Notes |
|---|---|---|
| Lambda | $0.20 / 1M requests + $0.0000166667 per GB-s | 400k GB-s/mo + 1M requests free (always free) |
| API Gateway HTTP API | ~$1.00 / 1M requests | first 1M requests free for 12 months |
| DynamoDB on-demand | ~$0.29 / WRU-M, ~$0.0625 / RRU-M? (see AWS page) | 25 WCU + RCU + 25 GB free for 12 months |
| S3 | ~$0.025 / GB-month storage, ~$0.005 / 1k PUT | uploads are short-lived media |
| SQS | $0.40 / 1M requests after the first 1M/mo free | DLQ polling is cheap |
| EventBridge | ~$1.00 / 1M custom events | only Transcribe state-change events here |
| Cognito | 50k MAU free | always free tier |
| CloudWatch | logs ~$0.50/GB ingested; custom metrics ~$0.30/metric-month; alarms $0.10 each | EMF metrics are extracted from logs |
| Transcribe | ~$0.024 / media minute | 60 min/mo free for 12 months |
| Bedrock | pay per token | disabled until `bedrock_model_id` is set |

## Scenario: 10,000 jobs per month

Assumptions: 2-minute average media, ~6 Lambda invocations per job (api, dispatcher,
worker, finalizer + retries), ~10 API requests per job, 1 KB log lines, 10 custom metrics.

| Item | Volume | Cost |
|---|---|---|
| Lambda (pipeline) | 60k invocations, ~2 GB-s each | ~$2.0 |
| API Gateway | 100k requests | ~$0.1 |
| DynamoDB | ~150k WRU + 500k RRU | ~$0.1 |
| S3 | 10k PUT + 20 GB-month transient | ~$0.6 |
| SQS + EventBridge | ~40k events | ~$0.05 |
| CloudWatch | ~2 GB logs, 10 metrics, ~20 alarms | ~$3.2 |
| Transcribe | 20,000 minutes | ~$480 (60 min free/mo) |
| Bedrock | ~20k × 2k tokens (in+out) | ~$5-20 depending on model |
| **Total** | | **~$490/month, dominated by Transcribe** |

## Takeaways

- The pipeline itself (everything except transcription and scoring) costs pennies at this
  scale: serverless glue is cheap, media minutes are not.
- Cheapest lever: keep `Transcribe` behind the provider interface and run tests against a
  fake — the development cost is literally zero.
- The cost-per-1M-requests story for the API alone: Lambda + API Gateway + DynamoDB land
  in the single-digit dollars per million end-to-end requests.
- Before any real deployment: set an AWS Budget alert first, deploy second.
