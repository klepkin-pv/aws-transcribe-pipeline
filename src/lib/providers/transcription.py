"""Transcription provider interface: AWS Transcribe async jobs + a test fake.

moto does not implement Transcribe, so the AWS job-starting call path is
covered by the interface separation alone; the S3 output-reading path is
fully testable against moto.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_MEDIA_FORMATS = {"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"}


class TranscriptionError(Exception):
    pass


class TranscriptionProvider(Protocol):
    def start_transcription(self, job_id: str, bucket: str, key: str) -> str:
        """Start an async transcription job and return its name."""
        ...

    def transcript_text(self, job_name: str) -> str:
        """Return the plain-text transcript of a finished job."""
        ...


class TranscribeProvider:
    """AWS Transcribe: output JSON lands in S3 under transcripts/<job name>.json."""

    OUTPUT_PREFIX = "transcripts/"

    def __init__(
        self, transcribe_client: Any, s3_client: Any, output_bucket: str, language: str
    ) -> None:
        self._transcribe = transcribe_client
        self._s3 = s3_client
        self._output_bucket = output_bucket
        self._language = language

    @staticmethod
    def job_name(job_id: str) -> str:
        # Transcribe names allow [0-9a-zA-Z._-]; our job ids are uuid hex.
        return f"job-{job_id}"

    def start_transcription(self, job_id: str, bucket: str, key: str) -> str:
        name = self.job_name(job_id)
        media_format = key.rsplit(".", 1)[-1].lower()
        if media_format not in _MEDIA_FORMATS:
            raise TranscriptionError(f"unsupported media format: .{media_format}")
        try:
            self._transcribe.start_transcription_job(
                TranscriptionJobName=name,
                LanguageCode=self._language,
                Media={"MediaFileUri": f"s3://{bucket}/{key}"},
                MediaFormat=media_format,
                OutputBucketName=self._output_bucket,
                OutputKey=f"{self.OUTPUT_PREFIX}{name}.json",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConflictException":
                # A retried message already started this job — same name,
                # same output location, so the retry can proceed.
                return name
            raise
        return name

    def transcript_text(self, job_name: str) -> str:
        obj = self._s3.get_object(
            Bucket=self._output_bucket, Key=f"{self.OUTPUT_PREFIX}{job_name}.json"
        )
        payload = json.loads(obj["Body"].read())
        try:
            return payload["results"]["transcripts"][0]["transcript"]
        except (KeyError, IndexError) as exc:
            raise TranscriptionError(f"unexpected transcript payload for {job_name}") from exc


class FakeTranscriptionProvider:
    """Deterministic fake: records started jobs, returns a fixed transcript."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str, str]] = []

    @staticmethod
    def job_name(job_id: str) -> str:
        return f"job-{job_id}"

    def start_transcription(self, job_id: str, bucket: str, key: str) -> str:
        self.started.append((job_id, bucket, key))
        return self.job_name(job_id)

    def transcript_text(self, job_name: str) -> str:
        return "fake transcript text"
