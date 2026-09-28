"""Jobs repository on top of a DynamoDB single-table design.

Key layout (mirrors infra/dynamodb.tf):
- base item:  pk = USER#<cognito-sub>,  sk = JOB#<created_at>#<job_id>
- alt lookup: gsi1pk = JOB#<job_id>,    gsi1sk = JOB

Status transitions are guarded by server-side conditional writes, so two
workers racing on the same job cannot apply a stale update: the second
write fails with InvalidTransitionError instead of corrupting the state.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

JobItem = dict[str, Any]

JOB_TTL_DAYS = 30

# Forward edges of the job state machine. `failed` is reachable from every
# non-terminal state; terminal states have no outgoing edges.
STATE_MACHINE: dict[str, set[str]] = {
    "created": {"uploading", "queued", "failed"},
    "uploading": {"queued", "failed"},
    "queued": {"processing", "failed"},
    "processing": {"transcribing", "failed"},
    "transcribing": {"scoring", "failed"},
    "scoring": {"done", "failed"},
    "done": set(),
    "failed": set(),
}


class JobNotFoundError(Exception):
    pass


class JobAlreadyExistsError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass


def new_job_id() -> str:
    return uuid.uuid4().hex


def _encode_cursor(key: dict[str, Any]) -> str:
    raw = json.dumps(key, default=str).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(cursor.encode())
    return json.loads(raw)


class JobsRepository:
    """Data access for job items. Every method is a single DynamoDB API call."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def create_job(
        self,
        user_sub: str,
        filename: str,
        content_type: str,
        now: datetime,
        job_id: str | None = None,
    ) -> JobItem:
        """Create a job item in the `created` state.

        `job_id` is generated server-side (uuid4), so cross-user collisions
        are not a realistic scenario; the conditional write protects against
        replays of the same create call.
        """
        job_id = job_id or new_job_id()
        created_at = now.isoformat()
        item: JobItem = {
            "pk": f"USER#{user_sub}",
            "sk": f"JOB#{created_at}#{job_id}",
            "gsi1pk": f"JOB#{job_id}",
            "gsi1sk": "JOB",
            "job_id": job_id,
            "user_sub": user_sub,
            "status": "created",
            "filename": filename,
            "content_type": content_type,
            "created_at": created_at,
            "updated_at": created_at,
            "expires_at": int((now + timedelta(days=JOB_TTL_DAYS)).timestamp()),
        }
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise JobAlreadyExistsError(job_id) from exc
            raise
        return item

    def get_job(self, job_id: str) -> JobItem:
        response = self._table.query(
            IndexName="gsi1-job-id",
            KeyConditionExpression=Key("gsi1pk").eq(f"JOB#{job_id}"),
            Limit=1,
        )
        items = response.get("Items", [])
        if not items:
            raise JobNotFoundError(job_id)
        return items[0]

    def list_user_jobs(
        self, user_sub: str, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[JobItem], str | None]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"USER#{user_sub}"),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if cursor is not None:
            kwargs["ExclusiveStartKey"] = _decode_cursor(cursor)
        response = self._table.query(**kwargs)
        items = response.get("Items", [])
        if "LastEvaluatedKey" in response:
            return items, _encode_cursor(response["LastEvaluatedKey"])
        return items, None

    def transition(self, job_id: str, to_status: str, now: datetime) -> JobItem:
        """Move a job to `to_status` if the current status allows it.

        Read-then-write by design: the caller gets the fresh item either way,
        and the server-side condition makes the update atomic.
        """
        if to_status not in STATE_MACHINE:
            raise InvalidTransitionError(f"unknown status: {to_status}")
        item = self.get_job(job_id)

        allowed_from = sorted(
            from_status for from_status, targets in STATE_MACHINE.items() if to_status in targets
        )
        placeholders = ", ".join(f":s{i}" for i in range(len(allowed_from)))
        try:
            response = self._table.update_item(
                Key={"pk": item["pk"], "sk": item["sk"]},
                UpdateExpression="SET #status = :to, updated_at = :now",
                ConditionExpression=f"#status IN ({placeholders})",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    **{f":s{i}": value for i, value in enumerate(allowed_from)},
                    ":to": to_status,
                    ":now": now.isoformat(),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise InvalidTransitionError(f"{item['status']} -> {to_status}") from exc
            raise
        return response["Attributes"]
