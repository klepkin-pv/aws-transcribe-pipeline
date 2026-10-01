"""Presigned upload URLs scoped to the caller's own S3 prefix."""

from __future__ import annotations

from typing import Any


def object_key(sub: str, job_id: str, filename: str) -> str:
    """Key under the user's own prefix; the API schema validates the filename."""
    return f"uploads/{sub}/{job_id}/{filename}"


def presigned_upload_url(
    s3_client: Any,
    bucket: str,
    sub: str,
    job_id: str,
    filename: str,
    content_type: str,
    expires_in: int = 900,
) -> str:
    """Presigned PUT valid for `expires_in` seconds.

    The signature pins Content-Type, so the client must send the same header,
    otherwise S3 rejects the upload.
    """
    return s3_client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": object_key(sub, job_id, filename),
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )
