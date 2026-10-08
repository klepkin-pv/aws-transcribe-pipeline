"""Provider unit tests. moto covers the S3 reading path; Transcribe is not in moto."""

import json

import boto3
import pytest
from moto import mock_aws

from lib.providers.scoring import (
    BedrockScoringProvider,
    FakeScoringProvider,
    ScoringError,
    parse_score,
)
from lib.providers.transcription import (
    FakeTranscriptionProvider,
    TranscribeProvider,
    TranscriptionError,
)


def test_fake_transcription_provider_is_deterministic():
    fake = FakeTranscriptionProvider()

    assert fake.start_transcription("id1", "bucket", "a.mp3") == "job-id1"
    assert fake.started == [("id1", "bucket", "a.mp3")]
    assert fake.transcript_text("job-id1") == "fake transcript text"


def test_transcribe_provider_rejects_unknown_media_format():
    provider = TranscribeProvider(None, None, "bucket", "ru-RU")

    with pytest.raises(TranscriptionError):
        provider.start_transcription("id1", "bucket", "uploads/s/j/notes.txt")


def test_transcribe_provider_transcript_text_reads_s3():
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-central-1")
        s3.create_bucket(
            Bucket="out", CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
        )
        s3.put_object(
            Bucket="out",
            Key="transcripts/job-id1.json",
            Body=json.dumps({"results": {"transcripts": [{"transcript": "hello"}]}}),
        )
        provider = TranscribeProvider(None, s3, "out", "ru-RU")

        assert provider.transcript_text("job-id1") == "hello"


def test_parse_score_extracts_json_from_reply():
    parsed = parse_score('noise {"score": 87, "summary": "ok"} noise')

    assert parsed == {"score": 87, "summary": "ok"}


def test_parse_score_rejects_garbage():
    with pytest.raises(ScoringError):
        parse_score("no json here")

    with pytest.raises(ScoringError):
        parse_score('{"score": 200, "summary": "out of range"}')


def test_bedrock_scoring_requires_model_id():
    provider = BedrockScoringProvider(None, "")

    with pytest.raises(ScoringError):
        provider.score("transcript")


def test_bedrock_scoring_sends_prompt_with_literal_json_braces():
    # The prompt template carries literal JSON, so its substitution must not go
    # through str.format — that raises KeyError on {"score": ...}.
    captured: dict = {}

    class StubBedrock:
        def converse(self, **kwargs):
            captured.update(kwargs)
            reply = '{"score": 87, "summary": "ok"}'
            return {"output": {"message": {"content": [{"text": reply}]}}}

    provider = BedrockScoringProvider(StubBedrock(), "amazon.nova-micro-v1:0")

    assert provider.score("hello there") == {"score": 87, "summary": "ok"}
    assert captured["modelId"] == "amazon.nova-micro-v1:0"
    prompt = captured["messages"][0]["content"][0]["text"]
    assert '{"score": <0-100>' in prompt
    assert prompt.endswith("hello there")


def test_fake_scoring_provider():
    assert FakeScoringProvider().score("hello") == {"score": 42, "summary": "fake summary"}
